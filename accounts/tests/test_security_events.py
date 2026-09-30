"""The security event trail (accounts/audit.py) and its retention purge.

The events each flow records are asserted beside the flows themselves
(test_google_login.py, test_tokens.py, test_devices.py); this file is about
the trail's own guarantees: it never raises, it never stores what it
should not, and it forgets on schedule.
"""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import transaction
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from accounts import audit, tasks
from accounts.models import SecurityEvent

User = get_user_model()


class RecordTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_it_records_who_where_and_which_request(self):
        user = User.objects.create_user("alice@example.com")
        request = self.factory.get("/", REMOTE_ADDR="203.0.113.7")

        with mock.patch("config.middleware.get_request_id", return_value="req-123"):
            audit.record("logged_out", request=request, user=user, reason="user")

        row = SecurityEvent.objects.get()
        self.assertEqual(row.event, "logged_out")
        self.assertEqual(row.user, user)
        self.assertEqual(row.email, "alice@example.com")
        self.assertEqual(row.ip, "203.0.113.7")
        self.assertEqual(row.request_id, "req-123")
        self.assertEqual(row.detail, {"reason": "user"})
        self.assertIn("Logged out", str(row))
        self.assertIn("alice@example.com", str(row))

    def test_an_event_with_no_request_and_no_user_is_still_recorded(self):
        audit.record("google_login_failed")

        row = SecurityEvent.objects.get()
        self.assertIsNone(row.ip)
        self.assertEqual(row.email, "")
        self.assertEqual(row.request_id, "")
        self.assertIn("unknown", str(row))

    def test_an_explicit_email_is_kept_as_a_snapshot(self):
        audit.record("google_login_failed", email="Someone@Example.com")
        self.assertEqual(SecurityEvent.objects.get().email, "Someone@Example.com")

    def test_str_falls_back_to_the_user_id(self):
        user = User.objects.create_user("alice@example.com")
        audit.record("signed_up", user=user)
        SecurityEvent.objects.update(email="")
        self.assertIn(f"user {user.pk}", str(SecurityEvent.objects.get()))

    def test_a_request_without_a_usable_address_still_records_the_event(self):
        request = self.factory.get("/")
        del request.META["REMOTE_ADDR"]
        audit.record("google_login_failed", request=request)

        self.assertIsNone(SecurityEvent.objects.get().ip)

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_a_forged_forwarded_address_is_dropped_not_stored(self):
        request = self.factory.get("/", HTTP_X_FORWARDED_FOR="not-an-address")
        audit.record("google_login_failed", request=request)

        self.assertIsNone(SecurityEvent.objects.get().ip)


class RecordNeverRaisesTests(TestCase):
    def test_a_database_failure_is_logged_not_raised(self):
        with mock.patch(
            "accounts.audit.SecurityEvent.objects.create", side_effect=RuntimeError("boom")
        ):
            with self.assertLogs("accounts.audit", level="WARNING") as logs:
                audit.record("google_login_failed")

        self.assertIn("google_login_failed", logs.output[0])
        self.assertEqual(SecurityEvent.objects.count(), 0)

    def test_a_failure_leaves_the_surrounding_transaction_usable(self):
        # On Postgres a failed statement aborts the whole transaction unless
        # it is rolled back to a savepoint. An event value too long for its
        # column is a real database error, not a mock.
        user = User.objects.create_user("alice@example.com")
        with transaction.atomic(), self.assertLogs("accounts.audit", level="WARNING"):
            audit.record("x" * 100, user=user)
            # Would raise "current transaction is aborted" without the savepoint.
            self.assertEqual(User.objects.filter(pk=user.pk).count(), 1)

        self.assertEqual(SecurityEvent.objects.count(), 0)

    def test_a_broken_user_object_does_not_raise(self):
        broken = mock.Mock(get_username=mock.Mock(side_effect=RuntimeError("boom")))
        with self.assertLogs("accounts.audit", level="WARNING"):
            audit.record("signed_up", user=broken)


class AdminTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin@example.com", "a-long-admin-password")
        self.client.force_login(self.admin)
        audit.record("signed_up", user=self.admin)

    def test_the_changelist_renders(self):
        response = self.client.get("/admin/accounts/securityevent/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "admin@example.com")

    def test_the_trail_cannot_be_added_to_edited_or_deleted_from_the_admin(self):
        row = SecurityEvent.objects.get()
        self.assertEqual(self.client.get("/admin/accounts/securityevent/add/").status_code, 403)
        response = self.client.post(
            f"/admin/accounts/securityevent/{row.pk}/change/", {"event": "logged_out"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            self.client.post(f"/admin/accounts/securityevent/{row.pk}/delete/").status_code, 403
        )
        self.assertEqual(SecurityEvent.objects.get().event, "signed_up")


class PurgeSecurityEventsTests(TestCase):
    def make(self, days_ago):
        audit.record("google_login_failed")
        row = SecurityEvent.objects.order_by("-id").first()
        SecurityEvent.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - timedelta(days=days_ago)
        )
        return row

    def test_it_removes_only_rows_older_than_the_retention_period(self):
        old = self.make(400)
        recent = self.make(10)
        out = StringIO()

        call_command("purge_security_events", stdout=out)

        self.assertEqual(list(SecurityEvent.objects.values_list("pk", flat=True)), [recent.pk])
        self.assertFalse(SecurityEvent.objects.filter(pk=old.pk).exists())
        self.assertIn("Removed 1", out.getvalue())

    def test_days_overrides_the_setting(self):
        self.make(10)
        call_command("purge_security_events", days=5, stdout=StringIO())
        self.assertEqual(SecurityEvent.objects.count(), 0)

    def test_dry_run_removes_nothing(self):
        self.make(400)
        out = StringIO()

        call_command("purge_security_events", dry_run=True, stdout=out)

        self.assertEqual(SecurityEvent.objects.count(), 1)
        self.assertIn("1 security event(s)", out.getvalue())

    def test_the_beat_task_runs_the_command_with_its_default(self):
        self.make(400)
        self.make(10)

        with (
            self.settings(SECURITY_EVENT_RETENTION_DAYS=365),
            mock.patch("sys.stdout", new_callable=StringIO),
        ):
            tasks.purge_security_events.delay()

        self.assertEqual(SecurityEvent.objects.count(), 1)

    def test_it_is_scheduled_daily(self):
        from django.conf import settings

        entry = settings.CELERY_BEAT_SCHEDULE["purge-old-security-events"]
        self.assertEqual(entry["task"], "accounts.tasks.purge_security_events")
        self.assertEqual(entry["schedule"], 24 * 60 * 60)
