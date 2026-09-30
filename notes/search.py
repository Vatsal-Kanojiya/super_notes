"""Keyword search over notes: one expression, shared by the index and the query.

A GIN index over ``to_tsvector(...)`` is an *expression* index. Postgres
uses it only when a query's expression is the same expression, text-search
configuration included: ``to_tsvector('english', x)`` and ``to_tsvector(x)``
are different, and an index on one is invisible to the other. The reference
(expenses/filters.py) keeps the two in step by comment; here both are built
by ``note_search_vector()``, so they cannot drift. notes/tests/test_search.py
proves the index is used with an EXPLAIN.

``SEARCH_CONFIG`` is the text-search configuration (DECISIONS D24). Chunk
search in retrieval/ reuses it, so a word matches the same way in both.
Changing it means a migration for every index built on it; that is why it
is a constant and not a setting.
"""

from django.contrib.postgres.search import SearchQuery, SearchVector

SEARCH_CONFIG = "english"


def note_search_vector() -> SearchVector:
    """``to_tsvector`` over the title and the derived body text."""
    return SearchVector("title", "content_text", config=SEARCH_CONFIG)


def keyword_search(queryset, query: str):
    """Narrow a Note queryset to notes matching ``query``.

    ``websearch_to_tsquery`` because it takes what people type -- words,
    "quoted phrases", -exclusions, ``or`` -- and never raises a syntax
    error, unlike ``to_tsquery``. ``alias`` rather than ``annotate``: the
    vector is needed for the WHERE clause only, and selecting it would
    compute it again for every row returned.
    """
    return queryset.alias(search=note_search_vector()).filter(
        search=SearchQuery(query, config=SEARCH_CONFIG, search_type="websearch")
    )
