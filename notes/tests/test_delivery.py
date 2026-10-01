"""Reminder delivery: what is due, the at-most-once claim, the email."""

import threading
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import mock
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core import mail
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, override_settings

from notes import delivery, services
from notes.models import Reminder, ReminderDelivery
from notes.schedule import occurrences
from notes.tasks import send_reminder_task

from .helpers import doc, make_user

# datetime.UTC is Python 3.11+; CI runs 3.10.
UTC = dt_timezone.utc
LONDON = ZoneInfo("Europe/London")

# Due Tuesday 27 October 2026, 09:00 London. The clocks go back on the
# 25th, so the heads-ups are 08:00 UTC until the 24th and 09:00 UTC after.
DUE = datetime(2026, 10, 27, 9, 0, tzinfo=LONDON)
SERIES = occurrences(DUE, 7, LONDON)


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def make_reminder(owner, *, title="Passport", due_at=DUE, changed_at=None, **fields):
    """A reminder last changed at ``changed_at`` (default: well before its series)."""
    note = services.create_note(owner, title=title, content=doc("the secret body"))
    reminder = services.create_reminder(owner, note.pk, due_at=due_at, **fields)
    changed_at = changed_at or utc(2026, 10, 1)
    Reminder.objects.filter(pk=reminder.pk).update(updated_at=changed_at)
    reminder.refresh_from_db()
    return reminder


def london_user(name="alice"):
    user = make_user(name)
    user.timezone = "Europe/London"
    user.save(update_fields=["timezone"])
    return user


class SweepTestCase(TestCase):
    def setUp(self):
        self.alice = london_user()

    def sweep(self, now):
        """One sweep with its on-commit sends run (eager, so mail is sent inline)."""
        with self.captureOnCommitCallbacks(execute=True):
            return delivery.sweep(now)

    def delivered(self, reminder=None):
        rows = ReminderDelivery.objects.order_by("occurrence_at")
        if reminder is not None:
            rows = rows.filter(reminder=reminder)
        return list(rows.values_list("occurrence_at", flat=True))


class WhatIsDueTests(SweepTestCase):
    def test_nothing_before_the_first_heads_up(self):
        make_reminder(self.alice)
        self.assertEqual(self.sweep(SERIES[0] - timedelta(seconds=1)), [])
        self.assertEqual(mail.outbox, [])

    def test_each_occurrence_is_sent_once_over_the_whole_series(self):
        reminder = make_reminder(self.alice)

        for at in SERIES:
            # Several sweeps per occurrence, as beat would run them.
            for delay in (timedelta(0), timedelta(seconds=30), timedelta(minutes=5)):
                self.sweep(at + delay)

        self.assertEqual(self.delivered(reminder), SERIES)
        self.assertEqual(len(mail.outbox), 8)
        local_times = {at.astimezone(LONDON).strftime("%H:%M") for at in self.delivered()}
        self.assertEqual(local_times, {"09:00"})

    def test_after_an_outage_only_the_latest_missed_occurrence_is_sent(self):
        reminder = make_reminder(self.alice)
        self.sweep(SERIES[0])

        # Down from the 20th until the 23rd at noon: the 21st, 22nd and
        # 23rd were missed. Only the 23rd's goes out.
        self.sweep(utc(2026, 10, 23, 12, 0))

        self.assertEqual(self.delivered(reminder), [SERIES[0], SERIES[3]])
        self.assertEqual(len(mail.outbox), 2)

    def test_an_occurrence_missed_beyond_the_grace_is_not_sent(self):
        reminder = make_reminder(self.alice, lead_days=0)
        late = DUE + timedelta(hours=settings.REMINDER_MISSED_GRACE_HOURS, minutes=1)

        self.assertEqual(self.sweep(late), [])
        self.sweep(DUE + timedelta(hours=2))
        self.assertEqual(self.delivered(reminder), [DUE])

    def test_a_heads_up_that_passed_before_the_reminder_was_set_is_not_sent(self):
        set_at = utc(2026, 10, 22, 10, 0)  # after that morning's 08:00 UTC heads-up
        reminder = make_reminder(self.alice, changed_at=set_at)

        self.assertEqual(self.sweep(set_at + timedelta(minutes=1)), [])
        self.sweep(SERIES[3])
        self.assertEqual(self.delivered(reminder), [SERIES[3]])

    def test_lead_zero_sends_once_at_the_due_time(self):
        reminder = make_reminder(self.alice, lead_days=0)
        for day in range(20, 28):
            self.sweep(utc(2026, 10, day, 9, 0))
        self.assertEqual(self.delivered(reminder), [DUE])

    def test_done_cancelled_and_deleted_are_never_sent(self):
        done = make_reminder(self.alice, title="done")
        services.mark_reminder_done(self.alice, done.pk)
        deleted = make_reminder(self.alice, title="deleted")
        services.delete_reminder(self.alice, deleted.pk)
        cancelled = make_reminder(self.alice, title="note deleted")
        services.delete_note(self.alice, cancelled.note_id)
        for reminder in (done, deleted):
            # Their writes moved updated_at; put it back so only status counts.
            Reminder.objects.filter(pk=reminder.pk).update(updated_at=utc(2026, 10, 1))

        for at in SERIES:
            self.sweep(at)

        self.assertEqual(self.delivered(), [])
        self.assertEqual(mail.outbox, [])

    def test_mark_done_stops_the_rest_of_the_series(self):
        reminder = make_reminder(self.alice)
        self.sweep(SERIES[0])
        self.sweep(SERIES[1])
        services.mark_reminder_done(self.alice, reminder.pk)

        for at in SERIES[2:]:
            self.sweep(at)

        self.assertEqual(self.delivered(reminder), SERIES[:2])

    def test_reminders_of_different_users_follow_their_own_timezones(self):
        bob = make_user("bob")  # Asia/Kolkata
        alices = make_reminder(self.alice, lead_days=0)
        bobs = make_reminder(bob, lead_days=1)

        # Bob's heads-up is the day before, at the same instant in UTC.
        self.sweep(DUE - timedelta(days=1))
        self.sweep(DUE)

        self.assertEqual(self.delivered(alices), [DUE])
        self.assertEqual(self.delivered(bobs), [DUE - timedelta(days=1), DUE])


