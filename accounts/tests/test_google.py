"""accounts/google.py: verifying Google ID tokens and finding the account.

Tokens are really signed and really verified by google-auth, against a
throwaway key served in place of Google's (fake_google.py). No network.
"""

import time
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from accounts import google
from accounts.models import SecurityEvent

from .fake_google import ANDROID_CLIENT_ID, CLIENT_IDS, claims, google_keys, id_token

User = get_user_model()

with_google = override_settings(GOOGLE_OAUTH_CLIENT_IDS=CLIENT_IDS)


class SwitchTests(TestCase):
    @override_settings(GOOGLE_OAUTH_CLIENT_IDS=[])
    def test_off_when_no_client_id_is_configured(self):
        self.assertFalse(google.google_signin_enabled())

    @with_google
    def test_on_once_configured(self):
        self.assertTrue(google.google_signin_enabled())

    def test_the_transport_has_a_timeout(self):
        transport = google._transport()
        self.assertEqual(transport.keywords, {"timeout": google.CERTS_TIMEOUT})


@with_google
class VerifyIdTokenTests(TestCase):
    def verify(self, token):
        with google_keys():
            return google.verify_id_token(token)

    def assertRefused(self, token, reason):
        with self.assertRaises(google.GoogleSignInError) as caught:
            self.verify(token)
        self.assertEqual(caught.exception.reason, reason)

    def test_a_good_token_returns_the_claims_with_the_email_lowercased(self):
        self.assertEqual(
            self.verify(id_token(email="Alice@Example.COM")),
            {
                "sub": "111111111111111111111",
                "email": "alice@example.com",
                "name": "Alice Example",
                "picture": "https://lh3.googleusercontent.com/a/alice",
            },
        )

    def test_every_configured_audience_is_accepted(self):
        self.assertEqual(self.verify(id_token(aud=ANDROID_CLIENT_ID))["email"], "alice@example.com")

    def test_a_token_for_another_app_is_refused(self):
        with self.assertLogs("accounts.google", level="WARNING"):
            self.assertRefused(
                id_token(aud="someone-else.apps.googleusercontent.com"), "invalid_token"
            )

    def test_an_expired_token_is_refused(self):
        past = int(time.time()) - 7200
        with self.assertLogs("accounts.google", level="WARNING"):
            self.assertRefused(id_token(iat=past, exp=past + 3600), "invalid_token")

    def test_a_token_signed_by_another_key_is_refused(self):
        good = id_token()
        header, payload, signature = good.split(".")
        tampered = ".".join([header, payload, signature[::-1]])
        with self.assertLogs("accounts.google", level="WARNING"):
            self.assertRefused(tampered, "invalid_token")

    def test_garbage_is_refused(self):
        with self.assertLogs("accounts.google", level="WARNING"):
            self.assertRefused("not-a-token", "invalid_token")

    def test_a_bad_issuer_is_refused_by_the_library(self):
        with self.assertLogs("accounts.google", level="WARNING") as logs:
            self.assertRefused(id_token(iss="https://evil.example.com"), "invalid_token")
        self.assertIn("GoogleAuthError", logs.output[0])

    def test_a_bad_issuer_is_refused_even_if_the_library_let_it_through(self):
        with mock.patch.object(
            google.google_id_token,
            "verify_oauth2_token",
            return_value=claims(iss="https://evil.example.com"),
        ):
            with self.assertLogs("accounts.google", level="WARNING"):
                with self.assertRaises(google.GoogleSignInError) as caught:
                    google.verify_id_token("token")
        self.assertEqual(caught.exception.reason, "bad_issuer")

    def test_the_issuer_without_a_scheme_is_accepted(self):
        self.assertEqual(self.verify(id_token(iss="accounts.google.com"))["sub"], claims()["sub"])

    def test_an_unverified_email_is_refused(self):
        self.assertRefused(id_token(email_verified=False), "email_unverified")

    def test_email_verified_must_be_true_not_merely_truthy(self):
        self.assertRefused(id_token(email_verified="false"), "email_unverified")

    def test_a_token_without_an_email_or_sub_is_refused(self):
        self.assertRefused(id_token(email=None), "missing_claims")
        self.assertRefused(id_token(sub=None), "missing_claims")

    def test_name_and_picture_are_optional(self):
        verified = self.verify(id_token(name=None, picture=None))
        self.assertEqual((verified["name"], verified["picture"]), ("", ""))

    def test_the_token_is_never_logged(self):
        secret = "eyJ-super-secret-token-xyz"
        with mock.patch.object(
            google.google_id_token,
            "verify_oauth2_token",
            side_effect=ValueError(f"bad signature: {secret}"),
        ):
            with self.assertLogs("accounts.google", level="WARNING") as logs:
                with self.assertRaises(google.GoogleSignInError):
                    google.verify_id_token(secret)

        self.assertEqual(
            logs.output, ["WARNING:accounts.google:Google ID token verification failed: ValueError"]
        )

    def test_the_audience_is_passed_as_the_whole_list(self):
        with mock.patch.object(
            google.google_id_token, "verify_oauth2_token", return_value=claims()
        ) as verify:
            google.verify_id_token("token")
        self.assertEqual(verify.call_args.kwargs["audience"], CLIENT_IDS)


