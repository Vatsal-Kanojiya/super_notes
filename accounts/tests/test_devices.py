"""The signed-in device limit and the devices endpoints (accounts/devices.py).

Each "device" is a separate sign-in through the real API, with its own
User-Agent, so every test exercises the path a client takes.
"""

import threading
import time
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from accounts import devices, tasks
from accounts.models import SecurityEvent, SignedInDevice

from .fake_google import CLIENT_IDS, google_keys, id_token

User = get_user_model()

PHONE = "SuperNotes/1.0 (Android 15)"
LAPTOP = "Mozilla/5.0 (X11; Linux x86_64) Firefox/140.0"
TABLET = "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X)"

BOB = {"sub": "222222222222222222222", "email": "bob@example.com"}


def url(name, **kwargs):
    return reverse(f"api:v1:{name}", kwargs=kwargs)


@override_settings(GOOGLE_OAUTH_CLIENT_IDS=CLIENT_IDS)
class DeviceTestCase(TestCase):
    def sign_in(self, ua=PHONE, **claims):
        """Sign in from a fresh client; returns the token pair."""
        with google_keys():
            response = APIClient().post(
                url("auth-google"),
                {"id_token": id_token(**claims)},
                format="json",
                HTTP_USER_AGENT=ua,
            )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def refresh(self, pair):
        return APIClient().post(url("auth-refresh"), {"refresh": pair["refresh"]}, format="json")

    def as_(self, pair):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {pair['access']}")
        return client

    def devices_of(self, email="alice@example.com"):
        return list(SignedInDevice.objects.filter(user__email=email))

    def age(self, device, minutes):
        """Make a device look last seen ``minutes`` ago."""
        SignedInDevice.objects.filter(pk=device.pk).update(
            last_seen_at=timezone.now() - timedelta(minutes=minutes)
        )

    def jti(self, pair):
        return str(RefreshToken(pair["refresh"], verify=False)["jti"])


class RegisteringTests(DeviceTestCase):
    def test_a_sign_in_registers_its_refresh_token_with_the_user_agent(self):
        pair = self.sign_in(ua=PHONE)

        (device,) = self.devices_of()
        self.assertEqual(device.refresh_jti, self.jti(pair))
        self.assertEqual(device.label, PHONE)
        self.assertEqual(str(device), f"Device {device.pk} for user {device.user_id}")

    def test_the_label_is_cut_to_fit_and_may_be_empty(self):
        self.sign_in(ua="x" * 500)
        self.assertEqual(self.devices_of()[0].label, "x" * 200)
        self.assertEqual(devices.label_for(None), "")

    def test_both_tokens_carry_the_device_id(self):
        pair = self.sign_in()
        (device,) = self.devices_of()

        self.assertEqual(RefreshToken(pair["refresh"])["device"], device.pk)
        self.assertEqual(self.as_(pair).get(url("auth-devices")).json()[0]["current"], True)


class LimitTests(DeviceTestCase):
    def test_a_third_sign_in_ends_the_oldest_and_its_refresh_gets_401(self):
        first = self.sign_in(ua=PHONE)
        second = self.sign_in(ua=LAPTOP)
        third = self.sign_in(ua=TABLET)

        self.assertEqual(self.refresh(first).status_code, 401)
        self.assertEqual(self.refresh(second).status_code, 200)
        self.assertEqual(self.refresh(third).status_code, 200)
        self.assertEqual(sorted(d.label for d in self.devices_of()), sorted([LAPTOP, TABLET]))

    def test_ending_a_device_is_recorded_with_why(self):
        self.sign_in(ua=PHONE)
        (phone,) = self.devices_of()
        self.sign_in(ua=LAPTOP)
        self.sign_in(ua=TABLET)

        event = SecurityEvent.objects.get(event="device_signed_out")
        self.assertEqual(event.email, "alice@example.com")
        self.assertEqual(event.detail, {"device": phone.pk, "label": PHONE, "reason": "limit"})

    def test_the_oldest_is_the_least_recently_seen_not_the_first_signed_in(self):
        first = self.sign_in(ua=PHONE)
        second = self.sign_in(ua=LAPTOP)
        self.age(self.devices_of()[0], 60)
        first = self.refresh(first).json()  # the phone was used again just now
        self.age(next(d for d in self.devices_of() if d.label == LAPTOP), 30)

        self.sign_in(ua=TABLET)

        self.assertEqual(self.refresh(first).status_code, 200)
        self.assertEqual(self.refresh(second).status_code, 401)

    def test_the_ended_devices_access_token_works_until_it_expires(self):
        first = self.sign_in()
        self.sign_in()
        self.sign_in()

        self.assertEqual(self.as_(first).get(url("me")).status_code, 200)

    def test_one_accounts_limit_does_not_touch_another(self):
        bob = self.sign_in(**BOB)
        for _ in range(3):
            self.sign_in()

        self.assertEqual(self.refresh(bob).status_code, 200)
        self.assertEqual(len(self.devices_of()), 2)

    @override_settings(MAX_SIGNED_IN_DEVICES=3)
    def test_the_limit_is_a_setting(self):
        pairs = [self.sign_in() for _ in range(3)]
        self.assertTrue(all(self.refresh(p).status_code == 200 for p in pairs))

    @override_settings(MAX_SIGNED_IN_DEVICES=0)
    def test_a_limit_below_one_is_treated_as_one(self):
        first = self.sign_in()
        second = self.sign_in()

        self.assertEqual(self.refresh(first).status_code, 401)
        self.assertEqual(self.refresh(second).status_code, 200)


