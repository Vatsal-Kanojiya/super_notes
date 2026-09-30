"""Hybrid retrieval over a user's chunks (plan §6.4, docs/RAG.md).

Two legs, each one SQL query scoped to the owner in its WHERE clause:

* **vector** -- the query's embedding against the HNSW cosine index;
* **keyword** -- ``websearch_to_tsquery`` against the GIN index, through
  ``chunk_search_vector()`` so the query's expression is the indexed one.

Their ranked lists are merged by reciprocal rank fusion (``fuse``), capped
per note (``cap_per_note``), cut to k, and only then are the winners' texts
read. The fusion steps are pure functions over chunk ids, so their
ordering is unit-tested without a database.

``mode`` exists for the evaluation (eval_retrieval compares the three);
everything else searches in "hybrid".
"""

import logging
from dataclasses import dataclass

from django.conf import settings
from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db import connection, transaction
from django.db.models import F
from pgvector.django import CosineDistance

from notes.search import SEARCH_CONFIG

from .embeddings import EmbeddingError, EmbeddingTransientError, embed_query, embedding_model_id
from .models import NoteChunk
from .search_vector import chunk_search_vector

logger = logging.getLogger(__name__)

MODES = ("hybrid", "vector", "keyword")


@dataclass(frozen=True)
class SearchHit:
    """One retrieved chunk.

    ``score`` is the fused RRF score: it orders hits but says nothing about
    relevance on its own (about 1/(60 + rank) whatever the text). That is
    what ``similarity`` is for -- the vector leg's cosine similarity, which
    Ask's relevance floor compares against (D58, D69). It is None when the
    chunk came from the keyword leg only; ``keyword_rank`` (``ts_rank``)
    is None when it came from the vector leg only.
    """

    chunk_id: int
    note_id: int
    title: str
    heading_path: str
    text: str
    score: float
    similarity: float | None
    keyword_rank: float | None


