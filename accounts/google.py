"""Sign in with Google: the only way into an account.

Off unless ``GOOGLE_OAUTH_CLIENT_IDS`` lists at least one client id. Two
functions do the work, and :func:`sign_in_with_google` wraps them for the
API (accounts/api.py):

* :func:`verify_id_token` checks Google's signature, the audience (any of
  our client ids -- the web client's and the Android app's differ) and
  expiry, then the issuer and ``email_verified`` on top.
* :func:`find_or_create_user` matches the verified claims to an account,
  on Google's stable ``sub`` first and the email second, or creates one.

Any failure anywhere collapses to one generic :class:`GoogleSignInError`,
logged without the token itself.

Changed from the reference: the audience is a list, and a repeat sign-in is
matched on ``sub`` before email. An email address can move between Google
accounts (a Workspace admin deletes a user and later creates another with
the same address); ``sub`` never does.
"""

import functools
import logging
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, transaction
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from limits import service as limits

from . import audit

logger = logging.getLogger(__name__)

User = get_user_model()

# Google documents both forms as valid issuers for an ID token.
ALLOWED_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

# Seconds to wait for Google's signing keys. The transport's own default is
# 120: a slow key endpoint would hold a worker for two minutes per sign-in.
CERTS_TIMEOUT = 10

# Never keep Google's keys longer than this, whatever Cache-Control says
# (Google's is about six hours). Keys rotate, and a token signed with a new
# one must not be refused for long if the retry in verify_id_token ever
# misses: the cap bounds that worst case (DECISIONS D81).
CERTS_MAX_TTL = 60 * 60

_MAX_AGE = re.compile(r"(?:^|[\s,])max-age=(\d+)", re.IGNORECASE)

# The User columns the claims land in (accounts/models.py).
_NAME_MAX = User._meta.get_field("name").max_length
_AVATAR_MAX = User._meta.get_field("avatar_url").max_length


class GoogleSignInError(Exception):
    """Refused: a bad token, an unverified email, or an account that may not sign in.

    One class, and the client is told nothing more than "Google sign-in
    failed": which check failed is useful to an attacker probing tokens and
    to nobody else. ``reason`` is for the security event trail only -- a
    short fixed word, never anything from the token.
    """

    def __init__(self, reason="invalid_token"):
        super().__init__(reason)
        self.reason = reason


class SignupsClosed(GoogleSignInError):
    """No new accounts for now: the ``signups`` system limit is reached (DECISIONS D131).

    Unlike every other refusal this one is told to the client as itself
    (403 ``signups_closed``): it says nothing about the token, and a person
    who cannot get in deserves to know why. Existing accounts still sign in.
    """

    def __init__(self):
        super().__init__("signups_closed")


def google_signin_enabled():
    """Whether Sign in with Google is switched on at all."""
    return bool(settings.GOOGLE_OAUTH_CLIENT_IDS)


def _transport():
    """google-auth's HTTP transport, with a timeout the library does not set."""
    return functools.partial(google_requests.Request(), timeout=CERTS_TIMEOUT)


class _CachedBody:
    """The part of a transport response that google-auth reads."""

    status = 200
    headers = {}

    def __init__(self, data):
        self.data = data


class CachedCertsRequest:
    """A google-auth transport that caches Google's signing keys (DECISIONS D81).

    Wraps another transport, and only touches a GET of the certificates URL
    (the one request verification makes): the response body is kept in
    Django's cache for the ``max-age`` of its Cache-Control header, capped
    at CERTS_MAX_TTL, keyed by URL. No max-age, ``no-store`` or ``no-cache``
    means not cached. ``from_cache`` records whether the keys came from the
    cache, so a failed verification can tell a rotated key from a bad token.
    """

    def __init__(self, transport):
        self.transport = transport
        self.from_cache = False

    @staticmethod
    def key(url):
        return f"google-certs:{url}"

    def __call__(self, url, method="GET", **kwargs):
        if method != "GET" or url != google_id_token._GOOGLE_OAUTH2_CERTS_URL:
            return self.transport(url, method=method, **kwargs)

        body = cache.get(self.key(url))
        if body is not None:
            self.from_cache = True
            return _CachedBody(body)

        self.from_cache = False
        response = self.transport(url, method=method, **kwargs)
        ttl = self._ttl(response)
        if response.status == 200 and ttl:
            cache.set(self.key(url), response.data, ttl)
        return response

    @staticmethod
    def _ttl(response):
        headers = {
            str(k).lower(): str(v) for k, v in (getattr(response, "headers", None) or {}).items()
        }
        control = headers.get("cache-control", "")
        if re.search(r"no-store|no-cache", control, re.IGNORECASE):
            return 0
        match = _MAX_AGE.search(control)
        return min(int(match.group(1)), CERTS_MAX_TTL) if match else 0

    def forget(self):
        cache.delete(self.key(google_id_token._GOOGLE_OAUTH2_CERTS_URL))


