"""notes/services.py: revisions, versions, conflicts, tombstones."""

from unittest import mock

from django.test import TestCase

from notes import services
from notes.models import Note

from .helpers import checklist_doc, doc, make_user


def revision_of(user):
    user.refresh_from_db(fields=["notes_revision"])
    return user.notes_revision


class CreateTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")

    def test_create_stamps_the_next_revision_and_version_one(self):
        first = services.create_note(self.alice, title="one")
        second = services.create_note(self.alice, title="two")

        self.assertEqual((first.revision, second.revision), (1, 2))
        self.assertEqual((first.version, second.version), (1, 1))
        self.assertEqual(revision_of(self.alice), 2)

    def test_create_derives_content_text(self):
        note = services.create_note(
            self.alice, type=Note.Type.CHECKLIST, content=checklist_doc(("milk", True))
        )
        self.assertEqual(note.content_text, "- [x] milk")

    def test_create_without_content_is_an_empty_document(self):
        note = services.create_note(self.alice)
        self.assertEqual(note.content, {"type": "doc", "content": []})
        self.assertEqual(note.content_text, "")

    def test_revisions_are_per_user(self):
        bob = make_user("bob")
        services.create_note(self.alice)
        note = services.create_note(bob)

        self.assertEqual(note.revision, 1)
        self.assertEqual(revision_of(self.alice), 1)

    def test_after_write_runs_for_every_write(self):
        with mock.patch.object(services, "_after_write") as hook:
            note = services.create_note(self.alice)
            services.update_note(self.alice, note.pk, expected_version=1, title="t")
            services.delete_note(self.alice, note.pk)

        self.assertEqual(hook.call_count, 3)
        self.assertIsNotNone(hook.call_args.args[0].deleted_at)


class UpdateTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.note = services.create_note(self.alice, title="old", content=doc("old body"))

    def test_update_bumps_version_and_revision_and_rederives_text(self):
        note = services.update_note(
            self.alice, self.note.pk, expected_version=1, title="new", content=doc("new body")
        )

        self.assertEqual((note.version, note.revision), (2, 2))
        self.assertEqual((note.title, note.content_text), ("new", "new body"))

    def test_stale_version_raises_with_the_current_note_and_writes_nothing(self):
        services.update_note(self.alice, self.note.pk, expected_version=1, title="first")

        with self.assertRaises(services.VersionConflict) as caught:
            services.update_note(self.alice, self.note.pk, expected_version=1, title="second")

        self.assertEqual(caught.exception.current.title, "first")
        self.assertEqual(caught.exception.current.version, 2)
        self.assertIn("version 2", str(caught.exception))
        # The refused write's revision increment was rolled back with it.
        self.assertEqual(revision_of(self.alice), 2)

    def test_someone_elses_note_does_not_exist(self):
        with self.assertRaises(Note.DoesNotExist):
            services.update_note(make_user("bob"), self.note.pk, expected_version=1, title="x")

    def test_deleted_note_does_not_exist(self):
        services.delete_note(self.alice, self.note.pk)
        with self.assertRaises(Note.DoesNotExist):
            services.update_note(self.alice, self.note.pk, expected_version=2, title="x")

    def test_only_type_title_and_content_are_editable(self):
        with self.assertRaises(TypeError):
            services.update_note(self.alice, self.note.pk, expected_version=1, revision=99)


class DeleteTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.note = services.create_note(self.alice, title="doomed", content=doc("body"))

    def test_delete_leaves_a_stamped_tombstone(self):
        tombstone = services.delete_note(self.alice, self.note.pk)

        self.assertIsNotNone(tombstone.deleted_at)
        self.assertEqual((tombstone.version, tombstone.revision), (2, 2))
        stored = Note.objects.get(pk=self.note.pk)
        self.assertEqual(stored.content_text, "body")
        self.assertIsNotNone(stored.deleted_at)

    def test_deleting_twice_is_does_not_exist(self):
        services.delete_note(self.alice, self.note.pk)
        with self.assertRaises(Note.DoesNotExist):
            services.delete_note(self.alice, self.note.pk)


class ModelTests(TestCase):
    def test_save_with_update_fields_including_content_rederives_text(self):
        note = services.create_note(make_user(), content=doc("before"))
        note.content = doc("after")
        note.save(update_fields=["content"])

        note.refresh_from_db()
        self.assertEqual(note.content_text, "after")

    def test_save_with_other_update_fields_leaves_text_alone(self):
        note = services.create_note(make_user(), content=doc("kept"))
        note.content = doc("not saved")
        note.save(update_fields=["title"])

        note.refresh_from_db()
        self.assertEqual(note.content_text, "kept")

    def test_str(self):
        note = services.create_note(make_user())
        self.assertEqual(str(note), f"Note {note.pk}")
        note.title = "Named"
        self.assertEqual(str(note), "Named")
