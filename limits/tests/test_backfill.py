"""The data migration that moves V1's asks onto the ledger (DECISIONS D103)."""

import importlib
from datetime import datetime
from zoneinfo import ZoneInfo

from django.apps import apps
from django.test import TestCase

from assistant import quota
from assistant.models import AskQuery
from limits.models import UsageEvent
from notes.tests.helpers import make_user

backfill = importlib.import_module("limits.migrations.0002_backfill_ask_usage")

IST = ZoneInfo("Asia/Kolkata")


class BackfillTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.now = datetime(2026, 10, 15, tzinfo=IST)
        last_month = datetime(2026, 9, 30, 23, 59, tzinfo=IST)
        for key, status, created_at in [
            ("done", "done", self.now),
            ("pending", "pending", self.now),
            ("running", "running", self.now),
            ("failed", "failed", self.now),
            ("old", "done", last_month),
        ]:
            ask = AskQuery.objects.create(
                user=self.alice, question=key, idempotency_key=key, status=status
            )
            AskQuery.objects.filter(pk=ask.pk).update(created_at=created_at)

    def test_one_event_per_non_failed_ask_keeps_this_months_count(self):
        backfill.backfill(apps, None)

        events = UsageEvent.objects.order_by("ask__idempotency_key")
        self.assertEqual(
            [e.ask.idempotency_key for e in events], ["done", "old", "pending", "running"]
        )
        for event in events:
            self.assertEqual(
                (event.user, event.key, event.amount, event.created_at, event.refunded),
                (self.alice, "chat_turns", 1, event.ask.created_at, False),
            )
        # V1 counted 3 this month (done, pending, running); so does the ledger.
        self.assertEqual(quota.used(self.alice, self.now), 3)

    def test_an_answered_asks_cost_is_copied_and_others_have_none(self):
        AskQuery.objects.filter(idempotency_key="done").update(
            provider="claude", model="m-1", input_tokens=120, output_tokens=8
        )

        backfill.backfill(apps, None)

        answered = UsageEvent.objects.get(ask__idempotency_key="done")
        pending = UsageEvent.objects.get(ask__idempotency_key="pending")
        self.assertEqual(
            (answered.provider, answered.model, answered.input_tokens, answered.output_tokens),
            ("claude", "m-1", 120, 8),
        )
        self.assertEqual((pending.provider, pending.input_tokens), ("", None))

    def test_running_it_twice_adds_nothing(self):
        backfill.backfill(apps, None)
        backfill.backfill(apps, None)
        self.assertEqual(UsageEvent.objects.count(), 4)

    def test_reverse_removes_the_ask_events_only(self):
        backfill.backfill(apps, None)
        UsageEvent.objects.create(key="signups")

        backfill.unfill(apps, None)

        self.assertEqual(list(UsageEvent.objects.values_list("key", flat=True)), ["signups"])
