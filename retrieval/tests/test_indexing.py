"""retrieval/indexing.py and tasks.py: what is embedded, reused, and dropped."""

import logging
from unittest import mock

from django.test import TestCase, override_settings

from notes import services
from notes.models import Note
from notes.tests.helpers import doc, heading, make_user, para
from retrieval import indexing
from retrieval.embeddings import EmbeddingError, EmbeddingTransientError, embed_texts
from retrieval.tasks import index_note_task


def sections(*pairs):
    """A document of (heading, body) sections: one chunk each."""
    blocks = []
    for title, body in pairs:
        blocks += [heading(title), para(body)]
    return {"type": "doc", "content": blocks}


def spy():
    """Patch the boundary as indexing.py sees it; wraps the real (fake) one."""
    return mock.patch.object(indexing, "embed_texts", side_effect=embed_texts)


class IndexNoteTests(TestCase):
    def setUp(self):
        self.user = make_user("alice")
        self.note = services.create_note(
            self.user,
            title="Plan",
            content=sections(("Goals", "ship it"), ("Risks", "slipping"), ("Next", "review")),
        )

    def index(self, note=None):
        note = note or Note.objects.get(pk=self.note.pk)
        indexing.index_note(note.pk, note.version)

    def edit(self, **changes):
        note = Note.objects.get(pk=self.note.pk)
        return services.update_note(self.user, note.pk, expected_version=note.version, **changes)

    def test_indexing_creates_one_chunk_per_section_owned_by_the_owner(self):
        self.index()

        chunks = list(self.note.chunks.all())
        self.assertEqual([c.heading_path for c in chunks], ["Goals", "Risks", "Next"])
        self.assertEqual([c.ordinal for c in chunks], [0, 1, 2])
        self.assertEqual({c.owner_id for c in chunks}, {self.user.pk})
        self.assertEqual({c.note_version for c in chunks}, {1})
        self.assertEqual({c.embedding_model for c in chunks}, {"fake@1536"})

    def test_an_edit_embeds_only_the_changed_chunk(self):
        self.index()
        old = {c.heading_path: c.pk for c in self.note.chunks.all()}
        self.edit(
            content=sections(("Goals", "ship it"), ("Risks", "slipping badly"), ("Next", "review"))
        )

        with spy() as embed:
            self.index()

        (texts,), _ = embed.call_args
        self.assertEqual(len(texts), 1)
        self.assertIn("slipping badly", texts[0])
        chunks = {c.heading_path: c for c in self.note.chunks.all()}
        self.assertEqual(chunks["Goals"].pk, old["Goals"])  # reused rows, not rewritten
        self.assertEqual({c.note_version for c in chunks.values()}, {2})
        self.assertEqual(chunks["Risks"].text, "slipping badly")

    def test_reordered_sections_reuse_embeddings_and_renumber(self):
        self.index()
        self.edit(content=sections(("Next", "review"), ("Goals", "ship it"), ("Risks", "slipping")))

        with spy() as embed:
            self.index()

        embed.assert_not_called()
        self.assertEqual(
            [c.heading_path for c in self.note.chunks.order_by("ordinal")],
            ["Next", "Goals", "Risks"],
        )

    def test_removed_section_is_deleted(self):
        self.index()
        self.edit(content=sections(("Goals", "ship it")))

        self.index()

        self.assertEqual(self.note.chunks.count(), 1)

    def test_unchanged_reindex_makes_no_embedding_call(self):
        self.index()

        with spy() as embed:
            self.index()

        embed.assert_not_called()
        self.assertEqual(self.note.chunks.count(), 3)

    def test_model_change_reembeds_everything(self):
        self.index()

        with (
            override_settings(EMBEDDING_DIMENSIONS=1536, EMBEDDING_MODELS={"fake": "v2"}),
            spy() as embed,
        ):
            self.index()

        self.assertEqual(len(embed.call_args.args[0]), 3)
        self.assertEqual(
            set(self.note.chunks.values_list("embedding_model", flat=True)), {"fake/v2@1536"}
        )
        self.assertEqual(self.note.chunks.count(), 3)

    def test_repeated_paragraphs_each_keep_a_chunk_and_stay_stable(self):
        repeated = {"type": "doc", "content": [para("same"), heading("H"), para("same")]}
        note = services.create_note(self.user, title="Echo", content=repeated)
        self.index(note)
        first = list(note.chunks.values_list("pk", "ordinal"))
        self.assertEqual(len(first), 2)

        with spy() as embed:
            self.index(note)

        embed.assert_not_called()
        self.assertEqual(list(note.chunks.values_list("pk", "ordinal")), first)

    def test_duplicate_hashes_in_one_call_are_embedded_once(self):
        chunk = mock.Mock(content_hash="h", embed_text="t", ordinal=0, text="t", heading_path="")
        other = mock.Mock(content_hash="h", embed_text="t", ordinal=1, text="t", heading_path="")
        with (
            mock.patch.object(indexing, "chunk_note", return_value=[chunk, other]),
            spy() as embed,
        ):
            self.index()
        self.assertEqual(embed.call_args.args[0], ["t"])
        self.assertEqual(self.note.chunks.count(), 2)

    def test_stale_versions_are_no_ops(self):
        self.edit(title="Plan v2")  # note is at version 2

        with spy() as embed:
            indexing.index_note(self.note.pk, 1)  # older than the note
            indexing.index_note(self.note.pk, 3)  # newer than the note

        embed.assert_not_called()
        self.assertEqual(self.note.chunks.count(), 0)

    def test_missing_note_is_a_no_op(self):
        indexing.index_note(999999, 1)

    def test_delete_removes_the_chunks(self):
        self.index()
        services.delete_note(self.user, self.note.pk)

        indexing.index_note(self.note.pk, Note.objects.get(pk=self.note.pk).version)

        self.assertEqual(self.note.chunks.count(), 0)

    def test_deindex_counts_what_it_removed(self):
        self.index()
        self.assertEqual(indexing.deindex_note(self.note.pk), 3)
        self.assertEqual(indexing.deindex_note(self.note.pk), 0)

    def test_edit_during_embedding_writes_nothing(self):
        self.index()
        self.edit(content=sections(("Goals", "changed")))  # version 2

        def edit_meanwhile(texts):
            # A third write lands while we are "on the network".
            self.edit(title="Plan again")
            return embed_texts(texts)

        with mock.patch.object(indexing, "embed_texts", side_effect=edit_meanwhile):
            indexing.index_note(self.note.pk, 2)

        self.assertEqual({c.note_version for c in self.note.chunks.all()}, {1})

    def test_note_with_no_content_ends_with_no_chunks(self):
        self.index()
        self.edit(content={"type": "doc", "content": []}, title="")

        self.index()

        self.assertEqual(self.note.chunks.count(), 0)

    def test_lost_chunk_is_recreated_from_the_earlier_read(self):
        self.index()
        real = indexing._write_chunks

        def lose_one(*args):
            self.note.chunks.filter(ordinal=1).delete()
            return real(*args)

        with mock.patch.object(indexing, "_write_chunks", side_effect=lose_one), spy() as embed:
            self.index()

        embed.assert_not_called()
        self.assertEqual(self.note.chunks.count(), 3)