def verified(**overrides):
    return {
        "sub": "111",
        "email": "alice@example.com",
        "name": "Alice",
        "picture": "https://example.com/a.png",
        **overrides,
    }


class FindOrCreateUserTests(TestCase):
    def test_no_match_creates_an_account_without_a_password(self):
        user, created = google.find_or_create_user(verified())

        self.assertTrue(created)
        self.assertEqual(user.email, "alice@example.com")
        self.assertEqual(user.google_sub, "111")
        self.assertEqual(user.name, "Alice")
        self.assertEqual(user.avatar_url, "https://example.com/a.png")
        self.assertFalse(user.has_usable_password())

    def test_a_repeat_sign_in_is_matched_on_sub(self):
        first, _ = google.find_or_create_user(verified())
        again, created = google.find_or_create_user(verified())

        self.assertFalse(created)
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(User.objects.count(), 1)

    def test_sub_wins_over_email_when_googles_email_changed(self):
        first, _ = google.find_or_create_user(verified())

        again, created = google.find_or_create_user(verified(email="alice@new.example.com"))

        self.assertFalse(created)
        self.assertEqual(again.pk, first.pk)
        again.refresh_from_db()
        self.assertEqual(again.email, "alice@new.example.com")

    def test_a_changed_email_already_held_by_another_account_is_not_taken(self):
        first, _ = google.find_or_create_user(verified())
        User.objects.create_user("bob@example.com")

        again, _ = google.find_or_create_user(verified(email="bob@example.com"))

        self.assertEqual(again.pk, first.pk)
        again.refresh_from_db()
        self.assertEqual(again.email, "alice@example.com")

    def test_an_existing_account_without_a_sub_is_matched_by_email_and_linked(self):
        existing = User.objects.create_user("alice@example.com")

        user, created = google.find_or_create_user(verified(email="alice@example.com"))

        self.assertFalse(created)
        self.assertEqual(user.pk, existing.pk)
        existing.refresh_from_db()
        self.assertEqual(existing.google_sub, "111")

    def test_the_email_match_ignores_case(self):
        existing = User.objects.create_user("alice@example.com")
        User.objects.filter(pk=existing.pk).update(email="Alice@Example.com")

        user, _ = google.find_or_create_user(verified())

        self.assertEqual(user.pk, existing.pk)

    def test_an_email_linked_to_a_different_sub_is_refused(self):
        User.objects.create_user("alice@example.com", google_sub="999")

        with self.assertRaises(google.GoogleSignInError) as caught:
            google.find_or_create_user(verified(sub="111"))

        self.assertEqual(caught.exception.reason, "sub_mismatch")
        self.assertEqual(User.objects.get().google_sub, "999")

    def test_a_deactivated_account_is_refused_by_sub_and_by_email(self):
        User.objects.create_user("alice@example.com", google_sub="111", is_active=False)
        User.objects.create_user("bob@example.com", is_active=False)

        for claim_set in (verified(), verified(sub="222", email="bob@example.com")):
            with self.assertRaises(google.GoogleSignInError) as caught:
                google.find_or_create_user(claim_set)
            self.assertEqual(caught.exception.reason, "inactive")

    def test_name_and_avatar_are_refreshed_on_every_sign_in(self):
        google.find_or_create_user(verified())

        user, _ = google.find_or_create_user(
            verified(name="Alice B.", picture="https://example.com/b.png")
        )

        user.refresh_from_db()
        self.assertEqual((user.name, user.avatar_url), ("Alice B.", "https://example.com/b.png"))

    def test_an_unchanged_profile_is_not_written(self):
        google.find_or_create_user(verified())
        with self.assertNumQueries(1):
            google.find_or_create_user(verified())

    def test_an_overlong_name_is_cut_and_an_overlong_avatar_dropped(self):
        user, _ = google.find_or_create_user(
            verified(name="N" * 300, picture="https://example.com/" + "a" * 600)
        )
        self.assertEqual(user.name, "N" * 150)
        self.assertEqual(user.avatar_url, "")

    def test_a_first_sign_in_that_loses_a_race_signs_in_as_the_winner(self):
        winner = User.objects.create_user("alice@example.com", google_sub="111")
        # Both requests looked before either inserted: this one saw nothing.
        with mock.patch.object(User.objects, "filter", wraps=User.objects.filter) as filt:
            filt.side_effect = [User.objects.none(), User.objects.none(), User.objects.all()]
            user, created = google.find_or_create_user(verified())

        self.assertFalse(created)
        self.assertEqual(user.pk, winner.pk)

    def test_a_race_lost_to_a_different_account_is_refused(self):
        User.objects.create_user("alice@example.com", google_sub="999")
        with mock.patch.object(User.objects, "filter") as filt:
            filt.return_value = User.objects.none()
            with self.assertRaises(google.GoogleSignInError) as caught:
                google.find_or_create_user(verified())
        self.assertEqual(caught.exception.reason, "conflict")


