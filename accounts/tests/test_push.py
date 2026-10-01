"""Web push: the subscription endpoints and the reminder push channel.

``accounts.push.webpush`` is mocked: no network, and the tests read what
would have been sent.
"""

import json
from datetime import datetime
from datetime import timezone as dt_timezone
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from pywebpush import WebPushException
from rest_framework.test import APIClient

from accounts.models import PushSubscription
from notes import delivery, services
from notes.models import Reminder, ReminderDelivery
from notes.tests.helpers import doc, make_user

KEYS = {"VAPID_PUBLIC_KEY": "pub", "VAPID_PRIVATE_KEY": "priv", "VAPID_SUBJECT": "mailto:a@b.c"}
SECRET = "the secret body"


# Real-looking keys: 65 and 16 bytes of base64url.
P256DH = "B" + "A" * 86
AUTH = "A" * 22


def url(name):
    return reverse(f"api:v1:{name}")


def sub_body(endpoint="https://fcm.googleapis.com/abc"):
    return {"endpoint": endpoint, "p256dh": P256DH, "auth": AUTH}


def gone(status):
    return WebPushException("gone", response=mock.Mock(status_code=status))


@override_settings(**KEYS)
class EndpointTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        self.client_a = APIClient()
        self.client_a.force_authenticate(self.alice)
        self.client_b = APIClient()
        self.client_b.force_authenticate(self.bob)

    def test_vapid_key(self):
        response = self.client_a.get(url("push-vapid-key"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"public_key": "pub"})

    def test_requires_sign_in(self):
        self.assertEqual(APIClient().get(url("push-vapid-key")).status_code, 401)
        self.assertEqual(
            APIClient().post(url("me-push-subscriptions"), sub_body(), format="json").status_code,
            401,
        )

    @override_settings(VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY="")
    def test_push_off_is_404(self):
        self.assertEqual(self.client_a.get(url("push-vapid-key")).status_code, 404)
        response = self.client_a.post(url("me-push-subscriptions"), sub_body(), format="json")
        self.assertEqual(response.status_code, 404)

    def test_register_is_an_upsert_by_endpoint(self):
        for _ in range(2):
            response = self.client_a.post(
                url("me-push-subscriptions"), sub_body(), format="json", HTTP_USER_AGENT="UA/1"
            )
            self.assertEqual(response.status_code, 204)
        self.assertEqual(PushSubscription.objects.count(), 1)
        sub = PushSubscription.objects.get()
        self.assertEqual(
            (sub.user, sub.p256dh, sub.auth, sub.user_agent), (self.alice, P256DH, AUTH, "UA/1")
        )

        body = {**sub_body(), "p256dh": "C" + "A" * 86}
        self.client_a.post(url("me-push-subscriptions"), body, format="json")
        self.assertEqual(PushSubscription.objects.get().p256dh, "C" + "A" * 86)

    def test_an_endpoint_registered_to_another_user_moves_with_the_same_keys(self):
        self.client_a.post(url("me-push-subscriptions"), sub_body(), format="json")
        response = self.client_b.post(url("me-push-subscriptions"), sub_body(), format="json")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(PushSubscription.objects.get().user, self.bob)

    def test_another_users_endpoint_with_other_keys_is_409_and_unchanged(self):
        self.client_a.post(url("me-push-subscriptions"), sub_body(), format="json")
        for body in (
            {**sub_body(), "p256dh": "C" + "A" * 86},
            {**sub_body(), "auth": "B" * 22},
        ):
            with self.subTest(body=body):
                response = self.client_b.post(url("me-push-subscriptions"), body, format="json")
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["code"], "endpoint_in_use")
                sub = PushSubscription.objects.get()
                self.assertEqual((sub.user, sub.p256dh, sub.auth), (self.alice, P256DH, AUTH))

    def test_unsafe_endpoints_are_refused(self):
        cases = {
            "http": "http://fcm.googleapis.com/x",
            "unknown host": "https://evil.example/x",
            "lookalike host": "https://fcm.googleapis.com.evil.example/x",
            "suffix without dot": "https://evilpush.apple.com.example/x",
            "bare wildcard suffix": "https://push.apple.com/x",
            "ipv4": "https://127.0.0.1/x",
            "ipv6": "https://[::1]/x",
            "port": "https://fcm.googleapis.com:8443/x",
            "userinfo": "https://user:pw@fcm.googleapis.com/x",
            "userinfo trick": "https://evil.example\\@fcm.googleapis.com/x",
            "too long": "https://fcm.googleapis.com/" + "a" * 1000,
            "empty": "",
        }
        for name, endpoint in cases.items():
            response = self.client_a.post(
                url("me-push-subscriptions"), {**sub_body(), "endpoint": endpoint}, format="json"
            )
            self.assertEqual(response.status_code, 400, name)
            self.assertEqual(response.json()["code"], "invalid_endpoint", name)
        self.assertEqual(PushSubscription.objects.count(), 0)

    def test_allowed_hosts_and_explicit_443(self):
        for endpoint in (
            "https://fcm.googleapis.com:443/fcm/send/abc",
            "https://updates.push.services.mozilla.com/wpush/v2/abc",
            "https://eu.push.services.mozilla.com/x",
            "https://db5p.notify.windows.com/x",
            "https://web.push.apple.com/x",
            "https://FCM.googleapis.com/y",
        ):
            response = self.client_a.post(
                url("me-push-subscriptions"), {**sub_body(), "endpoint": endpoint}, format="json"
            )
            self.assertEqual(response.status_code, 204, endpoint)

    def test_bad_keys_are_400(self):
        for field, value in (
            ("p256dh", "short"),
            ("p256dh", "!" * 87),
            ("p256dh", "A" * 22),  # right alphabet, 16 bytes
            ("auth", "A" * 200),
            ("auth", "not base64url!"),
            ("auth", "AAAA"),
        ):
            response = self.client_a.post(
                url("me-push-subscriptions"), {**sub_body(), field: value}, format="json"
            )
            self.assertEqual(response.status_code, 400, (field, value))
        self.assertEqual(PushSubscription.objects.count(), 0)

    def test_invalid_subscription_is_400(self):
        response = self.client_a.post(
            url("me-push-subscriptions"), {"endpoint": "nope"}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_delete_removes_only_the_callers_subscription(self):
        self.client_a.post(
            url("me-push-subscriptions"), sub_body("https://fcm.googleapis.com/a"), format="json"
        )
        self.client_b.post(
            url("me-push-subscriptions"), sub_body("https://fcm.googleapis.com/b"), format="json"
        )

        # Bob cannot remove Alice's endpoint.
        response = self.client_b.delete(
            url("me-push-subscriptions"),
            {"endpoint": "https://fcm.googleapis.com/a"},
            format="json",
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(PushSubscription.objects.count(), 2)

        self.client_a.delete(
            url("me-push-subscriptions"),
            {"endpoint": "https://fcm.googleapis.com/a"},
            format="json",
        )
        self.assertEqual(
            list(PushSubscription.objects.values_list("endpoint", flat=True)),
            ["https://fcm.googleapis.com/b"],
        )

    def test_delete_works_with_push_off(self):
        self.client_a.post(url("me-push-subscriptions"), sub_body(), format="json")
        with override_settings(VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY=""):
            response = self.client_a.delete(url("me-push-subscriptions"), sub_body(), format="json")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(PushSubscription.objects.count(), 0)


@override_settings(**KEYS)
class PushChannelTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        note = services.create_note(self.alice, title="Passport", content=doc(SECRET))
        due = datetime(2026, 10, 27, 9, 0, tzinfo=dt_timezone.utc)
        self.reminder = services.create_reminder(self.alice, note.pk, due_at=due, channels=["push"])
        self.at = due

    def subscribe(self, endpoint="https://fcm.googleapis.com/a"):
        return PushSubscription.objects.create(
            user=self.alice, endpoint=endpoint, p256dh=P256DH, auth=AUTH
        )

    def deliver(self):
        d = ReminderDelivery.objects.create(reminder=self.reminder, occurrence_at=self.at)
        delivery.send(d.pk)
        d.refresh_from_db()
        return d.channel_results

    def test_payload_has_ids_and_title_but_no_note_content(self):
        sub = self.subscribe()
        with mock.patch("accounts.push.webpush") as wp:
            results = self.deliver()
        self.assertEqual(results, {"push": "sent"})
        kwargs = wp.call_args.kwargs
        self.assertEqual(kwargs["subscription_info"]["endpoint"], sub.endpoint)
        self.assertEqual(kwargs["vapid_claims"], {"sub": "mailto:a@b.c"})
        payload = json.loads(kwargs["data"])
        self.assertEqual(
            payload,
            {
                "type": "reminder",
                "reminder_id": self.reminder.pk,
                "note_id": self.reminder.note_id,
                "title": "Passport",
            },
        )
        self.assertNotIn(SECRET, kwargs["data"])
        sub.refresh_from_db()
        self.assertIsNotNone(sub.last_success_at)

    def test_the_push_service_call_has_a_timeout(self):
        self.subscribe()
        with mock.patch("accounts.push.webpush") as wp:
            self.deliver()
        self.assertEqual(wp.call_args.kwargs["timeout"], 10)

    def test_sent_to_every_subscription_of_the_owner_only(self):
        self.subscribe("https://fcm.googleapis.com/a")
        self.subscribe("https://fcm.googleapis.com/b")
        PushSubscription.objects.create(
            user=make_user("bob"), endpoint="https://fcm.googleapis.com/c", p256dh=P256DH, auth=AUTH
        )
        with mock.patch("accounts.push.webpush") as wp:
            self.deliver()
        endpoints = {c.kwargs["subscription_info"]["endpoint"] for c in wp.call_args_list}
        self.assertEqual(
            endpoints, {"https://fcm.googleapis.com/a", "https://fcm.googleapis.com/b"}
        )

    def test_404_and_410_delete_the_subscription(self):
        for status in (404, 410):
            PushSubscription.objects.all().delete()
            ReminderDelivery.objects.all().delete()
            self.subscribe()
            with mock.patch("accounts.push.webpush", side_effect=gone(status)):
                results = self.deliver()
            self.assertEqual(PushSubscription.objects.count(), 0, status)
            self.assertNotEqual(results["push"], "failed")

    def test_other_errors_are_recorded_and_keep_the_subscription(self):
        self.subscribe()
        with mock.patch("accounts.push.webpush", side_effect=gone(500)):
            results = self.deliver()
        self.assertEqual(results, {"push": "failed"})
        self.assertEqual(PushSubscription.objects.count(), 1)

    def test_one_failure_does_not_stop_the_others(self):
        self.subscribe("https://fcm.googleapis.com/a")
        self.subscribe("https://fcm.googleapis.com/b")
        with mock.patch("accounts.push.webpush", side_effect=[gone(500), None]) as wp:
            results = self.deliver()
        self.assertEqual(wp.call_count, 2)
        self.assertEqual(results, {"push": "partial"})

    def test_a_subscription_with_a_disallowed_endpoint_is_deleted_unsent(self):
        # A row from before the check existed.
        self.subscribe("https://169.254.169.254/latest")
        with mock.patch("accounts.push.webpush") as wp:
            self.deliver()
        wp.assert_not_called()
        self.assertEqual(PushSubscription.objects.count(), 0)

    def test_no_subscriptions(self):
        with mock.patch("accounts.push.webpush") as wp:
            self.assertEqual(self.deliver(), {"push": "no_subscriptions"})
        wp.assert_not_called()

    @override_settings(VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY="")
    def test_push_off_is_unavailable_and_sends_nothing(self):
        self.subscribe()
        with mock.patch("accounts.push.webpush") as wp:
            self.assertEqual(self.deliver(), {"push": "unavailable"})
        wp.assert_not_called()
        self.assertEqual(Reminder.objects.get().status, Reminder.Status.SCHEDULED)


class GenerateKeysTests(TestCase):
    def test_prints_a_usable_pair(self):
        from py_vapid import Vapid

        out = StringIO()
        call_command("generate_vapid_keys", stdout=out)
        lines = dict(line.split("=", 1) for line in out.getvalue().splitlines())
        vapid = Vapid.from_string(lines["VAPID_PRIVATE_KEY"])
        self.assertTrue(vapid.public_key)
        self.assertEqual(len(lines["VAPID_PUBLIC_KEY"]), 87)
