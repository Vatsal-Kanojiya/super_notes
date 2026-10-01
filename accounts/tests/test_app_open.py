"""Lifecycle part 2: app/version/, X-Client-Min-Version, session/open/, memory-notice/seen/."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from accounts import devices, lifecycle, signals
from accounts.api import DEVICE_CLAIM
from accounts.models import SignedInDevice

from .test_api_auth import url

User = get_user_model()

OLD = "202501010000-aaaaaaa"
MID = "202506010000-bbbbbbb"
NEW = "202510010000-ccccccc"


def sign_in_client(user):
    """A client holding an access token whose ``device`` claim names a real device row."""
    refresh = RefreshToken.for_user(user)
    device = devices.register(user, str(refresh["jti"]))
    refresh[DEVICE_CLAIM] = device.pk
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
    return client, device


class BuildCompareTests(TestCase):
    def test_compares_by_timestamp_prefix(self):
        self.assertTrue(lifecycle.is_older(OLD, NEW))
        self.assertFalse(lifecycle.is_older(NEW, OLD))
        self.assertFalse(lifecycle.is_older(NEW, NEW))
        # Same timestamp, different sha: not older.
        self.assertFalse(lifecycle.is_older("202501010000-aaa", "202501010000-zzz"))

    def test_unknown_or_empty_is_never_older(self):
        for build in ["", "dev", "2025", None]:
            self.assertFalse(lifecycle.is_older(build, NEW))
            self.assertFalse(lifecycle.is_older(OLD, build))


class AppVersionTests(TestCase):
    @override_settings(CLIENT_LATEST_VERSION=NEW, CLIENT_MIN_VERSION=MID)
    def test_public_and_returns_both(self):
        response = APIClient().get(url("app-version"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"latest": NEW, "min_supported": MID})

    @override_settings(CLIENT_LATEST_VERSION="", CLIENT_MIN_VERSION="")
    def test_empty_when_unset(self):
        self.assertEqual(
            APIClient().get(url("app-version")).json(), {"latest": "", "min_supported": ""}
        )

    def test_is_not_throttled_and_needs_no_auth(self):
        from config.api.views import AppVersionView

        self.assertEqual(AppVersionView.throttle_classes, [])
        self.assertEqual(AppVersionView.authentication_classes, [])


class MinVersionHeaderTests(TestCase):
    @override_settings(CLIENT_MIN_VERSION=MID)
    def test_every_api_response_carries_it(self):
        client = APIClient()
        self.assertEqual(client.get(url("health"))["X-Client-Min-Version"], MID)
        # An error response too.
        response = client.get(url("me"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["X-Client-Min-Version"], MID)

    @override_settings(CLIENT_MIN_VERSION="")
    def test_absent_when_unset(self):
        self.assertNotIn("X-Client-Min-Version", APIClient().get(url("health")))

    @override_settings(CLIENT_MIN_VERSION=MID)
    def test_not_added_outside_the_api(self):
        self.assertNotIn("X-Client-Min-Version", APIClient().get("/admin/login/"))

    @override_settings(CLIENT_MIN_VERSION=MID, CORS_ALLOWED_ORIGINS=["http://localhost:5173"])
    def test_cors_exposes_it(self):
        response = APIClient().get(url("health"), HTTP_ORIGIN="http://localhost:5173")
        exposed = response["Access-Control-Expose-Headers"].lower()
        self.assertIn("x-client-min-version", exposed)


@override_settings(
    CLIENT_LATEST_VERSION=NEW,
    CLIENT_MIN_VERSION=MID,
    MEMORY_NOTICE_EVERY_OPENS=5,
    APP_OPEN_MIN_INTERVAL_SECONDS=300,
)
class SessionOpenTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a@example.com")
        self.client, self.device = sign_in_client(self.user)

    def open(self, version=NEW, client=None, **extra):
        body = {"platform": "web", "app_version": version, "reason": "launch", **extra}
        return (client or self.client).post(url("session-open"), body, format="json")

    def make_open_countable(self):
        SignedInDevice.objects.filter(pk=self.device.pk).update(last_app_open_at=None)

    def open_n(self, n, version=NEW):
        """``n`` counted opens (the throttle window is cleared between them)."""
        for _ in range(n):
            self.make_open_countable()
            self.assertEqual(self.open(version).status_code, 200)

    def memory(self, response):
        return [n for n in response.json()["notices"] if n["kind"] == "memory"]

    def test_requires_sign_in(self):
        response = APIClient().post(url("session-open"), {}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_validates_the_body(self):
        for body in [
            {},
            {"platform": "ios", "app_version": NEW, "reason": "launch"},
            {"platform": "web", "app_version": NEW, "reason": "focus"},
            {"platform": "web", "reason": "launch"},
            {"platform": "web", "app_version": "x" * 65, "reason": "launch"},
        ]:
            with self.subTest(body=body):
                response = self.client.post(url("session-open"), body, format="json")
                self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.app_open_count, 0)

    def test_response_shape_and_count(self):
        response = self.open()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"notices", "server_time"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.app_open_count, 1)

    # --- update notice ---------------------------------------------------

    def test_current_build_gets_no_update_notice(self):
        kinds = [n["kind"] for n in self.open(NEW).json()["notices"]]
        self.assertNotIn("update", kinds)

    def test_older_than_latest_is_an_optional_update(self):
        # Between min (MID) and latest (NEW).
        build = "202507010000-ddddddd"
        notices = self.open(build).json()["notices"]
        self.assertIn({"kind": "update", "required": False}, notices)

    def test_older_than_min_is_a_required_update(self):
        notices = self.open(OLD).json()["notices"]
        self.assertIn({"kind": "update", "required": True}, notices)

    def test_unknown_build_gets_no_update_notice(self):
        kinds = [n["kind"] for n in self.open("dev").json()["notices"]]
        self.assertNotIn("update", kinds)

    @override_settings(CLIENT_LATEST_VERSION="", CLIENT_MIN_VERSION="")
    def test_nothing_configured_nothing_announced(self):
        kinds = [n["kind"] for n in self.open(OLD).json()["notices"]]
        self.assertNotIn("update", kinds)

    # --- memory notice: the three D88 states -------------------------------

    def test_on_never_chosen_is_prominent(self):
        self.assertEqual(
            self.memory(self.open()), [{"kind": "memory", "style": "prominent", "state": "on"}]
        )

    def test_on_and_chosen_is_subtle(self):
        User.objects.update(memory_enabled=True, memory_choice_explicit=True)
        self.assertEqual(
            self.memory(self.open()), [{"kind": "memory", "style": "subtle", "state": "on"}]
        )

    def test_off_is_subtle(self):
        User.objects.update(memory_enabled=False, memory_choice_explicit=True)
        self.assertEqual(
            self.memory(self.open()), [{"kind": "memory", "style": "subtle", "state": "off"}]
        )

    def test_choice_made_through_patch_me_quietens_the_notice(self):
        self.client.patch(url("me"), {"memory_enabled": True}, format="json")
        self.assertEqual(self.memory(self.open())[0]["style"], "subtle")

    # --- memory notice: cadence (D94) --------------------------------------

    def test_due_on_the_first_open_and_until_seen(self):
        self.assertTrue(self.memory(self.open()))
        # Not confirmed seen: still due on the next open.
        self.make_open_countable()
        self.assertTrue(self.memory(self.open()))

    def test_seen_silences_it_for_four_opens_then_it_is_due_again(self):
        self.assertTrue(self.memory(self.open()))  # open 1
        self.assertEqual(self.client.post(url("me-memory-notice-seen")).status_code, 204)
        self.user.refresh_from_db()
        self.assertEqual(self.user.memory_notice_seen_at_open, 1)

        for opens in (2, 3, 4, 5):
            self.make_open_countable()
            self.assertEqual(self.memory(self.open()), [], f"open {opens}")
        self.make_open_countable()
        self.assertTrue(self.memory(self.open()), "open 6")

    def test_every_n_follows_the_setting(self):
        with override_settings(MEMORY_NOTICE_EVERY_OPENS=2):
            self.open()
            self.client.post(url("me-memory-notice-seen"))
            self.make_open_countable()
            self.assertEqual(self.memory(self.open()), [])
            self.make_open_countable()
            self.assertTrue(self.memory(self.open()))

    def test_seen_needs_sign_in_and_only_moves_the_callers_row(self):
        self.assertEqual(APIClient().post(url("me-memory-notice-seen")).status_code, 401)
        other = User.objects.create_user("b@example.com", app_open_count=7)
        self.client.post(url("me-memory-notice-seen"))
        other.refresh_from_db()
        self.assertIsNone(other.memory_notice_seen_at_open)

    # --- throttle and the signal ------------------------------------------

    def test_repeat_from_one_device_is_not_counted_but_gets_notices(self):
        first = self.open()
        second = self.open()
        self.user.refresh_from_db()
        self.assertEqual(self.user.app_open_count, 1)
        self.assertEqual(second.json()["notices"], first.json()["notices"])

    def test_counted_again_after_the_window(self):
        self.open()
        SignedInDevice.objects.filter(pk=self.device.pk).update(
            last_app_open_at=timezone.now() - timedelta(seconds=301)
        )
        self.open()
        self.user.refresh_from_db()
        self.assertEqual(self.user.app_open_count, 2)

    def test_the_window_follows_the_setting(self):
        with override_settings(APP_OPEN_MIN_INTERVAL_SECONDS=0):
            self.open()
            self.open()
        self.user.refresh_from_db()
        self.assertEqual(self.user.app_open_count, 2)

    def test_two_devices_are_throttled_separately(self):
        self.open()
        client2, _device2 = sign_in_client(self.user)
        self.open(client=client2)
        self.user.refresh_from_db()
        self.assertEqual(self.user.app_open_count, 2)

    def test_app_opened_is_sent_once_per_counted_open(self):
        receiver = mock.Mock()
        signals.app_opened.connect(receiver, weak=False)
        self.addCleanup(signals.app_opened.disconnect, receiver)

        self.open(app_version=NEW, reason="resume")
        self.open()  # throttled

        receiver.assert_called_once()
        kwargs = receiver.call_args.kwargs
        self.assertEqual(kwargs["user"], self.user)
        self.assertEqual(kwargs["device"], self.device)
        self.assertEqual(kwargs["platform"], "web")
        self.assertEqual(kwargs["app_version"], NEW)
        self.assertEqual(kwargs["reason"], "resume")

    def test_last_seen_moves_even_when_throttled(self):
        self.open()
        old = timezone.now() - timedelta(hours=3)
        SignedInDevice.objects.filter(pk=self.device.pk).update(last_seen_at=old)
        self.open()  # throttled
        self.device.refresh_from_db()
        self.assertGreater(self.device.last_seen_at, old)

    # --- another user's device --------------------------------------------

    def test_another_users_device_cannot_be_used(self):
        victim = User.objects.create_user("v@example.com")
        _victim_client, victim_device = sign_in_client(victim)
        before = SignedInDevice.objects.get(pk=victim_device.pk)
        old_seen = timezone.now() - timedelta(days=1)
        SignedInDevice.objects.filter(pk=victim_device.pk).update(last_seen_at=old_seen)

        # The attacker's own valid token, but with the victim's device id in the claim.
        refresh = RefreshToken.for_user(self.user)
        refresh[DEVICE_CLAIM] = victim_device.pk
        attacker = APIClient()
        attacker.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

        receiver = mock.Mock()
        signals.app_opened.connect(receiver, weak=False)
        self.addCleanup(signals.app_opened.disconnect, receiver)

        self.assertEqual(self.open(client=attacker).status_code, 200)

        victim_device.refresh_from_db()
        self.assertEqual(victim_device.last_seen_at, old_seen)
        self.assertIsNone(victim_device.last_app_open_at)
        self.assertIsNone(receiver.call_args.kwargs["device"])
        victim.refresh_from_db()
        self.assertEqual(victim.app_open_count, 0)
        self.assertEqual(before.user_id, victim.pk)

    def test_a_token_with_no_device_claim_still_works_and_always_counts(self):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.user).access_token}"
        )
        self.open(client=client)
        self.open(client=client)
        self.user.refresh_from_db()
        self.assertEqual(self.user.app_open_count, 2)