def verify_id_token(credential):
    """Return the verified claims of a Google ID token, or raise.

    Returns ``{"sub", "email", "name", "picture"}``, the email lower-cased.

    ``verify_oauth2_token`` checks the signature against Google's published
    keys, expiry, and the audience: google-auth 2.58 accepts a *list* there
    and requires ``aud`` to be one of its entries (google/auth/jwt.py,
    ``decode``), so every configured client id is accepted and no other.
    ``iss`` is checked again here although the library now checks it too --
    the reference's rule, and cheap insurance against a library change.
    ``email_verified`` is not checked by the library at all.
    """
    transport = CachedCertsRequest(_transport())
    try:
        try:
            payload = _verify(credential, transport)
        except Exception:
            if not transport.from_cache:
                raise
            # Fail closed, but not forever: keys that came from the cache
            # may be out of date (Google rotated them), so drop them and
            # verify once more against a fresh fetch. A token that is really
            # bad fails again, at the cost of the fetch sign-in made before
            # caching existed.
            transport.forget()
            payload = _verify(credential, transport)
    except Exception as exc:
        # Every failure the library can raise (bad signature, expired, wrong
        # audience, malformed token, Google's key endpoint unreachable)
        # collapses to the same refusal. Logged by exception type only, with
        # no message and no traceback: `credential` is a bearer credential
        # for the account it names, and some verifier errors echo part of
        # the offending value back.
        logger.warning("Google ID token verification failed: %s", type(exc).__name__)
        raise GoogleSignInError("invalid_token") from None

    if payload.get("iss") not in ALLOWED_ISSUERS:
        logger.warning("Google ID token had an unexpected issuer")
        raise GoogleSignInError("bad_issuer")

    # A Google account can carry an address it has never proved it owns
    # (a non-Gmail address typed in at sign-up). Linking by such an email
    # would hand whoever typed it the real owner's account here.
    if payload.get("email_verified") is not True:
        raise GoogleSignInError("email_unverified")

    sub = payload.get("sub")
    email = (payload.get("email") or "").strip().lower()
    if not sub or not email:
        raise GoogleSignInError("missing_claims")

    return {
        "sub": str(sub),
        "email": email,
        "name": (payload.get("name") or "").strip(),
        "picture": payload.get("picture") or "",
    }


def _verify(credential, transport):
    return google_id_token.verify_oauth2_token(
        credential,
        transport,
        audience=list(settings.GOOGLE_OAUTH_CLIENT_IDS),
    )


def _profile(claims):
    """Name and avatar from the claims, fitted to their columns.

    A name is cut to fit; an avatar URL that does not fit is dropped rather
    than cut, since a truncated URL is a broken image.
    """
    picture = claims.get("picture", "")
    return {
        "name": claims.get("name", "")[:_NAME_MAX],
        "avatar_url": picture if len(picture) <= _AVATAR_MAX else "",
    }


def find_or_create_user(claims):
    """Match verified ``claims`` to an account, or create one. Returns ``(user, created)``.

    1. **By ``sub``** -- the account this Google account signed in as before.
    2. **By email**, case-insensitively, for an account with no ``sub`` yet
       (made by ``createsuperuser`` or the admin) -- the ``sub`` is linked
       to it now. An account whose email matches but which is linked to a
       *different* ``sub`` is refused (DECISIONS D13): that address now
       belongs to another Google account, and its new holder is not the
       person whose notes are in there.
    3. **Otherwise a new account**, with no usable password -- if the day's
       ``signups`` limit has room (:class:`SignupsClosed` if not).

    A deactivated account is refused however it was matched. Name and
    avatar are refreshed from Google on every sign-in, and so is the email
    when Google's has changed and no other account holds the new one.
    """
    sub, email = claims["sub"], claims["email"]
    profile = _profile(claims)

    user = User.objects.filter(google_sub=sub).first()
    if user is None:
        user = User.objects.filter(email__iexact=email).first()
        if user is not None and user.google_sub and user.google_sub != sub:
            raise GoogleSignInError("sub_mismatch")

    if user is None:
        return _create_user(sub, email, profile)

    if not user.is_active:
        raise GoogleSignInError("inactive")

    changed = {"google_sub": sub, **profile}
    if user.email != email and not User.objects.filter(email__iexact=email).exists():
        changed["email"] = email
    stale = [field for field, value in changed.items() if getattr(user, field) != value]
    for field in stale:
        setattr(user, field, changed[field])
    if stale:
        user.save(update_fields=stale)
    return user, False


def _create_user(sub, email, profile):
    """A new account for a first sign-in.

    Consumes one ``signups`` (a system-only limit, no user yet) in the same
    transaction as the insert, so a sign-up that fails to insert hands it
    back. Full: :class:`SignupsClosed`, and nothing is created.

    Two first sign-ins of the same Google account can race (a double tap,
    or web and Android at once). The unique constraints on ``email`` and
    ``google_sub`` let only one insert through; the other finds the winner
    and signs in as it.
    """
    try:
        with transaction.atomic():
            limits.consume(None, "signups")
            return User.objects.create_user(email, google_sub=sub, **profile), True
    except limits.SystemLimitExceeded:
        # The race below, at the last place of the day: the winner took it,
        # and this request, refused, is the same person -- sign them in.
        user = User.objects.filter(google_sub=sub).first()
        if user is not None and user.is_active:
            return user, False
        raise SignupsClosed() from None
    except IntegrityError:
        user = User.objects.filter(google_sub=sub).first()
        if user is None or not user.is_active:
            raise GoogleSignInError("conflict") from None
        return user, False


def sign_in_with_google(credential, request=None):
    """Verify ``credential`` and resolve it to an account. Returns ``(user, created)``.

    Records ``google_login_failed`` (with the reason) on any refusal and
    re-raises :class:`GoogleSignInError`; ``google_login_succeeded`` -- and,
    for a new account, ``signed_up`` first -- on success. The caller still
    issues the tokens.
    """
    claims = None
    try:
        claims = verify_id_token(credential)
        user, created = find_or_create_user(claims)
    except GoogleSignInError as exc:
        # The email only once Google has vouched for it: before that it is
        # whatever an unverified token claimed.
        email = claims["email"] if claims else ""
        audit.record("google_login_failed", request=request, email=email, reason=exc.reason)
        raise

    if created:
        audit.record("signed_up", request=request, user=user, via="google")
    audit.record("google_login_succeeded", request=request, user=user)
    return user, created
