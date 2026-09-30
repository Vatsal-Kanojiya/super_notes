"""retrieval/search.py: fusion, the per-note cap, and who can see what.

The fake provider is a hashed bag of words (D39), so notes sharing words
with the query rank higher; assertions stay on orderings and membership.
"""

from unittest import mock

from django.db import connection, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from notes import services
from notes.tests.helpers import doc, heading, make_user, para
from retrieval import search as search_module
from retrieval.embeddings import (
    EmbeddingError,
    EmbeddingTransientError,
    embed_query,
    embed_texts,
    embedding_model_id,
)
from retrieval.indexing import index_note
from retrieval.models import NoteChunk
from retrieval.search import cap_per_note, fuse, keyword_queryset, search, vector_queryset

MODES = ("hybrid", "vector", "keyword")


def write(owner, title, *sections):
    """Create and index a note of (heading, body) sections, one chunk each."""
    blocks = []
    for name, body in sections:
        blocks += [heading(name), para(body)]
    note = services.create_note(owner, title=title, content={"type": "doc", "content": blocks})
    # Straight to the indexer: TestCase never runs the on-commit task.
    index_note(note.pk, note.version)
    return note


class FuseTests(SimpleTestCase):
    def test_hand_built_example(self):
        vector = [1, 2, 3]
        keyword = [3, 1, 4]

        fused = fuse([vector, keyword], rrf_k=60)

        # 1: 1/61 + 1/62; 3: 1/63 + 1/61; 2: 1/62; 4: 1/63.
        self.assertEqual([chunk for chunk, _ in fused], [1, 3, 2, 4])
        scores = dict(fused)
        self.assertAlmostEqual(scores[1], 1 / 61 + 1 / 62)
        self.assertAlmostEqual(scores[3], 1 / 61 + 1 / 63)
        self.assertAlmostEqual(scores[2], 1 / 62)
        self.assertAlmostEqual(scores[4], 1 / 63)

    def test_agreement_beats_a_single_first_place(self):
        # 9 is first in one list only; 5 is second in both.
        fused = fuse([[9, 5], [7, 5]], rrf_k=60)
        self.assertEqual(fused[0][0], 5)

    def test_ties_go_to_the_lower_chunk_id(self):
        self.assertEqual([c for c, _ in fuse([[7], [5]], rrf_k=60)], [5, 7])
        self.assertEqual([c for c, _ in fuse([[5], [7]], rrf_k=60)], [5, 7])

    def test_empty_lists(self):
        self.assertEqual(fuse([[], []], rrf_k=60), [])

    def test_cap_per_note_keeps_fused_order(self):
        fused = [(1, 0.9), (2, 0.8), (3, 0.7), (4, 0.6), (5, 0.5)]
        note_of = {1: 10, 2: 10, 3: 10, 4: 20, 5: 10}

        self.assertEqual([c for c, _ in cap_per_note(fused, note_of, max_per_note=2)], [1, 2, 4])


class SearchTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.bob = make_user("bob")

    def test_another_users_identical_note_never_appears(self):
        mine = write(self.alice, "Passport", ("Renewal", "renew the passport before the trip"))
        # Bob's copy is word for word the same, plus one that matches better.
        write(self.bob, "Passport", ("Renewal", "renew the passport before the trip"))
        write(self.bob, "Passport passport", ("Passport", "passport passport renew trip"))

        for mode in MODES:
            with self.subTest(mode=mode):
                hits = search(self.alice, "renew passport trip", mode=mode)
                self.assertEqual({hit.note_id for hit in hits}, {mine.pk})

    def test_a_user_with_no_notes_gets_nothing_from_anyone(self):
        write(self.bob, "Passport", ("Renewal", "renew the passport"))
        for mode in MODES:
            with self.subTest(mode=mode):
                self.assertEqual(search(self.alice, "passport", mode=mode), [])

    def test_hybrid_ranks_the_matching_note_first_and_reports_both_legs(self):
        write(self.alice, "Groceries", ("List", "milk eggs bread"))
        target = write(self.alice, "Passport", ("Renewal", "renew the passport before the trip"))

        hits = search(self.alice, "passport renewal")

        self.assertEqual(hits[0].note_id, target.pk)
        self.assertEqual(hits[0].title, "Passport")
        self.assertEqual(hits[0].heading_path, "Renewal")
        self.assertIn("passport", hits[0].text)
        self.assertGreater(hits[0].similarity, 0)
        self.assertGreater(hits[0].keyword_rank, 0)
        # The groceries chunk is a vector-only neighbour: no keyword match.
        groceries = [hit for hit in hits if hit.note_id != target.pk]
        self.assertIsNone(groceries[0].keyword_rank)
        self.assertEqual([hit.score for hit in hits], sorted((h.score for h in hits), reverse=True))

    def test_keyword_mode_has_no_similarity_and_vector_mode_no_keyword_rank(self):
        write(self.alice, "Passport", ("Renewal", "renew the passport"))

        (keyword_hit,) = search(self.alice, "passport", mode="keyword")
        (vector_hit,) = search(self.alice, "passport", mode="vector")

        self.assertIsNone(keyword_hit.similarity)
        self.assertIsNone(vector_hit.keyword_rank)

    def test_chunks_per_note_are_capped(self):
        sections = [(f"Part {n}", f"budget review item {n}") for n in range(4)]
        long_note = write(self.alice, "Budget", *sections)
        other = write(self.alice, "Other", ("Notes", "budget"))

        hits = search(self.alice, "budget review", k=8)
        from_long = [hit for hit in hits if hit.note_id == long_note.pk]
        self.assertEqual(len(from_long), 2)
        self.assertIn(other.pk, {hit.note_id for hit in hits})

        with override_settings(SEARCH_MAX_CHUNKS_PER_NOTE=3):
            hits = search(self.alice, "budget review", k=8)
        self.assertEqual(len([hit for hit in hits if hit.note_id == long_note.pk]), 3)

    def test_k_limits_the_result(self):
        for n in range(5):
            write(self.alice, f"Note {n}", ("Body", f"budget {n}"))
        self.assertEqual(len(search(self.alice, "budget", k=3)), 3)

    def test_deleted_notes_are_excluded_even_before_deindexing(self):
        gone = write(self.alice, "Passport", ("Renewal", "renew the passport"))
        # The de-index task runs on commit, which TestCase never reaches, so
        # the chunks are still there: only the SQL filter hides them.
        services.delete_note(self.alice, gone.pk)
        self.assertTrue(NoteChunk.objects.filter(note=gone).exists())

        for mode in MODES:
            with self.subTest(mode=mode):
                self.assertEqual(search(self.alice, "passport", mode=mode), [])

    def test_chunks_from_another_embedding_model_are_left_out_of_the_vector_leg(self):
        note = write(self.alice, "Passport", ("Renewal", "renew the passport"))
        NoteChunk.objects.filter(note=note).update(embedding_model="other/model@1536")

        self.assertEqual(search(self.alice, "passport", mode="vector"), [])
        self.assertEqual(len(search(self.alice, "passport", mode="keyword")), 1)

    def test_blank_query_returns_nothing_without_touching_the_database(self):
        for query in ("", "   ", "\n\t"):
            with self.subTest(query=query), self.assertNumQueries(0):
                self.assertEqual(search(self.alice, query), [])

    def test_bad_k_or_mode_raise(self):
        for k in (0, -1, 21):
            with self.subTest(k=k), self.assertRaises(ValueError):
                search(self.alice, "x", k=k)
        with self.assertRaises(ValueError):
            search(self.alice, "x", mode="semantic")

    def test_embedding_failure_falls_back_to_keyword_in_hybrid_only(self):
        write(self.alice, "Passport", ("Renewal", "renew the passport"))

        for error in (EmbeddingError("bad"), EmbeddingTransientError("rate limited")):
            with self.subTest(error=type(error).__name__):
                with (
                    mock.patch.object(search_module, "embed_query", side_effect=error),
                    self.assertLogs("retrieval.search", "WARNING"),
                ):
                    (hit,) = search(self.alice, "passport")
                self.assertIsNone(hit.similarity)
                self.assertIsNotNone(hit.keyword_rank)

                with (
                    mock.patch.object(search_module, "embed_query", side_effect=error),
                    self.assertRaises(type(error)),
                ):
                    search(self.alice, "passport", mode="vector")


