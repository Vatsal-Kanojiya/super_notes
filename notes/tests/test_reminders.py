"""Reminders: the schedule, the service writes, the API, and sync."""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from notes import services
from notes.models import Note, Reminder, ReminderDelivery
from notes.schedule import occurrences, user_timezone

from .helpers import make_user

# datetime.UTC is Python 3.11+; the project targets 3.10 (CI).
UTC = dt_timezone.utc

LONDON = ZoneInfo("Europe/London")
KOLKATA = ZoneInfo("Asia/Kolkata")

NOTES = "/api/v1/notes/"
REMINDERS = "/api/v1/reminders/"
CHANGES = "/api/v1/notes/changes/"


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def revision_of(user):
    user.refresh_from_db(fields=["notes_revision"])
    return user.notes_revision


class ScheduleTests(SimpleTestCase):
    def test_default_lead_is_eight_daily_notifications_at_the_same_local_time(self):
        due = datetime(2026, 6, 15, 9, 30, tzinfo=KOLKATA)

        result = occurrences(due, 7, KOLKATA)

        self.assertEqual(len(result), 8)
        self.assertEqual(result[-1], due)
        self.assertEqual(result[0], due - timedelta(days=7))
        for at in result:
            self.assertEqual(at.tzinfo, UTC)
            self.assertEqual(at.astimezone(KOLKATA).time(), due.timetz().replace(tzinfo=None))

    def test_lead_zero_is_the_due_time_alone(self):
        due = utc(2026, 6, 15, 4, 0)
        self.assertEqual(occurrences(due, 0, KOLKATA), [due])

    def test_across_the_october_change_the_local_time_holds_and_utc_moves(self):
        # Clocks go back on Sunday 25 October 2026 in London.
        due = datetime(2026, 10, 27, 9, 0, tzinfo=LONDON)

        result = occurrences(due, 7, LONDON)

        self.assertEqual(len(result), 8)
        self.assertEqual(
            [at.astimezone(LONDON).strftime("%d %H:%M") for at in result],
            [f"{day} 09:00" for day in range(20, 28)],
        )
        # BST (UTC+1) before the change, GMT after.
        self.assertEqual(result[4], utc(2026, 10, 24, 8, 0))
        self.assertEqual(result[5], utc(2026, 10, 25, 9, 0))

    def test_a_repeated_local_time_is_its_first_instance(self):
        # 01:30 happens twice on 25 October; the heads-up takes the BST one.
        due = datetime(2026, 10, 26, 1, 30, tzinfo=LONDON)
        result = occurrences(due, 1, LONDON)
        self.assertEqual(result[0], utc(2026, 10, 25, 0, 30))

    def test_the_due_occurrence_is_due_at_itself_even_as_the_second_instance(self):
        due = datetime(2026, 10, 25, 1, 30, tzinfo=LONDON, fold=1)  # 01:30 GMT
        result = occurrences(due, 1, LONDON)
        self.assertEqual(result[-1], utc(2026, 10, 25, 1, 30))

    def test_a_skipped_local_time_lands_just_after_the_change(self):
        # 01:30 does not exist on Sunday 29 March 2026 in London.
        due = datetime(2026, 3, 30, 1, 30, tzinfo=LONDON)

        result = occurrences(due, 2, LONDON)

        self.assertEqual(result[1], utc(2026, 3, 29, 1, 30))  # 02:30 BST, not 00:30
        self.assertEqual(result[1].astimezone(LONDON).strftime("%H:%M"), "02:30")
        self.assertEqual(result, sorted(result))

    def test_naive_due_at_is_refused(self):
        with self.assertRaises(ValueError):
            occurrences(datetime(2026, 1, 1, 9, 0), 1, LONDON)

    def test_an_unknown_user_timezone_falls_back_to_the_site_default(self):
        class Somebody:
            timezone = "Mars/Olympus_Mons"

        self.assertEqual(user_timezone(Somebody()), ZoneInfo("Asia/Kolkata"))


class ServiceTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        self.note = services.create_note(self.alice, title="passport")
        self.due = timezone.now() + timedelta(days=30)

    def test_create_takes_a_revision_and_stamps_the_note_without_a_new_version(self):
        updated_at = self.note.updated_at

        reminder = services.create_reminder(self.alice, self.note.pk, due_at=self.due)

        self.note.refresh_from_db()
        self.assertEqual(revision_of(self.alice), 2)
        self.assertEqual(self.note.revision, 2)
        self.assertEqual(self.note.version, 1)
        self.assertEqual(self.note.updated_at, updated_at)
        self.assertEqual((reminder.owner, reminder.lead_days), (self.alice, 7))
        self.assertEqual(reminder.channels, ["email", "push"])
        self.assertEqual(reminder.status, Reminder.Status.SCHEDULED)

    def test_create_on_someone_elses_note_or_a_deleted_one_is_refused(self):
        with self.assertRaises(Note.DoesNotExist):
            services.create_reminder(self.bob, self.note.pk, due_at=self.due)
        services.delete_note(self.alice, self.note.pk)
        with self.assertRaises(Note.DoesNotExist):
            services.create_reminder(self.alice, self.note.pk, due_at=self.due)
        self.assertFalse(Reminder.objects.exists())
        self.assertEqual(revision_of(self.bob), 0)

    def test_a_note_holds_at_most_the_cap(self):
        for _ in range(services.REMINDERS_PER_NOTE_MAX):
            services.create_reminder(self.alice, self.note.pk, due_at=self.due)
        before = revision_of(self.alice)

        with self.assertRaises(services.TooManyReminders):
            services.create_reminder(self.alice, self.note.pk, due_at=self.due)
        self.assertEqual(revision_of(self.alice), before)

    def test_deleted_reminders_do_not_count_toward_the_cap(self):
        first = services.create_reminder(self.alice, self.note.pk, due_at=self.due)
        for _ in range(services.REMINDERS_PER_NOTE_MAX - 1):
            services.create_reminder(self.alice, self.note.pk, due_at=self.due)
        services.delete_reminder(self.alice, first.pk)

        services.create_reminder(self.alice, self.note.pk, due_at=self.due)

    def test_update_changes_fields_and_stamps_the_note(self):
        reminder = services.create_reminder(self.alice, self.note.pk, due_at=self.due)

        services.update_reminder(self.alice, reminder.pk, lead_days=0, channels=["email"])

        reminder.refresh_from_db()
        self.note.refresh_from_db()
        self.assertEqual((reminder.lead_days, reminder.channels), (0, ["email"]))
        self.assertEqual(self.note.revision, 3)

    def test_update_refuses_other_fields(self):
        reminder = services.create_reminder(self.alice, self.note.pk, due_at=self.due)
        with self.assertRaises(TypeError):
            services.update_reminder(self.alice, reminder.pk, status="done")

    def test_writes_to_someone_elses_reminder_are_refused(self):
        reminder = services.create_reminder(self.alice, self.note.pk, due_at=self.due)
        for write in (
            lambda: services.update_reminder(self.bob, reminder.pk, lead_days=1),
            lambda: services.mark_reminder_done(self.bob, reminder.pk),
            lambda: services.delete_reminder(self.bob, reminder.pk),
        ):
            with self.assertRaises(Reminder.DoesNotExist):
                write()
        reminder.refresh_from_db()
        self.assertEqual((reminder.lead_days, reminder.status), (7, "scheduled"))
        self.assertIsNone(reminder.deleted_at)

    def test_done_twice_takes_one_revision(self):
        reminder = services.create_reminder(self.alice, self.note.pk, due_at=self.due)

        services.mark_reminder_done(self.alice, reminder.pk)
        again = services.mark_reminder_done(self.alice, reminder.pk)

        self.assertEqual(again.status, Reminder.Status.DONE)
        self.assertEqual(revision_of(self.alice), 3)

    def test_deleting_the_note_cancels_its_scheduled_reminders_only(self):
        scheduled = services.create_reminder(self.alice, self.note.pk, due_at=self.due)
        done = services.create_reminder(self.alice, self.note.pk, due_at=self.due)
        services.mark_reminder_done(self.alice, done.pk)
        other_note = services.create_note(self.alice)
        elsewhere = services.create_reminder(self.alice, other_note.pk, due_at=self.due)

        services.delete_note(self.alice, self.note.pk)

        statuses = dict(Reminder.objects.values_list("pk", "status"))
        self.assertEqual(statuses[scheduled.pk], "cancelled")
        self.assertEqual(statuses[done.pk], "done")
        self.assertEqual(statuses[elsewhere.pk], "scheduled")

    def test_a_deleted_notes_reminders_cannot_be_written(self):
        reminder = services.create_reminder(self.alice, self.note.pk, due_at=self.due)
        services.delete_note(self.alice, self.note.pk)
        with self.assertRaises(Reminder.DoesNotExist):
            services.mark_reminder_done(self.alice, reminder.pk)

    def test_a_delivery_is_unique_per_occurrence(self):
        reminder = services.create_reminder(self.alice, self.note.pk, due_at=self.due)
        ReminderDelivery.objects.create(reminder=reminder, occurrence_at=self.due)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReminderDelivery.objects.create(reminder=reminder, occurrence_at=self.due)

    def test_lead_days_above_the_maximum_is_refused_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Reminder.objects.create(note=self.note, owner=self.alice, due_at=self.due, lead_days=31)


