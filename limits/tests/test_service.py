from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from assistant.models import AskQuery
from limits import service
from limits.models import Limit, UsageEvent
from limits.service import SystemLimitExceeded, UserLimitExceeded
from notes.tests.helpers import make_user

from .helpers import TEST_LIMITS, QuietSystemLimitLog, consume_as

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


def at(*args):
    """Freeze the service's clock at a UTC instant."""
    return mock.patch("django.utils.timezone.now", return_value=datetime(*args, tzinfo=UTC))


@override_settings(LIMIT_DEFAULTS=TEST_LIMITS)
class PerUserTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")

    def test_free_plan_limit_then_refused(self):
        consume_as(self.alice, "chat_turns")
        consume_as(self.alice, "chat_turns")

        with self.assertRaises(UserLimitExceeded) as caught:
            consume_as(self.alice, "chat_turns")

        self.assertEqual((caught.exception.used, caught.exception.limit), (2, 2))
        self.assertEqual(UsageEvent.objects.filter(user=self.alice).count(), 2)

    def test_premium_plan_has_its_own_limit(self):
        self.alice.plan = "premium"
        self.alice.save()
        for _ in range(5):
            consume_as(self.alice, "chat_turns")

        with self.assertRaises(UserLimitExceeded) as caught:
            consume_as(self.alice, "chat_turns")
        self.assertEqual(caught.exception.limit, 5)

    def test_one_users_use_does_not_count_for_another(self):
        bob = make_user("bob")
        consume_as(bob, "chat_turns")
        consume_as(bob, "chat_turns")

        consume_as(self.alice, "chat_turns")
        self.assertEqual(service.usage(self.alice, "chat_turns")["used"], 1)

    def test_keys_are_counted_separately(self):
        consume_as(self.alice, "chat_turns")
        consume_as(self.alice, "chat_turns")

        consume_as(self.alice, "daily")  # unaffected by chat_turns being full

    def test_a_row_overrides_every_default(self):
        Limit.objects.create(key="chat_turns", user_free=1, user_premium=1, period="day")

        consume_as(self.alice, "chat_turns")
        with self.assertRaises(UserLimitExceeded) as caught:
            consume_as(self.alice, "chat_turns")

        self.assertEqual(caught.exception.limit, 1)
        # Its period too: the day ends before the month does.
        tomorrow = (datetime.now(IST) + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        self.assertEqual(caught.exception.resets_at, tomorrow)

    def test_a_disabled_limit_is_not_enforced_but_still_recorded(self):
        Limit.objects.create(
            key="chat_turns", user_free=1, user_premium=1, system=1, period="month", enabled=False
        )
        for _ in range(3):
            consume_as(self.alice, "chat_turns")

        self.assertEqual(service.usage(self.alice, "chat_turns")["used"], 3)
        self.assertIsNone(service.usage(self.alice, "chat_turns")["limit"])
        self.assertIsNone(service.system_usage("chat_turns")["limit"])

    def test_amounts_add_up(self):
        consume_as(self.alice, "bytes", 60)
        with self.assertRaises(UserLimitExceeded) as caught:
            consume_as(self.alice, "bytes", 41)
        self.assertEqual(caught.exception.used, 60)

        consume_as(self.alice, "bytes", 40)  # exactly the limit fits
        self.assertEqual(service.usage(self.alice, "bytes")["used"], 100)

    def test_unlimited_key_takes_no_system_lock(self):
        with CaptureQueriesContext(connection) as queries:
            for _ in range(50):
                consume_as(self.alice, "open")

        self.assertEqual(service.usage(self.alice, "open")["used"], 50)
        self.assertFalse(any("advisory" in q["sql"] for q in queries.captured_queries))

    def test_meta_is_stored_on_the_event(self):
        ask = AskQuery.objects.create(user=self.alice, question="q", idempotency_key="k")

        event = consume_as(
            self.alice, "chat_turns", ask=ask, provider="claude", model="m", input_tokens=10
        )

        event.refresh_from_db()
        self.assertEqual((event.ask, event.provider, event.model), (ask, "claude", "m"))
        self.assertEqual(event.input_tokens, 10)
        self.assertIsNone(event.output_tokens)

    def test_bad_arguments_are_refused(self):
        with self.assertRaises(KeyError):
            consume_as(self.alice, "chat_turn")  # a typo fails loudly
        with self.assertRaises(TypeError):
            consume_as(self.alice, "chat_turns", note=1)
        with self.assertRaises(ValueError):
            consume_as(self.alice, "chat_turns", 0)
        self.assertFalse(UsageEvent.objects.exists())

    def test_a_row_for_an_unknown_key_does_not_validate(self):
        with self.assertRaises(ValidationError):
            Limit(key="chat_turn", period="month").full_clean()
        Limit(key="chat_turns", period="month").full_clean()


@override_settings(LIMIT_DEFAULTS=TEST_LIMITS)
class PeriodTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")

    def test_the_month_is_the_local_calendar_month(self):
        # 2026-10-31 23:59:59 in Kolkata.
        with at(2026, 10, 31, 18, 29, 59):
            consume_as(self.alice, "chat_turns")
            consume_as(self.alice, "chat_turns")
            with self.assertRaises(UserLimitExceeded) as caught:
                consume_as(self.alice, "chat_turns")
        self.assertEqual(caught.exception.resets_at, datetime(2026, 11, 1, tzinfo=IST))

        # One second later it is November there, while still October in UTC.
        with at(2026, 10, 31, 18, 30, 0):
            consume_as(self.alice, "chat_turns")
            self.assertEqual(service.usage(self.alice, "chat_turns")["used"], 1)

    def test_the_day_is_the_local_calendar_day(self):
        with at(2026, 3, 1, 18, 29, 59):
            consume_as(self.alice, "daily")
            with self.assertRaises(UserLimitExceeded) as caught:
                consume_as(self.alice, "daily")
        self.assertEqual(caught.exception.resets_at, datetime(2026, 3, 2, tzinfo=IST))

        with at(2026, 3, 1, 18, 30, 0):
            consume_as(self.alice, "daily")

    def test_month_bounds_through_december_and_february(self):
        start, end = service.period_bounds("month", datetime(2026, 12, 15, tzinfo=IST))
        self.assertEqual(
            (start, end), (datetime(2026, 12, 1, tzinfo=IST), datetime(2027, 1, 1, tzinfo=IST))
        )
        start, end = service.period_bounds("month", datetime(2028, 2, 29, 12, tzinfo=IST))
        self.assertEqual(
            (start, end), (datetime(2028, 2, 1, tzinfo=IST), datetime(2028, 3, 1, tzinfo=IST))
        )

    def test_total_never_resets(self):
        with at(2020, 1, 1, 0, 0, 0):
            consume_as(self.alice, "bytes", 100)

        with self.assertRaises(UserLimitExceeded) as caught:
            consume_as(self.alice, "bytes", 1)
        self.assertIsNone(caught.exception.resets_at)
        self.assertIsNone(service.usage(self.alice, "bytes")["resets_at"])

    def test_usage_reports_the_period(self):
        with at(2026, 10, 5, 0, 0, 0):
            consume_as(self.alice, "chat_turns")
            self.assertEqual(
                service.usage(self.alice, "chat_turns"),
                {"used": 1, "limit": 2, "resets_at": datetime(2026, 11, 1, tzinfo=IST)},
            )
        self.assertEqual(
            service.usage(self.alice, "chat_turns", now=datetime(2026, 11, 2, tzinfo=UTC))["used"],
            0,
        )


@override_settings(LIMIT_DEFAULTS=TEST_LIMITS)
class RefundTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")

    def test_a_refunded_event_no_longer_counts(self):
        first = consume_as(self.alice, "chat_turns")
        consume_as(self.alice, "chat_turns")

        self.assertTrue(service.refund(first))
        consume_as(self.alice, "chat_turns")  # the refund made room

        self.assertEqual(service.usage(self.alice, "chat_turns")["used"], 2)
        self.assertEqual(UsageEvent.objects.filter(user=self.alice).count(), 3)

    def test_refunding_twice_refunds_once(self):
        event = consume_as(self.alice, "chat_turns")

        self.assertTrue(service.refund(event))
        self.assertFalse(service.refund(UsageEvent.objects.get(pk=event.pk)))
        self.assertTrue(UsageEvent.objects.get(pk=event.pk).refunded)

    def test_a_refund_frees_system_room_too(self):
        consume_as(None, "signups")
        consume_as(None, "signups")
        third = consume_as(None, "signups")

        service.refund(third)
        consume_as(None, "signups")
        self.assertEqual(service.system_usage("signups")["used"], 3)


@override_settings(LIMIT_DEFAULTS=TEST_LIMITS)
class SystemLimitTests(QuietSystemLimitLog, TestCase):
    def test_system_only_key(self):
        for _ in range(3):
            consume_as(None, "signups")

        with self.assertRaises(SystemLimitExceeded) as caught:
            consume_as(None, "signups")

        self.assertEqual(caught.exception.key, "signups")
        self.assertEqual(service.system_usage("signups")["used"], 3)

    def test_system_limit_counts_every_user(self):
        Limit.objects.create(
            key="chat_turns", user_free=2, user_premium=2, system=3, period="month"
        )
        alice, bob, carol = make_user("alice"), make_user("bob"), make_user("carol")
        consume_as(alice, "chat_turns")
        consume_as(alice, "chat_turns")
        consume_as(bob, "chat_turns")

        # Carol has her whole quota left; the system has none.
        with self.assertRaises(SystemLimitExceeded):
            consume_as(carol, "chat_turns")
        self.assertEqual(service.usage(carol, "chat_turns")["used"], 0)

    def test_the_user_limit_is_checked_first(self):
        Limit.objects.create(
            key="chat_turns", user_free=1, user_premium=1, system=1, period="month"
        )
        alice = make_user("alice")
        consume_as(alice, "chat_turns")

        with self.assertRaises(UserLimitExceeded):
            consume_as(alice, "chat_turns")

    def test_system_amounts(self):
        consume_as(make_user("alice"), "bytes", 100)
        with self.assertRaises(SystemLimitExceeded):
            consume_as(make_user("bob"), "bytes", 51)
        consume_as(make_user("carol"), "bytes", 50)

    def test_a_deleted_account_still_counts_for_the_system(self):
        Limit.objects.create(
            key="chat_turns", user_free=2, user_premium=2, system=2, period="month"
        )
        alice = make_user("alice")
        consume_as(alice, "chat_turns")
        consume_as(alice, "chat_turns")
        alice.delete()

        with self.assertRaises(SystemLimitExceeded):
            consume_as(make_user("bob"), "chat_turns")


@override_settings(
    LIMIT_DEFAULTS=TEST_LIMITS,
    ADMINS=[("Admin", "admin@example.com")],
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class AdminMailTests(QuietSystemLimitLog, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        for _ in range(3):
            consume_as(None, "signups")

    def refuse(self):
        with self.assertRaises(SystemLimitExceeded):
            consume_as(None, "signups")

    def test_the_first_refusal_mails_the_admins_once(self):
        self.refuse()
        self.refuse()
        self.refuse()

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("signups", mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].to, ["admin@example.com"])

    def test_a_new_period_alerts_again(self):
        self.refuse()
        with mock.patch(
            "django.utils.timezone.now", return_value=datetime.now(UTC) + timedelta(days=1)
        ):
            for _ in range(3):
                consume_as(None, "signups")
            self.refuse()

        self.assertEqual(len(mail.outbox), 2)

    def test_a_raised_limit_alerts_again_when_reached(self):
        self.refuse()
        Limit.objects.create(key="signups", system=4, period="day")
        consume_as(None, "signups")
        self.refuse()

        self.assertEqual(len(mail.outbox), 2)

    def test_a_broker_failure_keeps_the_refusal_and_retries_the_mail(self):
        with (
            mock.patch("limits.tasks.mail_system_limit_reached.delay", side_effect=OSError),
            self.assertLogs("limits.service", "ERROR"),
        ):
            self.refuse()
        self.refuse()

        self.assertEqual(len(mail.outbox), 1)

    def test_no_mail_under_the_limit(self):
        self.assertEqual(len(mail.outbox), 0)