class DeadRowTests(DeviceTestCase):
    def test_a_logged_out_device_leaves_room_and_ends_nobody(self):
        first = self.sign_in()
        second = self.sign_in()
        APIClient().post(url("auth-logout"), {"refresh": second["refresh"]}, format="json")
        self.sign_in()

        self.assertEqual(self.refresh(first).status_code, 200)
        self.assertFalse(SecurityEvent.objects.filter(event="device_signed_out").exists())

    def test_an_expired_token_does_not_push_out_a_live_device(self):
        first = self.sign_in()
        second = self.sign_in()
        OutstandingToken.objects.filter(jti=self.jti(second)).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.sign_in()

        self.assertEqual(self.refresh(first).status_code, 200)

    def test_a_blacklisted_or_flushed_token_does_not_count(self):
        first = self.sign_in()
        second = self.sign_in()
        BlacklistedToken.objects.create(token=OutstandingToken.objects.get(jti=self.jti(second)))
        self.sign_in()
        OutstandingToken.objects.filter(jti=self.jti(first)).delete()

        self.assertEqual(len(devices.live_devices(User.objects.get())), 1)


class RefreshTests(DeviceTestCase):
    def test_a_refresh_moves_the_row_to_the_new_token(self):
        pair = self.sign_in()
        (before,) = self.devices_of()
        self.age(before, 30)

        new = self.refresh(pair).json()

        (after,) = self.devices_of()
        self.assertEqual(after.pk, before.pk)
        self.assertEqual(after.refresh_jti, self.jti(new))
        self.assertGreater(after.last_seen_at, timezone.now() - timedelta(minutes=1))
        self.assertEqual(after.created_at, before.created_at)

    def test_a_chain_of_refreshes_stays_one_device_and_keeps_its_id_claim(self):
        pair = self.sign_in()
        (device,) = self.devices_of()
        for _ in range(3):
            pair = self.refresh(pair).json()

        self.assertEqual(len(self.devices_of()), 1)
        self.assertEqual(RefreshToken(pair["refresh"])["device"], device.pk)

    def test_a_bad_refresh_changes_nothing(self):
        self.sign_in()
        for body in ({"refresh": "junk"}, {"refresh": 5}, {}):
            APIClient().post(url("auth-refresh"), body, format="json")
        self.assertEqual(len(self.devices_of()), 1)

    def test_a_token_issued_outside_sign_in_is_registered_when_used(self):
        self.sign_in()
        self.sign_in()
        legacy = RefreshToken.for_user(User.objects.get())

        response = APIClient().post(url("auth-refresh"), {"refresh": str(legacy)}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.devices_of()), 2)
        self.assertIn(self.jti(response.json()), [d.refresh_jti for d in self.devices_of()])


