"""The account API: auth/google/, auth/refresh/, auth/logout/, me/.

Google's side is faked by fake_google.py: real tokens, really verified, with
our certificate served in place of Google's. No network.
"""

import logging
import time
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.views.debug import SafeExceptionReporterFilter
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

from accounts import api, ratelimit
from accounts.models import SecurityEvent

from .fake_google import ANDROID_CLIENT_ID, CLIENT_IDS, google_keys, id_token

User = get_user_model()

with_google = override_settings(GOOGLE_OAUTH_CLIENT_IDS=CLIENT_IDS)
with_cache = override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "api-auth-tests",
        }
    }
)


def url(name):
    return reverse(f"api:v1:{name}")


class AuthApiTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()

    def post(self, name, data, **extra):
        return self.client.post(url(name), data, format="json", **extra)

    def google(self, token=None, **extra):
        with google_keys():
            return self.post("auth-google", {"id_token": token or id_token()}, **extra)

    def sign_in(self, **claims):
        response = self.google(id_token(**claims))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def me(self, access):
        return APIClient().get(url("me"), HTTP_AUTHORIZATION=f"Bearer {access}")

    def refresh(self, refresh):
        return APIClient().post(url("auth-refresh"), {"refresh": refresh}, format="json")


class GoogleOffTests(AuthApiTestCase):
    @override_settings(GOOGLE_OAUTH_CLIENT_IDS=[])
    def test_sign_in_is_404_until_a_client_id_is_configured(self):
        response = self.google()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(User.objects.count(), 0)


@with_google
class GoogleSignInTests(AuthApiTestCase):
    def test_a_valid_token_creates_the_account_and_returns_a_token_pair(self):
        pair = self.sign_in()

        user = User.objects.get()
        self.assertEqual(set(pair), {"access", "refresh", "user"})
        self.assertEqual(
            pair["user"],
            {
                "id": user.pk,
                "email": "alice@example.com",
                "name": "Alice Example",
                "avatar_url": "https://lh3.googleusercontent.com/a/alice",
                "plan": "free",
                "date_joined": pair["user"]["date_joined"],
                "timezone": "Asia/Kolkata",
                "memory_enabled": True,
                "memory_choice_explicit": False,
                "ask_usage": pair["user"]["ask_usage"],
            },
        )
        self.assertFalse(user.has_usable_password())
        self.assertIsNotNone(user.last_login)
        self.assertEqual(self.me(pair["access"]).json()["id"], user.pk)

    def test_a_repeat_sign_in_is_the_same_account(self):
        first = self.sign_in()
        again = self.sign_in(aud=ANDROID_CLIENT_ID)

        self.assertEqual(again["user"]["id"], first["user"]["id"])
        self.assertEqual(User.objects.count(), 1)

    def test_a_pre_existing_account_is_linked_by_email(self):
        existing = User.objects.create_user("alice@example.com")

        pair = self.sign_in(email="Alice@Example.com")

        self.assertEqual(pair["user"]["id"], existing.pk)
        existing.refresh_from_db()
        self.assertEqual(existing.google_sub, "111111111111111111111")

    def assertGoogleFailed(self, response):
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(), {"detail": "Google sign-in failed.", "code": "google_failed"}
        )

    def test_every_bad_token_gets_the_same_refusal(self):
        past = int(time.time()) - 7200
        cases = {
            "wrong audience": id_token(aud="someone-else.apps.googleusercontent.com"),
            "bad issuer": id_token(iss="https://evil.example.com"),
            "expired": id_token(iat=past, exp=past + 3600),
            "unverified email": id_token(email_verified=False),
            "garbage": "not-a-token",
        }
        with self.assertLogs("accounts.google", level="WARNING"):
            for case, token in cases.items():
                with self.subTest(case):
                    self.assertGoogleFailed(self.google(token))

        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(
            SecurityEvent.objects.filter(event="google_login_failed").count(), len(cases)
        )

    def test_a_deactivated_account_is_refused(self):
        User.objects.create_user("alice@example.com", is_active=False)
        self.assertGoogleFailed(self.google())

    def test_a_missing_id_token_is_a_field_error(self):
        response = self.post("auth-google", {})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid")
        self.assertIn("id_token", response.json())

    def test_an_expired_access_token_sent_along_does_not_block_sign_in(self):
        response = self.google(HTTP_AUTHORIZATION="Bearer expired.garbage.token")
        self.assertEqual(response.status_code, 200)


