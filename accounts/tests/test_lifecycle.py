"""Lifecycle (D88, D90, D94), part 1: the User fields, the signals, ``PATCH me/``."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts import signals
from accounts.models import SignedInDevice

from .fake_google import CLIENT_IDS, google_keys, id_token
from .test_api_auth import AuthApiTestCase, url

User = get_user_model()


class UserDefaultsTests(TestCase):
    def test_defaults(self):
        user = User.objects.create_user("a@example.com")
        self.assertEqual(user.timezone, "Asia/Kolkata")
        self.assertTrue(user.memory_enabled)
        self.assertFalse(user.memory_choice_explicit)
        self.assertEqual(user.app_open_count, 0)
        self.assertIsNone(user.memory_notice_seen_at_open)


@override_settings(GOOGLE_OAUTH_CLIENT_IDS=CLIENT_IDS)
class UserSignedInSignalTests(AuthApiTestCase):
    def capture(self):
        calls = []

        def receiver(sender, **kwargs):
            calls.append(kwargs)

        signals.user_signed_in.connect(receiver, weak=False)
        self.addCleanup(signals.user_signed_in.disconnect, receiver)
        return calls

    def test_sent_with_created_true_for_a_new_account_then_false(self):
        calls = self.capture()

        self.sign_in()
        self.sign_in()

        self.assertEqual([c["created"] for c in calls], [True, False])
        first = calls[0]
        self.assertEqual(first["user"], User.objects.get())
        self.assertEqual(first["device"], SignedInDevice.objects.order_by("id").first())
        self.assertIsNotNone(first["request"])

    def test_not_sent_when_sign_in_fails(self):
        calls = self.capture()
        with google_keys():
            response = self.post("auth-google", {"id_token": id_token(email_verified=False)})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(calls, [])

    def test_a_failing_receiver_does_not_break_sign_in(self):
        def boom(sender, **kwargs):
            raise RuntimeError("boom")

        signals.user_signed_in.connect(boom, weak=False)
        self.addCleanup(signals.user_signed_in.disconnect, boom)

        with self.assertLogs("accounts.signals", level="ERROR"):
            pair = self.sign_in()
        self.assertIn("access", pair)

    def test_app_opened_signal_reaches_its_receivers(self):
        receiver = mock.Mock()
        signals.app_opened.connect(receiver, weak=False)
        self.addCleanup(signals.app_opened.disconnect, receiver)
        signals.send(signals.app_opened, user=None, device=None, platform="web")
        receiver.assert_called_once()


@override_settings(GOOGLE_OAUTH_CLIENT_IDS=CLIENT_IDS)
class PatchMeTests(AuthApiTestCase):
    def setUp(self):
        super().setUp()
        pair = self.sign_in()
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pair['access']}")
        self.user = User.objects.get()

    def patch(self, data):
        return self.client.patch(url("me"), data, format="json")

    def test_me_shows_the_new_fields(self):
        body = self.client.get(url("me")).json()
        self.assertEqual(body["timezone"], "Asia/Kolkata")
        self.assertTrue(body["memory_enabled"])
        self.assertFalse(body["memory_choice_explicit"])

    def test_timezone_is_saved(self):
        response = self.patch({"timezone": "America/New_York"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["timezone"], "America/New_York")
        self.user.refresh_from_db()
        self.assertEqual(self.user.timezone, "America/New_York")
        self.assertFalse(self.user.memory_choice_explicit)

    def test_invalid_timezones_are_refused(self):
        for bad in ["Mars/Phobos", "", "../etc/passwd", "IST", "x" * 100, 5, None]:
            with self.subTest(bad=bad):
                self.assertEqual(self.patch({"timezone": bad}).status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.timezone, "Asia/Kolkata")

    def test_setting_memory_enabled_marks_the_choice_explicit(self):
        response = self.patch({"memory_enabled": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["memory_enabled"])
        self.assertTrue(response.json()["memory_choice_explicit"])
        self.user.refresh_from_db()
        self.assertFalse(self.user.memory_enabled)
        self.assertTrue(self.user.memory_choice_explicit)

    def test_choosing_the_default_value_still_counts_as_explicit(self):
        self.patch({"memory_enabled": True})
        self.user.refresh_from_db()
        self.assertTrue(self.user.memory_choice_explicit)

    def test_other_fields_cannot_be_set(self):
        response = self.patch(
            {"plan": "premium", "app_open_count": 9, "memory_choice_explicit": True, "name": "X"}
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.plan, "free")
        self.assertEqual(self.user.app_open_count, 0)
        self.assertFalse(self.user.memory_choice_explicit)
        self.assertEqual(self.user.name, "Alice Example")

    def test_requires_sign_in(self):
        response = APIClient().patch(url("me"), {"timezone": "UTC"}, format="json")
        self.assertEqual(response.status_code, 401)