class DevicesEndpointTests(DeviceTestCase):
    def test_it_needs_a_signed_in_user(self):
        self.assertEqual(APIClient().get(url("auth-devices")).status_code, 401)
        self.assertEqual(APIClient().delete(url("auth-device", pk=1)).status_code, 401)

    def test_it_lists_the_documented_fields_most_recent_first_and_marks_this_one(self):
        self.sign_in(ua=PHONE)
        laptop = self.sign_in(ua=LAPTOP)
        self.age(next(d for d in self.devices_of() if d.label == PHONE), 10)

        body = self.as_(laptop).get(url("auth-devices")).json()

        self.assertEqual([d["label"] for d in body], [LAPTOP, PHONE])
        self.assertEqual([d["current"] for d in body], [True, False])
        self.assertEqual(set(body[0]), {"id", "label", "created_at", "last_seen_at", "current"})
        self.assertNotIn(self.jti(laptop), str(body))

    def test_it_lists_only_your_own_devices(self):
        self.sign_in(**BOB)
        alice = self.sign_in()

        body = self.as_(alice).get(url("auth-devices")).json()

        self.assertEqual([d["id"] for d in body], [d.pk for d in self.devices_of()])

    def test_signing_a_device_out_revokes_its_refresh_token(self):
        phone = self.sign_in(ua=PHONE)
        laptop = self.sign_in(ua=LAPTOP)
        phone_device = next(d for d in self.devices_of() if d.label == PHONE)

        response = self.as_(laptop).delete(url("auth-device", pk=phone_device.pk))

        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.refresh(phone).status_code, 401)
        self.assertEqual(self.refresh(laptop).status_code, 200)
        event = SecurityEvent.objects.get(event="device_signed_out")
        self.assertEqual(event.detail["reason"], "user")

    def test_one_user_cannot_see_or_sign_out_another_users_device(self):
        bob = self.sign_in(**BOB)
        alice = self.sign_in()
        (bobs_device,) = self.devices_of("bob@example.com")

        response = self.as_(alice).delete(url("auth-device", pk=bobs_device.pk))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")
        self.assertEqual(self.refresh(bob).status_code, 200)

    def test_an_unknown_id_is_404(self):
        alice = self.sign_in()
        self.assertEqual(self.as_(alice).delete(url("auth-device", pk=999999)).status_code, 404)


class FlushExpiredTokensTests(DeviceTestCase):
    def test_the_beat_task_flushes_expired_tokens_and_is_scheduled(self):
        from django.conf import settings

        live = self.sign_in()
        stale = RefreshToken.for_user(User.objects.get())
        OutstandingToken.objects.filter(jti=str(stale["jti"])).update(
            expires_at=timezone.now() - timedelta(days=1)
        )

        with (
            mock.patch("sys.stdout", new_callable=StringIO),
            self.assertLogs("celery.app.trace", level="INFO"),
        ):
            tasks.flush_expired_tokens.delay()

        self.assertEqual(
            list(OutstandingToken.objects.values_list("jti", flat=True)), [self.jti(live)]
        )
        entry = settings.CELERY_BEAT_SCHEDULE["flush-expired-tokens"]
        self.assertEqual(entry["task"], "accounts.tasks.flush_expired_tokens")


@override_settings(GOOGLE_OAUTH_CLIENT_IDS=CLIENT_IDS)
class ConcurrentRefreshTests(TransactionTestCase):
    """Two refreshes racing with one token: exactly one may win (D20).

    Real threads on real connections, as in the reference's concurrency
    tests. simplejwt's blacklist write is slowed down so that, without the
    row lock in RefreshView, both requests would pass the blacklist check
    before either wrote to it.
    """

    def test_only_one_of_two_racing_refreshes_gets_a_new_pair(self):
        user = User.objects.create_user("alice@example.com")
        refresh = str(RefreshToken.for_user(user))
        original_blacklist = RefreshToken.blacklist
        start = threading.Barrier(2)
        statuses = []

        def slow_blacklist(token):
            time.sleep(0.3)
            return original_blacklist(token)

        def attempt():
            try:
                start.wait()
                response = APIClient().post(
                    url("auth-refresh"), {"refresh": refresh}, format="json"
                )
                statuses.append(response.status_code)
            finally:
                connection.close()

        with mock.patch.object(RefreshToken, "blacklist", slow_blacklist):
            threads = [threading.Thread(target=attempt) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(sorted(statuses), [200, 401])
        self.assertEqual(SignedInDevice.objects.count(), 1)