@with_google
@with_cache
class GoogleRateLimitTests(AuthApiTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def test_failures_from_one_address_are_blocked_after_the_limit(self):
        with (
            mock.patch.object(ratelimit, "GOOGLE_LOGIN_IP_LIMIT", 3),
            self.assertLogs("accounts.google", level="WARNING"),
        ):
            for _ in range(3):
                self.assertEqual(self.google("not-a-token").status_code, 400)

            blocked = self.google()  # even a good token, from this address

        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json()["code"], "rate_limited")
        self.assertEqual(User.objects.count(), 0)
        self.assertTrue(SecurityEvent.objects.filter(event="login_blocked").exists())

    def test_another_address_is_not_blocked(self):
        with (
            mock.patch.object(ratelimit, "GOOGLE_LOGIN_IP_LIMIT", 1),
            self.assertLogs("accounts.google", level="WARNING"),
        ):
            self.google("not-a-token", REMOTE_ADDR="198.51.100.1")
            self.assertEqual(self.google(REMOTE_ADDR="198.51.100.2").status_code, 200)

    def test_successes_are_not_counted(self):
        with mock.patch.object(ratelimit, "GOOGLE_LOGIN_IP_LIMIT", 1):
            for _ in range(3):
                self.assertEqual(self.google().status_code, 200)

    def test_the_auth_endpoints_share_the_auth_throttle_scope(self):
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"auth": "2/hour"}):
            self.assertEqual(self.google().status_code, 200)
            self.assertEqual(self.refresh("junk").status_code, 401)
            response = self.post("auth-logout", {"refresh": "junk"})

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "throttled")


@with_google
class RefreshTests(AuthApiTestCase):
    def test_a_refresh_returns_a_new_pair_that_works(self):
        pair = self.sign_in()

        response = self.refresh(pair["refresh"])

        self.assertEqual(response.status_code, 200)
        new = response.json()
        self.assertEqual(set(new), {"access", "refresh"})
        self.assertNotEqual(new["refresh"], pair["refresh"])
        self.assertEqual(self.me(new["access"]).status_code, 200)

    def test_a_rotated_refresh_token_cannot_be_reused(self):
        pair = self.sign_in()
        self.assertEqual(self.refresh(pair["refresh"]).status_code, 200)

        replay = self.refresh(pair["refresh"])

        self.assertEqual(replay.status_code, 401)
        self.assertEqual(replay.json()["code"], "token_not_valid")

    def test_junk_is_401_and_a_missing_token_400(self):
        self.assertEqual(self.refresh("junk").status_code, 401)
        self.assertEqual(APIClient().post(url("auth-refresh"), {}, format="json").status_code, 400)

    def test_a_token_whose_account_was_deleted_is_401_not_500(self):
        user = User.objects.create_user("gone@example.com")
        refresh = str(RefreshToken.for_user(user))
        User.objects.filter(pk=user.pk).delete()

        self.assertEqual(self.refresh(refresh).status_code, 401)

    def test_a_deactivated_account_cannot_refresh(self):
        pair = self.sign_in()
        User.objects.update(is_active=False)

        self.assertEqual(self.refresh(pair["refresh"]).status_code, 401)


@with_google
class LogoutTests(AuthApiTestCase):
    def test_logout_blacklists_the_refresh_token(self):
        pair = self.sign_in()

        response = self.post("auth-logout", {"refresh": pair["refresh"]})

        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.refresh(pair["refresh"]).status_code, 401)
        event = SecurityEvent.objects.get(event="logged_out")
        self.assertEqual(event.user_id, pair["user"]["id"])

    def test_logging_out_twice_or_with_junk_is_400(self):
        pair = self.sign_in()
        self.post("auth-logout", {"refresh": pair["refresh"]})

        for token in (pair["refresh"], "junk"):
            response = self.post("auth-logout", {"refresh": token})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["code"], "token_invalid")

    def test_a_missing_token_is_a_field_error(self):
        self.assertEqual(self.post("auth-logout", {}).status_code, 400)

    def test_logout_signs_out_only_that_device(self):
        phone = self.sign_in()
        laptop = self.sign_in()

        self.post("auth-logout", {"refresh": phone["refresh"]})

        self.assertEqual(self.refresh(laptop["refresh"]).status_code, 200)


