"""The month window, the quota count, and create_ask's idempotency."""

from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, TestCase, override_settings

from assistant import quota
from assistant.models import AskQuery
from assistant.services import IdempotencyKeyReused, QuotaExceeded, create_ask
from limits.models import Limit, UsageEvent
from limits.service import SystemLimitExceeded
from notes.tests.helpers import make_user

from .helpers import chat_turns, record_usage

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


def ask_row(user, key, status=AskQuery.Status.DONE, created_at=None):
    """An AskQuery as the task would leave it, without running anything."""
    ask = AskQuery.objects.create(
        user=user, question=f"q {key}", idempotency_key=key, status=status
    )
    if created_at is not None:
        # auto_now_add ignores a value passed to create().
        AskQuery.objects.filter(pk=ask.pk).update(created_at=created_at)
    return record_usage(ask)


class MonthBoundsTests(SimpleTestCase):
    def test_month_is_local_not_utc(self):
        # 20:00 UTC on 31 Oct is 01:30 on 1 Nov in Kolkata: November already.
        start, end = quota.month_bounds(datetime(2026, 10, 31, 20, 0, tzinfo=UTC))
        self.assertEqual(start, datetime(2026, 11, 1, tzinfo=IST))
        self.assertEqual(end, datetime(2026, 12, 1, tzinfo=IST))

    def test_december_rolls_into_next_year(self):
        start, end = quota.month_bounds(datetime(2026, 12, 15, tzinfo=IST))
        self.assertEqual(
            (start, end), (datetime(2026, 12, 1, tzinfo=IST), datetime(2027, 1, 1, tzinfo=IST))
        )

    def test_short_month(self):
        _, end = quota.month_bounds(datetime(2027, 2, 28, 23, 59, tzinfo=IST))
        self.assertEqual(end, datetime(2027, 3, 1, tzinfo=IST))


@override_settings(LIMIT_DEFAULTS=chat_turns(3, 10))
class UsageTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.now = datetime(2026, 10, 15, 12, 0, tzinfo=IST)

    def test_counts_only_this_months_non_failed_asks(self):
        this_month = self.now - timedelta(days=3)
        ask_row(self.alice, "done", created_at=this_month)
        ask_row(self.alice, "pending", AskQuery.Status.PENDING, created_at=this_month)
        ask_row(self.alice, "running", AskQuery.Status.RUNNING, created_at=this_month)
        ask_row(self.alice, "failed", AskQuery.Status.FAILED, created_at=this_month)
        # One second before the month began, in Kolkata.
        ask_row(self.alice, "last-month", created_at=datetime(2026, 9, 30, 23, 59, 59, tzinfo=IST))
        ask_row(make_user("bob"), "bob", created_at=this_month)

        self.assertEqual(
            quota.usage(self.alice, self.now),
            {"used": 3, "limit": 3, "resets_at": datetime(2026, 11, 1, tzinfo=IST)},
        )

    def test_limit_follows_the_plan(self):
        self.alice.plan = "premium"
        self.assertEqual(quota.usage(self.alice, self.now)["limit"], 10)