class SearchSqlTests(TestCase):
    """The owner filter and the indexes, as Postgres sees them."""

    def setUp(self):
        self.alice = make_user("alice")
        for n in range(20):
            write(self.alice, f"Note {n}", ("Body", f"filler words {n}"))

    def test_the_vector_leg_filters_owner_in_sql_and_raises_ef_search(self):
        with CaptureQueriesContext(connection) as queries:
            search(self.alice, "filler", mode="vector")

        sql = [q["sql"] for q in queries.captured_queries]
        (vector_sql,) = [s for s in sql if "<=>" in s]
        self.assertIn(f'"retrieval_notechunk"."owner_id" = {self.alice.pk}', vector_sql)
        self.assertIn('"notes_note"."deleted_at" IS NULL', vector_sql)
        self.assertTrue(any("hnsw.ef_search" in s for s in sql))

    def test_the_keyword_leg_filters_owner_in_sql(self):
        with CaptureQueriesContext(connection) as queries:
            search(self.alice, "filler", mode="keyword")

        (keyword_sql,) = [q["sql"] for q in queries.captured_queries if "@@" in q["sql"]]
        self.assertIn(f'"retrieval_notechunk"."owner_id" = {self.alice.pk}', keyword_sql)

    def test_the_vector_query_orders_by_distance_alone(self):
        # pgvector's index serves only ORDER BY <distance> LIMIT n; any
        # second sort key would force a full scan and sort.
        with CaptureQueriesContext(connection) as queries:
            search(self.alice, "filler", mode="vector")

        (vector_sql,) = [q["sql"] for q in queries.captured_queries if "<=>" in q["sql"]]
        order_by = vector_sql[vector_sql.index("ORDER BY") :]
        self.assertRegex(order_by, r"^ORDER BY [^,]+ ASC LIMIT 50$")


class IndexUseTests(TestCase):
    """Both legs can use their index, on a table big enough to make it worth it.

    On a handful of rows every plan is cheap and Postgres picks the owner
    btree, so these build one owner with 1000 chunks (directly, not through
    the indexer), give Postgres statistics, and nudge it off sequential
    scans. A user with few chunks gets the owner-index scan in production
    too, which is exact and cheaper (D68).
    """

    def setUp(self):
        self.alice = make_user("alice")
        notes = [
            services.create_note(self.alice, title=f"n{n}", content=doc("x")) for n in range(20)
        ]
        vectors = embed_texts([f"word{n} filler{n % 7}" for n in range(50)])
        NoteChunk.objects.bulk_create(
            NoteChunk(
                note=notes[n % 20],
                owner=self.alice,
                ordinal=n,
                text="zeppelin" if n == 0 else f"filler words {n}",
                content_hash=str(n),
                embedding=vectors[n % 50],
                embedding_model=embedding_model_id(),
                note_version=1,
            )
            for n in range(1000)
        )

    def explain(self, queryset, *settings):
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("ANALYZE retrieval_notechunk")
            cursor.execute("ANALYZE notes_note")
            cursor.execute("SET LOCAL enable_seqscan = off")
            for setting in settings:
                cursor.execute(setting)
            return queryset.explain()

    def test_the_keyword_query_can_use_the_gin_index(self):
        # As in notes/tests/test_search.py: with plain index scans off too,
        # only a query building the indexed expression reaches the GIN
        # index (GIN supports bitmap scans only).
        plan = self.explain(
            keyword_queryset(self.alice, "zeppelin"), "SET LOCAL enable_indexscan = off"
        )
        self.assertIn("Bitmap Index Scan on chunk_fts", plan)

    def test_the_vector_query_can_use_the_hnsw_index(self):
        plan = self.explain(vector_queryset(self.alice, embed_query("filler"))[:50])
        self.assertIn("Index Scan using chunk_embedding_hnsw", plan)