@with_google
class SignInWithGoogleTests(TestCase):
    def test_a_first_sign_in_records_signed_up_then_succeeded(self):
        with google_keys():
            user, created = google.sign_in_with_google(id_token())

        self.assertTrue(created)
        events = list(SecurityEvent.objects.order_by("id").values_list("event", "user", "detail"))
        self.assertEqual(
            events,
            [("signed_up", user.pk, {"via": "google"}), ("google_login_succeeded", user.pk, {})],
        )

    def test_a_repeat_sign_in_records_only_success(self):
        with google_keys():
            google.sign_in_with_google(id_token())
            SecurityEvent.objects.all().delete()
            _, created = google.sign_in_with_google(id_token())

        self.assertFalse(created)
        self.assertEqual(
            list(SecurityEvent.objects.values_list("event", flat=True)), ["google_login_succeeded"]
        )

    def test_a_bad_token_records_the_failure_without_an_email(self):
        with google_keys(), self.assertLogs("accounts.google", level="WARNING"):
            with self.assertRaises(google.GoogleSignInError):
                google.sign_in_with_google(id_token(aud="someone-else"))

        event = SecurityEvent.objects.get()
        self.assertEqual(event.event, "google_login_failed")
        self.assertEqual(event.email, "")
        self.assertEqual(event.detail, {"reason": "invalid_token"})

    def test_a_refused_account_records_the_verified_email_and_why(self):
        User.objects.create_user("alice@example.com", google_sub="999")
        with google_keys(), self.assertRaises(google.GoogleSignInError):
            google.sign_in_with_google(id_token())

        event = SecurityEvent.objects.get()
        self.assertEqual(
            (event.email, event.detail), ("alice@example.com", {"reason": "sub_mismatch"})
        )

    def test_nothing_from_the_token_is_stored(self):
        token = id_token()
        with google_keys():
            google.sign_in_with_google(token)

        for event in SecurityEvent.objects.all():
            self.assertNotIn(token, str(event.detail))
            self.assertNotIn(claims()["sub"], str(event.detail))