@override_settings(LIMIT_DEFAULTS=chat_turns(2, 10))
class CreateAskTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")

    def test_creates_and_enqueues_on_commit(self):
        with (
            mock.patch("assistant.tasks.answer_ask.delay") as delay,
            self.captureOnCommitCallbacks(execute=True) as callbacks,
        ):
            ask, created = create_ask(self.alice, "  When is the launch?  ", "key-1")

        self.assertTrue(created)
        self.assertEqual((ask.question, ask.status), ("When is the launch?", "pending"))
        self.assertEqual(len(callbacks), 1)
        delay.assert_called_once_with(ask.pk)

    def test_over_quota_raises_with_usage(self):
        ask_row(self.alice, "a")
        ask_row(self.alice, "b")

        with self.assertRaises(QuotaExceeded) as caught:
            create_ask(self.alice, "One more?", "c")

        self.assertEqual((caught.exception.used, caught.exception.limit), (2, 2))
        self.assertEqual(caught.exception.resets_at, quota.month_bounds()[1])
        self.assertEqual(AskQuery.objects.count(), 2)

    def test_limit_is_read_from_the_committed_plan(self):
        ask_row(self.alice, "a")
        ask_row(self.alice, "b")
        type(self.alice).objects.filter(pk=self.alice.pk).update(plan="premium")

        # self.alice is stale ("free"); the locked copy is not.
        _, created = create_ask(self.alice, "Premium now?", "c")
        self.assertTrue(created)

    def test_failed_asks_do_not_count(self):
        ask_row(self.alice, "a")
        ask_row(self.alice, "b", AskQuery.Status.FAILED)

        _, created = create_ask(self.alice, "Room for this?", "c")
        self.assertTrue(created)

    def test_same_key_returns_the_same_ask_and_counts_once(self):
        with self.captureOnCommitCallbacks() as first_callbacks:
            first, created = create_ask(self.alice, "When is the launch?", "key-1")
        with self.captureOnCommitCallbacks() as second_callbacks:
            second, replayed = create_ask(self.alice, "When is the launch?", "key-1")

        self.assertEqual((created, replayed), (True, False))
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(quota.usage(self.alice)["used"], 1)
        # A replay is not answered twice.
        self.assertEqual((len(first_callbacks), len(second_callbacks)), (1, 0))

    def test_replay_is_returned_even_when_the_month_is_full(self):
        first, _ = create_ask(self.alice, "When is the launch?", "key-1")
        ask_row(self.alice, "b")

        again, created = create_ask(self.alice, "When is the launch?", "key-1")
        self.assertEqual((again.pk, created), (first.pk, False))

    def test_replay_ignores_surrounding_whitespace(self):
        first, _ = create_ask(self.alice, "When is the launch?", "key-1")
        again, created = create_ask(self.alice, "When is the launch? ", "key-1")
        self.assertEqual((again.pk, created), (first.pk, False))

    def test_same_key_with_another_question_is_refused(self):
        first, _ = create_ask(self.alice, "When is the launch?", "key-1")

        with self.assertRaises(IdempotencyKeyReused) as caught:
            create_ask(self.alice, "Who is on the team?", "key-1")

        self.assertEqual(caught.exception.existing.pk, first.pk)
        self.assertEqual(AskQuery.objects.count(), 1)

    def test_keys_are_per_user(self):
        mine, _ = create_ask(self.alice, "When is the launch?", "key-1")
        theirs, created = create_ask(make_user("bob"), "When is the launch?", "key-1")
        self.assertTrue(created)
        self.assertNotEqual(mine.pk, theirs.pk)


@override_settings(LIMIT_DEFAULTS=chat_turns(2, 10))
class CreateAskUsageTests(TestCase):
    """create_ask on the limits ledger (DECISIONS D101)."""

    def setUp(self):
        self.alice = make_user("alice")

    def test_a_new_ask_consumes_one_chat_turn_linked_to_it(self):
        ask, _ = create_ask(self.alice, "When is the launch?", "key-1")

        event = UsageEvent.objects.get()
        self.assertEqual(
            (event.user, event.key, event.amount, event.ask), (self.alice, "chat_turns", 1, ask)
        )
        self.assertFalse(event.refunded)

    def test_a_replay_consumes_nothing(self):
        create_ask(self.alice, "When is the launch?", "key-1")
        create_ask(self.alice, "When is the launch?", "key-1")
        with self.assertRaises(IdempotencyKeyReused):
            create_ask(self.alice, "Another question?", "key-1")

        self.assertEqual(UsageEvent.objects.count(), 1)

    def test_a_refusal_leaves_no_ask_and_no_event(self):
        create_ask(self.alice, "One?", "a")
        create_ask(self.alice, "Two?", "b")

        with self.assertRaises(QuotaExceeded):
            create_ask(self.alice, "Three?", "c")

        self.assertEqual((AskQuery.objects.count(), UsageEvent.objects.count()), (2, 2))

    def test_the_admins_limit_row_is_the_quota(self):
        Limit.objects.create(
            key="chat_turns", user_free=1, user_premium=1, system=None, period="month"
        )
        create_ask(self.alice, "One?", "a")

        with self.assertRaises(QuotaExceeded) as caught:
            create_ask(self.alice, "Two?", "b")
        self.assertEqual(caught.exception.limit, 1)

    def test_the_system_limit_passes_through_and_leaves_nothing(self):
        Limit.objects.create(
            key="chat_turns", user_free=5, user_premium=5, system=1, period="month"
        )
        create_ask(make_user("bob"), "Bob's?", "a")

        with self.assertLogs("limits.service", "WARNING"), self.assertRaises(SystemLimitExceeded):
            create_ask(self.alice, "Mine?", "b")
        self.assertFalse(AskQuery.objects.filter(user=self.alice).exists())
