"""notes/format_service.py: creating a job -- idempotency, the limit, ownership."""

from unittest import mock

from django.test import TestCase, override_settings

from limits import service as limits
from limits.models import UsageEvent
from notes import services
from notes.format_service import (
    IdempotencyKeyReused,
    NoteTooLong,
    NothingToFormat,
    create_format_job,
)
from notes.models import FormatJob, Note

from .helpers import doc, format_limit, make_user

DELAY = "notes.tasks.format_note.delay"


@override_settings(LIMIT_DEFAULTS=format_limit(2, 10))
class CreateFormatJobTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.note = services.create_note(self.alice, title="t", content=doc("buy milk", "call mum"))

    def create(self, key="key-1", note=None, user=None):
        return create_format_job(user or self.alice, (note or self.note).pk, key)

    def test_creates_a_pending_job_at_the_notes_version(self):
        job, created = self.create()

        self.assertTrue(created)
        self.assertEqual(
            (job.status, job.owner_id, job.note_id, job.base_version, job.proposed_content),
            ("pending", self.alice.pk, self.note.pk, self.note.version, None),
        )
        self.assertEqual(job.idempotency_key, "key-1")

    def test_base_version_follows_the_note(self):
        services.update_note(self.alice, self.note.pk, expected_version=1, title="edited")

        job, _ = self.create()
        self.assertEqual(job.base_version, 2)

    def test_consumes_one_format_use_linked_to_the_job(self):
        job, _ = self.create()

        event = UsageEvent.objects.get()
        self.assertEqual((event.user_id, event.key, event.amount), (self.alice.pk, "format", 1))
        self.assertEqual(job.usage_event_id, event.pk)
        self.assertEqual(limits.usage(self.alice, "format")["used"], 1)

    def test_enqueues_on_commit_not_before(self):
        with mock.patch(DELAY) as delay:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                job, _ = self.create()
            delay.assert_not_called()
            self.assertEqual(len(callbacks), 1)

            callbacks[0]()
            delay.assert_called_once_with(job.pk)

    def test_the_same_key_returns_the_same_job_and_counts_once(self):
        with self.captureOnCommitCallbacks() as first_callbacks:
            first, created = self.create()
        with self.captureOnCommitCallbacks() as second_callbacks:
            second, replayed = self.create()

        self.assertEqual((created, replayed), (True, False))
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(FormatJob.objects.count(), 1)
        self.assertEqual(limits.usage(self.alice, "format")["used"], 1)
        # A replay is not run twice.
        self.assertEqual((len(first_callbacks), len(second_callbacks)), (1, 0))

    def test_a_replay_is_returned_even_when_the_limit_is_used_up(self):
        first, _ = self.create("a")
        self.create("b")
        with self.assertRaises(limits.UserLimitExceeded):
            self.create("c")

        again, created = self.create("a")
        self.assertEqual((again.pk, created), (first.pk, False))

    def test_a_replay_survives_the_note_being_deleted(self):
        first, _ = self.create()
        services.delete_note(self.alice, self.note.pk)

        again, created = self.create()
        self.assertEqual((again.pk, created), (first.pk, False))

    def test_a_key_reused_for_another_note_is_refused(self):
        other = services.create_note(self.alice, content=doc("something else"))
        self.create()

        with self.assertRaises(IdempotencyKeyReused):
            self.create(note=other)
        self.assertEqual(FormatJob.objects.count(), 1)
        self.assertEqual(limits.usage(self.alice, "format")["used"], 1)

    def test_keys_are_per_user(self):
        bob = make_user("bob")
        bobs = services.create_note(bob, content=doc("bob's note"))

        mine, _ = self.create("shared-key")
        theirs, created = self.create("shared-key", note=bobs, user=bob)

        self.assertTrue(created)
        self.assertNotEqual(mine.pk, theirs.pk)

    def test_another_users_note_does_not_exist(self):
        bob = make_user("bob")

        with self.assertRaises(Note.DoesNotExist):
            self.create(user=bob)
        self.assertFalse(FormatJob.objects.exists())
        self.assertFalse(UsageEvent.objects.exists())

    def test_another_users_key_never_replays_their_job(self):
        bob = make_user("bob")
        self.create("k")

        with self.assertRaises(Note.DoesNotExist):
            self.create("k", user=bob)

    def test_a_deleted_note_does_not_exist(self):
        services.delete_note(self.alice, self.note.pk)

        with self.assertRaises(Note.DoesNotExist):
            self.create()
        self.assertFalse(UsageEvent.objects.exists())

    def test_an_empty_note_is_refused_without_using_the_limit(self):
        for content in (None, doc(para_empty())):
            blank = services.create_note(self.alice, content=content)
            with self.assertRaises(NothingToFormat):
                self.create(note=blank)
        self.assertFalse(UsageEvent.objects.exists())

    @override_settings(FORMAT_MAX_INPUT_CHARS=60)
    def test_a_note_too_long_is_refused_without_using_the_limit(self):
        with self.assertRaises(NoteTooLong):
            self.create()
        self.assertFalse(UsageEvent.objects.exists())
        self.assertFalse(FormatJob.objects.exists())

    def test_over_the_limit_raises_and_leaves_nothing_behind(self):
        self.create("a")
        self.create("b")

        with (
            mock.patch(DELAY) as delay,
            self.captureOnCommitCallbacks(execute=True),
            self.assertRaises(limits.UserLimitExceeded) as caught,
        ):
            self.create("c")

        self.assertEqual((caught.exception.used, caught.exception.limit), (2, 2))
        self.assertEqual(FormatJob.objects.count(), 2)
        self.assertEqual(UsageEvent.objects.count(), 2)
        delay.assert_not_called()

    def test_a_failed_job_frees_its_use(self):
        job, _ = self.create("a")
        self.create("b")
        limits.refund(job.usage_event)

        _, created = self.create("c")
        self.assertTrue(created)

    def test_the_limit_follows_the_committed_plan(self):
        self.create("a")
        self.create("b")
        type(self.alice).objects.filter(pk=self.alice.pk).update(plan="premium")

        # self.alice is stale ("free"); the locked copy is not.
        _, created = self.create("c")
        self.assertTrue(created)

    @override_settings(LIMIT_DEFAULTS=format_limit(5, 5, system=1))
    def test_the_system_limit_passes_through(self):
        self.create("a")

        with self.assertRaises(limits.SystemLimitExceeded):
            self.create("b", user=self.alice)
        self.assertEqual(FormatJob.objects.count(), 1)


def para_empty():
    return {"type": "paragraph"}