class ReminderAPITestCase(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        self.client = APIClient()
        self.client.force_authenticate(self.alice)
        self.bobs_client = APIClient()
        self.bobs_client.force_authenticate(self.bob)
        self.note = services.create_note(self.alice, title="passport")
        self.due = (timezone.now() + timedelta(days=30)).replace(microsecond=0)

    def create(self, on=None, client=None, **payload):
        payload.setdefault("due_at", self.due.isoformat())
        note_id = (on or self.note).pk
        return (client or self.client).post(f"{NOTES}{note_id}/reminders/", payload, format="json")

    def reminder(self, **fields):
        fields.setdefault("due_at", self.due)
        return services.create_reminder(self.alice, self.note.pk, **fields)


class CreateTests(ReminderAPITestCase):
    def test_create_with_defaults(self):
        response = self.create()

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["note"], self.note.pk)
        self.assertEqual(body["lead_days"], 7)
        self.assertEqual(body["channels"], ["email", "push"])
        self.assertEqual(body["status"], "scheduled")
        self.assertEqual(datetime.fromisoformat(body["due_at"]), self.due)
        self.assertEqual(Reminder.objects.get(pk=body["id"]).owner, self.alice)

    def test_an_offset_is_kept_as_the_same_instant(self):
        local = self.due.astimezone(LONDON).isoformat()
        response = self.create(due_at=local, lead_days=0, channels=["push", "push"])

        self.assertEqual(response.status_code, 201)
        reminder = Reminder.objects.get()
        self.assertEqual(reminder.due_at, self.due)
        self.assertEqual((reminder.lead_days, reminder.channels), (0, ["push"]))

    def test_invalid_payloads_are_400_and_write_nothing(self):
        past = (timezone.now() - timedelta(minutes=1)).isoformat()
        naive = self.due.replace(tzinfo=None).isoformat()
        for payload in (
            {"due_at": naive},
            {"due_at": past},
            {"due_at": "next friday"},
            {"lead_days": 31},
            {"lead_days": -1},
            {"channels": []},
            {"channels": ["sms"]},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.create(**payload).status_code, 400)
        response = self.client.post(f"{NOTES}{self.note.pk}/reminders/", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Reminder.objects.exists())
        self.assertEqual(revision_of(self.alice), 1)

    def test_server_fields_in_the_payload_are_ignored(self):
        response = self.create(status="done", owner=self.bob.pk, note=999)
        reminder = Reminder.objects.get(pk=response.json()["id"])
        self.assertEqual(
            (reminder.status, reminder.owner, reminder.note), ("scheduled", self.alice, self.note)
        )

    def test_someone_elses_note_is_404(self):
        response = self.create(client=self.bobs_client)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Reminder.objects.exists())

    def test_a_deleted_note_is_404(self):
        services.delete_note(self.alice, self.note.pk)
        self.assertEqual(self.create().status_code, 404)

    def test_past_the_cap_is_400_with_a_code(self):
        for _ in range(services.REMINDERS_PER_NOTE_MAX):
            self.reminder()
        response = self.create()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "too_many_reminders")


