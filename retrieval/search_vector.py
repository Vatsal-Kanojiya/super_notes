"""Keyword search over chunks: one expression, shared by the index and the query.

Same reason as notes/search.py: a GIN expression index is used only by a
query that builds the identical expression, configuration included. Phase
4's keyword query imports ``chunk_search_vector()`` instead of rebuilding
it. It covers the heading path as well as the text, so a query for
"risks" finds a chunk that sits under a "Risks" heading without
repeating the word (DECISIONS D64). The note title is not here: it lives
on the note, and notes/search.py already searches it.
"""

from django.contrib.postgres.search import SearchVector

from notes.search import SEARCH_CONFIG


def chunk_search_vector() -> SearchVector:
    return SearchVector("heading_path", "text", config=SEARCH_CONFIG)