class SendTests(SweepTestCase):
    def test_a_reminder_finished_between_claim_and_send_is_skipped(self):
        reminder = make_reminder(self.alice)
        with self.captureOnCommitCallbacks(execute=False) as sends:
            delivery.sweep(SERIES[0])
        services.mark_reminder_done(self.alice, reminder.pk)

        for send in sends:
            send()

        row = ReminderDelivery.objects.get()
        self.assertEqual(row.channel_results, {"skipped": "inactive"})
        self.assertEqual(mail.outbox, [])

    def test_a_send_run_twice_sends_once(self):
        make_reminder(self.alice, channels=["email"])
        (delivery_id,) = self.sweep(SERIES[0])

        send_reminder_task.delay(delivery_id)

        self.assertEqual(len(mail.outbox), 1)
        row = ReminderDelivery.objects.get()
        self.assertIsNotNone(row.sent_at)
        self.assertEqual(row.channel_results, {"email": "sent"})

    def test_push_is_recorded_as_unavailable_until_it_exists(self):
        make_reminder(self.alice)
        self.sweep(SERIES[0])
        self.assertEqual(
            ReminderDelivery.objects.get().channel_results, {"email": "sent", "push": "unavailable"}
        )

    def test_a_failing_email_is_recorded_and_not_retried(self):
        make_reminder(self.alice, channels=["email"])
        with mock.patch("notes.delivery.send_mail", side_effect=OSError("smtp down")) as sender:
            with self.assertLogs("notes.delivery", "ERROR"):
                self.sweep(SERIES[0])
            self.sweep(SERIES[0] + timedelta(minutes=1))

        self.assertEqual(sender.call_count, 1)
        self.assertEqual(ReminderDelivery.objects.get().channel_results, {"email": "failed"})

    def test_a_user_without_an_address_is_recorded(self):
        self.alice.email = ""
        self.alice.save(update_fields=["email"])
        make_reminder(self.alice, channels=["email"])
        self.sweep(SERIES[0])
        self.assertEqual(ReminderDelivery.objects.get().channel_results, {"email": "no_address"})


