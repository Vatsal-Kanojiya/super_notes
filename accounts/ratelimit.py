"""A cache-backed counter for failed sign-ins, keyed by address.

Carried over from the reference (expense_management/accounts/ratelimit.py),
cut down to the one door this project has: Sign in with Google. There is no
password to guess, so the reference's per-username and per-account caps
have nothing to count. What is left is the per-address cap on *failed*
Google sign-ins.

**Why count failures at all, with no password?** A failed Google sign-in
is a token that did not verify: forged, expired, meant for another app, or
replayed from somewhere it leaked. None of those is a person mistyping.
Each attempt also costs a fetch of Google's signing keys and a signature
check. Fifty in fifteen minutes from one address is a script.

**Keying is the design.** Before the token is verified there is no account
to key on -- the credential names the account, not a form field -- so the
key is the address alone. That means one office behind one NAT shares the
budget, which is why it is generous; and a botnet with a thousand
addresses is not stopped, only slowed. The DRF ``auth`` throttle
(config/settings.py) sits on top and counts *every* request to the auth
endpoints, successful or not.

**Why hand-written.** As in the reference: forty lines make the mechanism
visible -- a counter per key, a fixed window, and a decision about the
key. That decision is the part a library hides.
"""

from django.conf import settings
from django.core.cache import cache

# Failed Google sign-ins from one address. High enough for an office
# behind one NAT whose clocks are wrong, low enough to make a script visible.
GOOGLE_LOGIN_IP_LIMIT = 50
GOOGLE_LOGIN_IP_WINDOW = 15 * 60


def client_ip(request):
    """The caller's address, as far as the deployment can vouch for it.

    With no proxy in front (TRUSTED_PROXY_COUNT = 0), REMOTE_ADDR is the
    client, and X-Forwarded-For is ignored: it is whatever the client typed.

    Behind N trusted proxies, each appends the address it received the
    request from, so the entry N places from the right is the one the
    outermost trusted proxy saw. Everything to its left arrived with the
    request and is the client's to invent. Reading the left-most entry --
    as the reference once did -- lets any caller pick the key its attempts
    are counted under.
    """
    proxies = getattr(settings, "TRUSTED_PROXY_COUNT", 0)
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if proxies > 0 and forwarded:
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if hops:
            return hops[-min(proxies, len(hops))]

    return request.META.get("REMOTE_ADDR", "unknown")


def _key(scope, request):
    return f"ratelimit:{scope}:{client_ip(request)}"


def is_limited(scope, request, limit):
    """Whether this address has already used up its attempts.

    Read-only, so it can be asked before doing the work without consuming
    an attempt itself.
    """
    return (cache.get(_key(scope, request)) or 0) >= limit


def record_attempt(scope, request, window):
    """Count one attempt against this address, and return the total.

    The window is fixed, not sliding: the counter expires as a whole
    rather than ageing entry by entry, so a determined caller can get up to
    twice the limit across a window boundary. A sliding window costs a
    sorted set per key and is not worth it here, but the gap should be
    known rather than assumed away.

    add() then incr(), because incr() raises on a missing key and set()
    would reset the expiry on every attempt -- the window would restart
    for ever and the limit would be unreachable.
    """
    key = _key(scope, request)
    cache.add(key, 0, window)
    try:
        return cache.incr(key)
    except ValueError:
        # The key expired between add() and incr(). Rare, and the right
        # response is to treat this attempt as the first of a new window.
        cache.set(key, 1, window)
        return 1


# --- Sign in with Google --------------------------------------------------
#
# Only failures are counted, and a success does not clear the count: an
# address that sent forty forged tokens and then one good one is still an
# address that sent forty forged tokens.


def google_login_blocked(request):
    return is_limited("google-login-ip", request, GOOGLE_LOGIN_IP_LIMIT)


def record_google_login_failure(request):
    return record_attempt("google-login-ip", request, GOOGLE_LOGIN_IP_WINDOW)