@with_google
class MeTests(AuthApiTestCase):
    def test_me_requires_a_bearer_token(self):
        response = self.client.get(url("me"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "not_authenticated")

    def test_me_refuses_a_refresh_token_as_a_bearer(self):
        pair = self.sign_in()
        self.assertEqual(self.me(pair["refresh"]).status_code, 401)

    def test_me_returns_the_profile_with_the_plan(self):
        pair = self.sign_in()
        User.objects.update(plan=User.Plan.PREMIUM)

        body = self.me(pair["access"]).json()

        self.assertEqual(
            set(body),
            {
                "id",
                "email",
                "name",
                "avatar_url",
                "plan",
                "date_joined",
                "timezone",
                "memory_enabled",
                "memory_choice_explicit",
                "ask_usage",
            },
        )
        self.assertEqual(body["plan"], "premium")
        # The quota follows the plan (assistant/tests/test_api.py covers the count).
        self.assertEqual(body["ask_usage"]["limit"], settings.ASK_QUOTAS["premium"])


# --- @sensitive_variables() ------------------------------------------------
#
# Each test raises from inside the real view, after it has bound a token to
# a local, and checks what SafeExceptionReporterFilter -- the filter behind
# both the DEBUG error page and the mail_admins traceback -- would show for
# that frame. Ported from the reference's test_security_pass5.py.

CLEANSED = SafeExceptionReporterFilter.cleansed_substitute


def _raise(callable_):
    # Not assertRaises(): its context manager clears the traceback, which is
    # exactly the frame chain this needs. The expected 500 is kept out of
    # the test output.
    try:
        with mock.patch.object(logging.getLogger("django.request"), "disabled", True):
            callable_()
    except RuntimeError as exc:
        return exc
    raise AssertionError("expected a RuntimeError")


def _cleansed_locals_of_innermost(exc, fragment="accounts/api.py"):
    frame = None
    tb = exc.__traceback__
    while tb is not None:
        if fragment in tb.tb_frame.f_code.co_filename:
            frame = tb.tb_frame
        tb = tb.tb_next
    with override_settings(DEBUG=False):
        return dict(SafeExceptionReporterFilter().get_traceback_frame_variables(None, frame))


@with_google
class SensitiveVariablesTests(AuthApiTestCase):
    def test_google_sign_in_masks_the_id_token(self):
        token = id_token()
        with mock.patch("accounts.api.issue_tokens", side_effect=RuntimeError("boom")):
            exc = _raise(lambda: self.google(token))

        cleansed = _cleansed_locals_of_innermost(exc)
        self.assertEqual(cleansed.get("body"), CLEANSED)
        self.assertNotIn(token, repr(cleansed))

    def test_logout_masks_the_refresh_token(self):
        refresh = str(RefreshToken.for_user(User.objects.create_user("a@example.com")))
        with mock.patch("accounts.api.RefreshToken.blacklist", side_effect=RuntimeError("boom")):
            exc = _raise(lambda: self.post("auth-logout", {"refresh": refresh}))

        cleansed = _cleansed_locals_of_innermost(exc)
        self.assertEqual(cleansed.get("token"), CLEANSED)
        self.assertNotIn(refresh, repr(cleansed))

    def test_every_view_holding_a_token_carries_the_decorator(self):
        # Structural, for RefreshView: it delegates to simplejwt's base class,
        # so a live exception is awkward to provoke at the right point.
        for view in (api.GoogleLoginView, api.RefreshView, api.LogoutView):
            with self.subTest(view=view.__name__):
                self.assertTrue(hasattr(view.post, "__wrapped__"))