def search(user, query: str, k: int | None = None, *, mode: str = "hybrid") -> list[SearchHit]:
    """The user's top ``k`` chunks for ``query``, best first."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    k = settings.SEARCH_DEFAULT_K if k is None else k
    if not 1 <= k <= settings.SEARCH_MAX_K:
        raise ValueError(f"k must be between 1 and {settings.SEARCH_MAX_K}")
    query = (query or "").strip()
    if not query:
        return []

    similarity, keyword_rank = {}, {}
    notes = {}  # chunk id -> note id, for the per-note cap
    if mode != "keyword":
        try:
            rows = _vector_leg(user, embed_query(query))
        except (EmbeddingError, EmbeddingTransientError):
            if mode == "vector":
                raise
            # Search must answer while the provider is down or rate-limited;
            # the keyword leg alone is a worse ranking, not a wrong one (D70).
            logger.warning("Query embedding failed; searching by keyword only", exc_info=True)
            rows = []
        for chunk_id, note_id, value in rows:
            similarity[chunk_id], notes[chunk_id] = value, note_id
    if mode != "vector":
        for chunk_id, note_id, value in _keyword_leg(user, query):
            keyword_rank[chunk_id], notes[chunk_id] = value, note_id

    # Dicts keep insertion order, which is each leg's rank order.
    fused = fuse([list(similarity), list(keyword_rank)], settings.SEARCH_RRF_K)
    top = cap_per_note(fused, notes, settings.SEARCH_MAX_CHUNKS_PER_NOTE)[:k]
    return _hits(user, top, similarity, keyword_rank)


def fuse(rankings: list[list[int]], rrf_k: int) -> list[tuple[int, float]]:
    """Reciprocal rank fusion: each chunk scores Σ 1/(rrf_k + rank), rank from 1.

    Rank-based, so the legs' incomparable scores (cosine similarity,
    ts_rank) never have to be put on one scale. Ties go to the lower chunk
    id so equal inputs always give the same order.
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (rrf_k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def cap_per_note(
    fused: list[tuple[int, float]], note_of: dict[int, int], max_per_note: int
) -> list[tuple[int, float]]:
    """Keep at most ``max_per_note`` chunks of each note, in fused order."""
    kept, taken = [], {}
    for chunk_id, score in fused:
        note_id = note_of[chunk_id]
        if taken.get(note_id, 0) < max_per_note:
            taken[note_id] = taken.get(note_id, 0) + 1
            kept.append((chunk_id, score))
    return kept


def _live_chunks(user):
    """The owner's chunks of live notes. ``owner=user`` is SQL, never Python."""
    return NoteChunk.objects.filter(owner=user, note__deleted_at__isnull=True)


def _vector_leg(user, vector):
    """(chunk id, note id, cosine similarity), nearest first.

    HNSW applies the WHERE clause after its scan, so ``ef_search`` is
    raised for this query (SET LOCAL, hence the transaction) to leave room
    for rows the owner filter drops (D68). Equal distances are put in
    chunk-id order here, not in SQL: see ``vector_queryset``.
    """
    ef_search = max(settings.SEARCH_HNSW_EF_SEARCH, settings.SEARCH_CANDIDATES)
    queryset = vector_queryset(user, vector)[: settings.SEARCH_CANDIDATES]
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config('hnsw.ef_search', %s, true)", [str(ef_search)])
        rows = sorted(queryset, key=lambda row: (row[2], row[0]))
    return [(pk, note_id, 1.0 - distance) for pk, note_id, distance in rows]


def vector_queryset(user, vector):
    """The vector leg's query, uncut; public so a test can EXPLAIN it.

    Ordered by the distance alone: pgvector's index serves only
    ``ORDER BY embedding <=> v LIMIT n``, and a second sort key (even the
    pk) turns it into a full scan and sort. Only chunks embedded by the
    current model: another model's vectors live in another space, and a
    distance to them means nothing (D36).
    """
    return (
        _live_chunks(user)
        .filter(embedding_model=embedding_model_id())
        .annotate(distance=CosineDistance("embedding", vector))
        .order_by("distance")
        .values_list("pk", "note_id", "distance")
    )


def _keyword_leg(user, query):
    """(chunk id, note id, ts_rank), best first. An all-stopword query matches nothing."""
    return list(keyword_queryset(user, query)[: settings.SEARCH_CANDIDATES])


def keyword_queryset(user, query):
    """The keyword leg's query, uncut; public so a test can EXPLAIN it.

    ``chunk_search_vector()`` rather than a rebuilt SearchVector: the GIN
    index is on that exact expression and is used only by a query that
    builds the same one.
    """
    tsquery = SearchQuery(query, config=SEARCH_CONFIG, search_type="websearch")
    return (
        _live_chunks(user)
        .alias(search=chunk_search_vector())
        .filter(search=tsquery)
        .annotate(rank=SearchRank(F("search"), tsquery))
        .order_by("-rank", "pk")
        .values_list("pk", "note_id", "rank")
    )


def _hits(user, top, similarity, keyword_rank):
    """Read the winners' text and titles in one query, still owner-scoped."""
    rows = {
        row["pk"]: row
        for row in _live_chunks(user)
        .filter(pk__in=[chunk_id for chunk_id, _ in top])
        .values("pk", "note_id", "note__title", "heading_path", "text")
    }
    return [
        SearchHit(
            chunk_id=chunk_id,
            note_id=rows[chunk_id]["note_id"],
            title=rows[chunk_id]["note__title"],
            heading_path=rows[chunk_id]["heading_path"],
            text=rows[chunk_id]["text"],
            score=score,
            similarity=similarity.get(chunk_id),
            keyword_rank=keyword_rank.get(chunk_id),
        )
        for chunk_id, score in top
        # A note deleted between the legs and this read drops out here.
        if chunk_id in rows
    ]