class IndexTaskTests(TestCase):
    def setUp(self):
        self.user = make_user("alice")
        self.note = services.create_note(self.user, title="T", content=doc("hello"))

    def test_task_indexes(self):
        index_note_task(self.note.pk, self.note.version)
        self.assertEqual(self.note.chunks.count(), 1)

    def test_permanent_error_is_logged_and_not_raised(self):
        with (
            mock.patch("retrieval.tasks.index_note", side_effect=EmbeddingError("bad key")),
            self.assertLogs("retrieval.tasks", logging.ERROR),
        ):
            index_note_task(self.note.pk, self.note.version)

    def test_transient_error_is_retried(self):
        calls = mock.Mock(side_effect=[EmbeddingTransientError("429"), None])
        with (
            mock.patch("retrieval.tasks.index_note", calls),
            mock.patch.object(
                index_note_task, "retry", side_effect=RuntimeError("retried")
            ) as retry,
        ):
            with self.assertRaises(RuntimeError):
                index_note_task(self.note.pk, self.note.version)
        retry.assert_called_once()

    def test_task_is_configured_to_retry_transient_errors_only(self):
        self.assertEqual(index_note_task.autoretry_for, (EmbeddingTransientError,))
        self.assertEqual(index_note_task.max_retries, 5)


class WriteHookTests(TestCase):
    def setUp(self):
        self.user = make_user("alice")

    def patched(self):
        return mock.patch("retrieval.tasks.index_note_task.apply_async")

    @override_settings(INDEX_DEBOUNCE_SECONDS=20)
    def test_writes_enqueue_on_commit_with_the_debounce(self):
        with self.patched() as enqueue, self.captureOnCommitCallbacks(execute=True):
            note = services.create_note(self.user, title="a")
            enqueue.assert_not_called()  # not before the commit
        enqueue.assert_called_once_with((note.pk, 1), countdown=20)

        with self.patched() as enqueue, self.captureOnCommitCallbacks(execute=True):
            services.update_note(self.user, note.pk, expected_version=1, title="b")
        enqueue.assert_called_once_with((note.pk, 2), countdown=20)

    @override_settings(INDEX_DEBOUNCE_SECONDS=20)
    def test_a_delete_is_enqueued_at_once(self):
        note = services.create_note(self.user, title="a")
        with self.patched() as enqueue, self.captureOnCommitCallbacks(execute=True):
            services.delete_note(self.user, note.pk)
        enqueue.assert_called_once_with((note.pk, 2), countdown=0)

    def test_a_rolled_back_write_enqueues_nothing(self):
        note = services.create_note(self.user, title="a")
        with self.patched() as enqueue, self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(services.VersionConflict):
                services.update_note(self.user, note.pk, expected_version=9, title="b")
        enqueue.assert_not_called()

    def test_end_to_end_eager_write_indexes_and_delete_deindexes(self):
        with self.captureOnCommitCallbacks(execute=True):
            note = services.create_note(self.user, title="a", content=doc("hello"))
        self.assertEqual(note.chunks.count(), 1)

        with self.captureOnCommitCallbacks(execute=True):
            services.delete_note(self.user, note.pk)
        self.assertEqual(note.chunks.count(), 0)
