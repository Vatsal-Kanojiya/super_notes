"""Turn the ``[n]`` markers in an answer into citations back to the notes.

The prompt asks for ``[1]`` and ``[1][3]``; models also write ``[1, 3]``,
``[1; 3]`` and ``[1-3]``, so those are read too (DECISIONS D59). A range is
expanded only over excerpts that exist, so ``[1-99999]`` costs nothing.
Anything else in square brackets -- ``[see above]``, a Markdown link, a
``[^1]`` footnote -- is not a marker.

Two things are deliberately left alone (DECISIONS D60):

* **The answer text.** A marker pointing at no excerpt (a model inventing
  ``[9]`` when there were eight) is dropped from the citations but kept in
  the text: the stored answer is exactly what the model said, which is what
  debugging and evaluation need, and stripping "markers" would also eat an
  innocent ``[2024]``. The client links only the numbers in ``citations``.
* **The numbering.** Citations keep the excerpt's own ``n``, not a
  renumbered 1..m, so every marker in the text still matches its citation.
"""

import re

from .prompt import Excerpt

# "[1]", "[1, 2]", "[1;2]", "[1-3]", "[1–3]" (en dash), with any spacing.
_MARKER = re.compile(r"\[\s*(\d+(?:\s*[,;\-–]\s*\d+)*)\s*\]")
_PART = re.compile(r"(\d+)(?:\s*[\-–]\s*(\d+))?")

# How much of the excerpt a citation shows: enough to recognise the
# passage in a hover card, short enough for a list of eight.
SNIPPET_CHARS = 240

# Without a set of valid numbers to bound it, a range wider than this is
# read as its two ends.
MAX_RANGE = 50


def cited_numbers(answer: str, valid: set[int] | None = None) -> list[int]:
    """Every excerpt number the answer cites, in first-appearance order, once each.

    With `valid`, numbers outside it are skipped and ranges are expanded
    only over it; without, a range is expanded in full.
    """
    seen: dict[int, None] = {}
    for marker in _MARKER.finditer(answer):
        for start, end in _PART.findall(marker.group(1)):
            first, last = sorted((int(start), int(end or start)))
            if valid is None:
                # Unbounded by excerpts, a huge range would be a huge list.
                numbers = range(first, last + 1) if last - first < MAX_RANGE else (first, last)
            else:
                numbers = sorted(n for n in valid if first <= n <= last)
            for n in numbers:
                if valid is None or n in valid:
                    seen.setdefault(n, None)
    return list(seen)


def parse_citations(answer: str, excerpts: list[Excerpt]) -> list[dict]:
    """The AskQuery.citations list: ``{n, note_id, chunk_id, title, snippet}``."""
    by_number = {excerpt.n: excerpt for excerpt in excerpts}
    return [
        {
            "n": n,
            "note_id": by_number[n].note_id,
            "chunk_id": by_number[n].chunk_id,
            "title": by_number[n].title,
            "snippet": snippet(by_number[n].text),
        }
        for n in cited_numbers(answer, set(by_number))
    ]


def snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    """`text` on one line, cut at a word boundary to at most `limit` characters."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    # Mid-word? Back off to the last whole one (if there is one).
    if text[limit - 1] != " " and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + "…"