@override_settings(WEB_APP_URL="https://notes.example.com", DEFAULT_FROM_EMAIL="r@example.com")
class EmailTests(SweepTestCase):
    def test_title_due_date_and_link_and_never_the_body(self):
        reminder = make_reminder(self.alice)

        self.sweep(SERIES[0])

        (message,) = mail.outbox
        self.assertEqual(message.to, ["alice@example.com"])
        self.assertEqual(message.from_email, "r@example.com")
        self.assertEqual(message.subject, "Reminder: Passport (due in 7 days)")
        self.assertIn("Due Tue 27 Oct 2026, 09:00 GMT.", message.body)
        self.assertIn(f"https://notes.example.com/notes/{reminder.note_id}", message.body)
        self.assertNotIn("secret", message.body)
        self.assertNotIn("secret", message.subject)

    def test_the_last_two_say_tomorrow_and_now(self):
        make_reminder(self.alice)
        self.sweep(SERIES[-2])
        self.sweep(SERIES[-1])
        self.assertEqual(
            [m.subject for m in mail.outbox],
            ["Reminder: Passport (due tomorrow)", "Reminder: Passport (due now)"],
        )

    def test_titles_are_one_line_bounded_and_never_empty(self):
        long = make_reminder(self.alice, title="Renew\nthe   passport " + "x" * 200)
        empty = make_reminder(self.alice, title="")

        subject, _ = delivery.reminder_email(long, DUE)
        self.assertNotIn("\n", subject)
        self.assertTrue(subject.startswith("Reminder: Renew the passport xxx"))
        self.assertLessEqual(len(subject), len("Reminder: ") + 100 + len(" (due now)"))
        self.assertEqual(
            delivery.reminder_email(empty, DUE)[0], "Reminder: Untitled note (due now)"
        )


class BeatScheduleTests(TestCase):
    def test_the_sweep_runs_every_minute(self):
        entry = settings.CELERY_BEAT_SCHEDULE["deliver-due-reminders"]
        self.assertEqual(entry["task"], "notes.tasks.deliver_due_reminders")
        self.assertEqual(entry["schedule"], 60)

        from config.celery import app

        self.assertIn(entry["task"], app.tasks)


class ConcurrentSweepTests(TransactionTestCase):
    """Two beat workers sweeping the same minute, proved with real transactions.

    TransactionTestCase so each thread's claim really commits and the other
    really sees it (see test_concurrency.py). Both sweeps are held at a
    barrier after reading their candidates, so both decide the occurrence is
    due before either claims it -- the race the unique claim exists for.
    """

    def setUp(self):
        self.reminder = make_reminder(london_user())

    def race(self, now):
        barrier = threading.Barrier(2)
        original = delivery._candidates
        enqueued, errors = [], []
        lock = threading.Lock()

        def candidates_then_wait(at):
            found = original(at)
            barrier.wait(timeout=10)
            return found

        def record(delivery_id):
            with lock:
                enqueued.append(delivery_id)

        def run():
            try:
                delivery.sweep(now)
            except Exception as exc:  # surfaced below
                errors.append(exc)
            finally:
                connections.close_all()

        with (
            mock.patch.object(delivery, "_candidates", candidates_then_wait),
            mock.patch.object(send_reminder_task, "delay", side_effect=record),
        ):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        self.assertEqual(errors, [])
        return enqueued

    def test_two_concurrent_sweeps_deliver_an_occurrence_exactly_once(self):
        enqueued = self.race(SERIES[0])

        self.assertEqual(len(enqueued), 1)
        rows = ReminderDelivery.objects.filter(reminder=self.reminder)
        self.assertEqual(list(rows.values_list("pk", flat=True)), enqueued)
        self.assertEqual(rows.get().occurrence_at, SERIES[0])

    def test_without_the_unique_constraint_both_sweeps_would_send(self):
        """The guard, removed: the same race then sends the occurrence twice."""
        (constraint,) = ReminderDelivery._meta.constraints
        with connection.schema_editor() as editor:
            editor.remove_constraint(ReminderDelivery, constraint)
        try:
            enqueued = self.race(SERIES[0])
        finally:
            ReminderDelivery.objects.all().delete()
            with connection.schema_editor() as editor:
                editor.add_constraint(ReminderDelivery, constraint)

        self.assertEqual(len(enqueued), 2)