class ChangeTests(ReminderAPITestCase):
    def test_patch_changes_what_was_sent(self):
        reminder = self.reminder()
        later = self.due + timedelta(days=1)

        response = self.client.patch(
            f"{REMINDERS}{reminder.pk}/", {"due_at": later.isoformat()}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        reminder.refresh_from_db()
        self.assertEqual((reminder.due_at, reminder.lead_days), (later, 7))

    def test_patch_validates(self):
        reminder = self.reminder()
        response = self.client.patch(f"{REMINDERS}{reminder.pk}/", {"lead_days": 99}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_patch_does_not_reopen_a_done_reminder(self):
        reminder = self.reminder()
        services.mark_reminder_done(self.alice, reminder.pk)

        response = self.client.patch(f"{REMINDERS}{reminder.pk}/", {"lead_days": 1}, format="json")

        self.assertEqual(response.json()["status"], "done")

    def test_done_stops_the_series(self):
        reminder = self.reminder()

        response = self.client.post(f"{REMINDERS}{reminder.pk}/done/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "done")
        self.assertEqual(self.client.post(f"{REMINDERS}{reminder.pk}/done/").status_code, 200)

    def test_delete_is_soft_and_then_404(self):
        reminder = self.reminder()

        self.assertEqual(self.client.delete(f"{REMINDERS}{reminder.pk}/").status_code, 204)

        reminder.refresh_from_db()
        self.assertIsNotNone(reminder.deleted_at)
        self.assertEqual(self.client.delete(f"{REMINDERS}{reminder.pk}/").status_code, 404)
        self.assertEqual(self.client.post(f"{REMINDERS}{reminder.pk}/done/").status_code, 404)

    def test_someone_elses_reminder_is_404_everywhere_and_unchanged(self):
        reminder = self.reminder()
        url = f"{REMINDERS}{reminder.pk}/"

        self.assertEqual(
            self.bobs_client.patch(url, {"lead_days": 1}, format="json").status_code, 404
        )
        self.assertEqual(self.bobs_client.post(f"{url}done/").status_code, 404)
        self.assertEqual(self.bobs_client.delete(url).status_code, 404)

        reminder.refresh_from_db()
        self.assertEqual((reminder.lead_days, reminder.status), (7, "scheduled"))
        self.assertIsNone(reminder.deleted_at)

    def test_a_deleted_notes_reminder_is_404(self):
        reminder = self.reminder()
        services.delete_note(self.alice, self.note.pk)
        self.assertEqual(self.client.post(f"{REMINDERS}{reminder.pk}/done/").status_code, 404)


class RangeTests(ReminderAPITestCase):
    def setUp(self):
        super().setUp()
        self.alice.timezone = "Europe/London"
        self.alice.save(update_fields=["timezone"])

    def get_range(self, start, end, client=None):
        return (client or self.client).get(
            REMINDERS, {"from": start.isoformat(), "to": end.isoformat()}
        )

    def in_range(self, start, end):
        response = self.get_range(start, end)
        self.assertEqual(response.status_code, 200)
        return response.json()["results"]

    def test_occurrences_in_range_at_the_users_local_time_across_dst(self):
        reminder = self.reminder(due_at=datetime(2026, 10, 27, 9, 0, tzinfo=LONDON))

        results = self.in_range(utc(2026, 10, 1), utc(2026, 11, 1))

        self.assertEqual([r["reminder"]["id"] for r in results], [reminder.pk])
        self.assertEqual(results[0]["note_title"], "passport")
        times = [datetime.fromisoformat(at) for at in results[0]["occurrences"]]
        self.assertEqual(len(times), 8)
        self.assertEqual({at.astimezone(LONDON).strftime("%H:%M") for at in times}, {"09:00"})

    def test_only_the_occurrences_inside_the_range_are_listed(self):
        self.reminder(due_at=datetime(2026, 10, 27, 9, 0, tzinfo=LONDON))

        # Due after the range ends, so only the heads-ups 20-22 fall in it.
        results = self.in_range(utc(2026, 10, 1), utc(2026, 10, 23))

        self.assertEqual(len(results[0]["occurrences"]), 3)

    def test_the_end_is_exclusive_and_lead_zero_is_one_occurrence(self):
        due = utc(2026, 10, 10, 12, 0)
        self.reminder(due_at=due, lead_days=0)

        self.assertEqual(self.in_range(utc(2026, 10, 1), due), [])
        self.assertEqual(len(self.in_range(due, due + timedelta(seconds=1))[0]["occurrences"]), 1)

    def test_due_before_the_range_or_heads_ups_after_it_are_left_out(self):
        self.reminder(due_at=utc(2026, 9, 30, 12, 0))
        self.reminder(due_at=utc(2026, 11, 20, 12, 0))
        self.assertEqual(self.in_range(utc(2026, 10, 1), utc(2026, 11, 1)), [])

    def test_done_reminders_are_listed_deleted_ones_and_deleted_notes_are_not(self):
        done = self.reminder(due_at=utc(2026, 10, 10, 12, 0))
        services.mark_reminder_done(self.alice, done.pk)
        deleted = self.reminder(due_at=utc(2026, 10, 11, 12, 0))
        services.delete_reminder(self.alice, deleted.pk)
        gone_note = services.create_note(self.alice)
        services.create_reminder(self.alice, gone_note.pk, due_at=utc(2026, 10, 12, 12, 0))
        services.delete_note(self.alice, gone_note.pk)

        results = self.in_range(utc(2026, 10, 1), utc(2026, 11, 1))

        self.assertEqual(
            [(r["reminder"]["id"], r["reminder"]["status"]) for r in results], [(done.pk, "done")]
        )

    def test_someone_elses_reminders_are_never_listed(self):
        self.reminder(due_at=utc(2026, 10, 10, 12, 0))
        response = self.get_range(utc(2026, 10, 1), utc(2026, 11, 1), client=self.bobs_client)
        self.assertEqual(response.json(), {"results": []})

    def test_range_validation(self):
        start = utc(2026, 10, 1)
        self.assertEqual(self.get_range(start, start + timedelta(days=62)).status_code, 200)
        for end in (start, start - timedelta(days=1), start + timedelta(days=62, seconds=1)):
            with self.subTest(end=end):
                self.assertEqual(self.get_range(start, end).status_code, 400)
        self.assertEqual(self.client.get(REMINDERS).status_code, 400)
        self.assertEqual(
            self.client.get(
                REMINDERS, {"from": "2026-10-01T00:00:00", "to": "2026-10-02T00:00:00"}
            ).status_code,
            400,
        )


class SyncTests(ReminderAPITestCase):
    def changes(self, after=0):
        response = self.client.get(CHANGES, {"after": after})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_a_reminder_write_sends_its_note_again_with_its_reminders(self):
        after = self.changes()["latest_revision"]

        response = self.create()

        body = self.changes(after)
        self.assertEqual([n["id"] for n in body["results"]], [self.note.pk])
        note = body["results"][0]
        self.assertEqual(note["version"], 1)
        self.assertEqual([r["id"] for r in note["reminders"]], [response.json()["id"]])
        self.assertEqual(note["reminders"][0]["status"], "scheduled")

    def test_each_reminder_write_moves_the_note_past_the_clients_revision(self):
        reminder = self.reminder()
        for write in (
            lambda: services.update_reminder(self.alice, reminder.pk, lead_days=2),
            lambda: services.mark_reminder_done(self.alice, reminder.pk),
            lambda: services.delete_reminder(self.alice, reminder.pk),
        ):
            after = self.changes()["latest_revision"]
            write()
            self.assertEqual([n["id"] for n in self.changes(after)["results"]], [self.note.pk])

    def test_a_deleted_reminder_drops_out_of_its_notes_list(self):
        keep = self.reminder()
        drop = self.reminder()
        services.delete_reminder(self.alice, drop.pk)

        note = self.changes()["results"][0]

        self.assertEqual([r["id"] for r in note["reminders"]], [keep.pk])

    def test_a_note_without_reminders_has_an_empty_list(self):
        self.assertEqual(self.changes()["results"][0]["reminders"], [])

    def test_a_tombstone_carries_no_reminders(self):
        self.reminder()
        services.delete_note(self.alice, self.note.pk)
        tombstone = self.changes()["results"][0]
        self.assertIsNotNone(tombstone["deleted_at"])
        self.assertNotIn("reminders", tombstone)

    def test_reminders_are_fetched_in_one_query_for_the_batch(self):
        for _ in range(3):
            note = services.create_note(self.alice)
            services.create_reminder(self.alice, note.pk, due_at=self.due)
        # Auth is forced; the ceiling, the notes, their reminders and their
        # attachments.
        with self.assertNumQueries(4):
            self.changes()
