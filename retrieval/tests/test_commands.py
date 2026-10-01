"""reindex_notes, index_status and eval_retrieval."""

import re
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from assistant.chat import ChatError
from limits.models import UsageEvent
from notes import services
from notes.tests.helpers import doc, make_user
from retrieval import indexing
from retrieval.embeddings import EmbeddingError
from retrieval.models import NoteChunk


def run(name, *args, **options):
    out, err = StringIO(), StringIO()
    call_command(name, *args, stdout=out, stderr=err, **options)
    return out.getvalue(), err.getvalue()


class ReindexNotesTests(TestCase):
    def setUp(self):
        self.alice, self.bob = make_user("alice"), make_user("bob")
        self.a = services.create_note(self.alice, title="a", content=doc("one"))
        self.b = services.create_note(self.bob, title="b", content=doc("two"))
        gone = services.create_note(self.alice, title="gone", content=doc("three"))
        services.delete_note(self.alice, gone.pk)

    def test_requires_exactly_one_scope(self):
        with self.assertRaises(CommandError):
            run("reindex_notes")
        with self.assertRaises(CommandError):
            run("reindex_notes", "--all", user="alice@example.com")

    def test_unknown_user_is_an_error(self):
        with self.assertRaises(CommandError):
            run("reindex_notes", user="nobody@example.com")

    def test_sync_indexes_one_users_live_notes(self):
        out, _ = run("reindex_notes", user="alice@example.com", sync=True)

        self.assertEqual(out.strip(), "Indexed 1 notes, 0 failed.")
        self.assertEqual(set(NoteChunk.objects.values_list("note_id", flat=True)), {self.a.pk})

    def test_sync_all(self):
        out, _ = run("reindex_notes", "--all", "--sync")

        self.assertEqual(out.strip(), "Indexed 2 notes, 0 failed.")
        self.assertEqual(NoteChunk.objects.count(), 2)

    def test_sync_reports_failures_and_carries_on(self):
        real = indexing.index_note

        def flaky(note_id, version):
            if note_id == self.a.pk:
                raise EmbeddingError("bad key")
            return real(note_id, version)

        with mock.patch("retrieval.management.commands.reindex_notes.index_note", flaky):
            out, err = run("reindex_notes", "--all", "--sync")

        self.assertEqual(out.strip(), "Indexed 1 notes, 1 failed.")
        self.assertIn(f"Note {self.a.pk}: bad key", err)

    def test_without_sync_it_queues_a_task_per_note(self):
        with mock.patch("retrieval.management.commands.reindex_notes.index_note_task") as task:
            out, _ = run("reindex_notes", "--all")

        self.assertEqual(out.strip(), "Queued 2 notes.")
        task.delay.assert_has_calls(
            [mock.call(self.a.pk, 1), mock.call(self.b.pk, 1)], any_order=True
        )


def ready_attachment(note, text="The boiler was serviced in March."):
    """A ready attachment row with its text, indexed (no file: nothing reads it here)."""
    from notes.models import Attachment

    attachment = Attachment.objects.create(
        owner_id=note.owner_id,
        note=note,
        original_name="report.pdf",
        mime_type="application/pdf",
        size=10,
        sha256=f"{note.pk:064d}",
        status=Attachment.Status.READY,
        extracted_text=text,
    )
    indexing.index_attachment(attachment.pk)
    return attachment


class AttachmentCommandTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.note = services.create_note(self.alice, title="House", content=doc("fence"))
        indexing.index_note(self.note.pk, self.note.version)
        self.attachment = ready_attachment(self.note)

    def test_index_status_counts_attachment_chunks_apart(self):
        # The note moves on; its file's chunks do not follow its version.
        services.update_note(self.alice, self.note.pk, expected_version=1, title="House 2")
        NoteChunk.objects.filter(attachment=self.attachment).update(embedding_model="old@1536")

        out, _ = run("index_status")

        self.assertIn("behind the note's version: 1", out)
        self.assertIn("another embedding model: 0", out)
        self.assertIn("Attachment chunks: 1 (1 from another embedding model)", out)

    def test_reindex_sync_re_embeds_ready_attachments(self):
        NoteChunk.objects.filter(attachment=self.attachment).update(embedding_model="old@1536")

        out, _ = run("reindex_notes", "--all", "--sync")

        self.assertIn("Indexed 1 notes, 0 failed.", out)
        self.assertIn("Indexed 1 attachments, 0 failed.", out)
        chunks = NoteChunk.objects.filter(attachment=self.attachment)
        self.assertEqual(set(chunks.values_list("embedding_model", flat=True)), {"fake@1536"})

    def test_reindex_sync_reports_attachment_failures(self):
        with mock.patch(
            "retrieval.management.commands.reindex_notes.index_attachment",
            side_effect=EmbeddingError("bad key"),
        ):
            out, err = run("reindex_notes", "--all", "--sync")

        self.assertIn("Indexed 0 attachments, 1 failed.", out)
        self.assertIn(f"Attachment {self.attachment.pk}: bad key", err)

    def test_reindex_queues_a_task_per_attachment(self):
        with (
            mock.patch("retrieval.management.commands.reindex_notes.index_note_task"),
            mock.patch("retrieval.management.commands.reindex_notes.index_attachment_task") as task,
        ):
            out, _ = run("reindex_notes", user="alice@example.com")

        self.assertEqual(out.strip(), "Queued 1 notes and 1 attachments.")
        task.delay.assert_called_once_with(self.attachment.pk)

    def test_index_attachment_skips_one_not_ready_or_deleted(self):
        from django.utils import timezone

        self.attachment.status = "failed"
        self.attachment.save()
        self.assertFalse(indexing.index_attachment(self.attachment.pk))
        self.attachment.status, self.attachment.deleted_at = "ready", timezone.now()
        self.attachment.save()
        self.assertFalse(indexing.index_attachment(self.attachment.pk))


class ChunkSourceConstraintTests(TestCase):
    def test_an_attachment_chunk_needs_its_attachment_and_only_it_has_one(self):
        from django.db import IntegrityError, transaction

        alice = make_user("alice")
        note = services.create_note(alice, title="n", content=doc("text"))
        indexing.index_note(note.pk, note.version)
        attachment = ready_attachment(note)
        own = NoteChunk.objects.get(note=note, source="note")

        with self.assertRaises(IntegrityError), transaction.atomic():
            NoteChunk.objects.filter(pk=own.pk).update(source="attachment")
        with self.assertRaises(IntegrityError), transaction.atomic():
            NoteChunk.objects.filter(pk=own.pk).update(attachment=attachment)


class IndexStatusTests(TestCase):
    def setUp(self):
        self.user = make_user("alice")

    def status(self):
        out, _ = run("index_status")
        return out

    def test_empty(self):
        out = self.status()
        self.assertIn("Live notes: 0", out)
        self.assertIn("Orphan chunks of deleted notes: 0 (0 notes)", out)

    def test_counts_each_kind_of_lag(self):
        fresh = services.create_note(self.user, title="fresh", content=doc("a"))
        behind = services.create_note(self.user, title="behind", content=doc("b"))
        services.create_note(self.user, title="never indexed", content=doc("c"))
        old_model = services.create_note(self.user, title="old", content=doc("d"))
        deleted = services.create_note(self.user, title="deleted", content=doc("e"))
        for note in (fresh, behind, old_model, deleted):
            indexing.index_note(note.pk, note.version)
        services.update_note(self.user, behind.pk, expected_version=1, title="behind 2")
        NoteChunk.objects.filter(note=old_model).update(embedding_model="other@1536")
        services.delete_note(self.user, deleted.pk)

        out = self.status()

        self.assertIn("Embedding model: fake@1536", out)
        self.assertIn("Live notes: 4", out)
        self.assertIn("without chunks: 1", out)
        self.assertIn("behind the note's version: 1", out)
        self.assertIn("another embedding model: 1", out)
        self.assertIn("Orphan chunks of deleted notes: 1 (1 notes)", out)

    @override_settings(EMBEDDING_MODELS={"fake": "v2"})
    def test_a_model_change_makes_every_note_stale(self):
        note = services.create_note(self.user, title="n", content=doc("a"))
        with override_settings(EMBEDDING_MODELS={}):
            indexing.index_note(note.pk, note.version)

        self.assertIn("another embedding model: 1", self.status())


class EvalRetrievalTests(TestCase):
    """A smoke test with the fake provider: the shape of the report, not its numbers."""

    def test_reports_every_mode_and_leaves_no_data(self):
        users_before = get_user_model().objects.count()

        out, _ = run("eval_retrieval", k=3, by_kind=True)

        self.assertIn("Fake provider: a smoke test", out)
        self.assertIn("30 notes, 31 questions (28 answerable, 3 no-answer). k=3.", out)
        for mode in ("vector", "keyword", "hybrid"):
            self.assertRegex(out, rf"\n{mode} +(\d\.\d{{3}}|-) +(\d\.\d{{3}}|-)\n")
        self.assertRegex(out, r"\nparaphrase +6 ")
        self.assertRegex(out, r"q29  \d\.\d{3}  \(no answer\)")
        self.assertEqual(get_user_model().objects.count(), users_before)
        self.assertFalse(NoteChunk.objects.exists())

    def test_provider_override_and_bad_k(self):
        with self.assertRaises(CommandError):
            run("eval_retrieval", k=0)
        # openai with no key: the embedding boundary refuses, and the
        # command says so instead of printing numbers.
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": ""}), self.assertRaises(CommandError):
            run("eval_retrieval", k=3, provider="openai")
        self.assertFalse(NoteChunk.objects.exists())


class EvalConversationsTests(TestCase):
    """--conversations with the fake providers: the report's shape, and what it leaves behind."""

    def test_reports_raw_condensed_and_standalone_and_leaves_no_data(self):
        users_before = get_user_model().objects.count()

        out, err = run("eval_retrieval", conversations=True, k=5, by_kind=True)

        self.assertIn(
            "30 notes, 17 conversations (15 answerable, 2 no-answer). Hybrid search, k=5.", out
        )
        self.assertIn("Fake provider: a smoke test", out)
        self.assertIn("Last turns: 12 condensed, 5 stood alone, 0 fell back.", out)
        for variant in ("raw", "condensed", "standalone"):
            self.assertRegex(out, rf"\n{variant} +\d\.\d{{3}} +\d\.\d{{3}}\n")
        self.assertRegex(out, r"\npronoun +3 ")
        self.assertRegex(out, r"\ntopic_shift +3 ")
        self.assertNotIn("no_answer  ", out)
        self.assertEqual(err, "")
        self.assertEqual(get_user_model().objects.count(), users_before)
        self.assertFalse(NoteChunk.objects.exists())

    def test_condensing_in_the_eval_needs_no_ask_and_consumes_no_limit(self):
        run("eval_retrieval", conversations=True)

        self.assertFalse(UsageEvent.objects.exists())

    def test_condensing_helps_the_fake_pronoun_follow_ups(self):
        out, _ = run("eval_retrieval", conversations=True, k=5)

        def mrr(variant):
            return float(re.search(rf"\n{variant} +\d\.\d{{3}} +(\d\.\d{{3}})\n", out).group(1))

        self.assertGreater(mrr("condensed"), mrr("raw"))
        self.assertGreaterEqual(mrr("standalone"), mrr("condensed"))

    def test_a_condenser_that_fails_falls_back_to_the_follow_up_as_asked(self):
        with mock.patch("assistant.chat.complete", side_effect=ChatError("down")):
            out, err = run("eval_retrieval", conversations=True)

        self.assertIn("Last turns: 0 condensed, 5 stood alone, 12 fell back.", out)
        self.assertEqual(err.count("condensing failed, searched as asked"), 12)
        # Everything fell back to the raw question: condensed scores as raw does.
        raw = re.search(r"\nraw +(\d\.\d{3} +\d\.\d{3})\n", out).group(1)
        condensed = re.search(r"\ncondensed +(\d\.\d{3} +\d\.\d{3})\n", out).group(1)
        self.assertEqual(raw, condensed)

    def test_bad_k_and_a_failing_embedding_provider_are_command_errors(self):
        with self.assertRaises(CommandError):
            run("eval_retrieval", conversations=True, k=0)
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": ""}), self.assertRaises(CommandError):
            run("eval_retrieval", conversations=True, provider="openai")
        self.assertFalse(NoteChunk.objects.exists())
