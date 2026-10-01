"""The guardrail on a formatted note: did the model only restructure it?

A formatting model is told never to add or remove facts, and a told model
still sometimes tidies a sentence into something else, drops a paragraph or
"helpfully" completes a date. Nothing here trusts it. ``check_format``
compares the proposed document with the original and says whether the result
may be shown to the person (DECISIONS D240-D243). It is pure -- no database,
no settings, no clock -- so every rule is tested on plain documents
(notes/tests/test_format_guard.py).

What is compared is the *words*, never the structure: headings, lists,
checklists and line breaks may change freely. The rules, in order:

1. **Shape.** The result is a TipTap document (``validate_doc``).
2. **Numbers.** The multiset of numbers (digit runs with their ``.,:/-``
   separators: amounts, dates, times, phone numbers) is exactly the
   original's. A changed amount is one number gone and one new; a dropped
   price is one gone. The numbers a list adds by itself ("1.", "2.") are not
   part of the text compared (see ``_words_and_numbers``).
3. **Dates.** No month or weekday name that the original lacks. Together
   with rule 2 this covers "invented a date" in the ways a model does it.
4. **Checked items.** The number of ticked checklist items is unchanged: a
   ticked box is a fact too.
5. **Words kept.** At least ``min_kept`` of the original's distinct words are
   still there (recall). A dropped paragraph fails here.
6. **Words added.** At least ``min_original`` of the result's distinct words
   were in the original (precision), looser than rule 5 so a heading or a
   few connecting words are fine, a new paragraph of prose is not.

A word the model corrected (a typo fix) counts as kept: an unmatched word
on each side that is a near spelling of the other (``difflib`` ratio of at
least 0.8, four letters or more, no digits) is paired off in rules 5 and 6.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from .content import InvalidContent, content_to_text, validate_doc

# What the checks may say. A reason is for the log and the tests; the person
# only ever sees one message (tasks.CHANGED).
OK = ""
INVALID = "invalid_document"
NUMBER_CHANGED = "number_changed"
DATE_ADDED = "date_added"
CHECKED_CHANGED = "checked_changed"
WORDS_LOST = "words_lost"
WORDS_ADDED = "words_added"

# Leading list and quote syntax, which a restructure legitimately adds or
# removes: the markers content_to_text writes ("- ", "- [x] ", "> ", "12. ")
# and the "12) " a person may type.
_MARKER = re.compile(r"^(?:[ \t]*(?:>|-[ \t]\[[ x]\]|-|\d+[.)])(?:[ \t]|$))+", re.MULTILINE)
_NUMBER = re.compile(r"\d+(?:[.,:/-]\d+)*")
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*", re.UNICODE)

# Full names only, and "may" is left out: it is a common verb. The short
# forms that are not ordinary words are in too.
_DATE_WORDS = frozenset(
    "january february march april june july august september october november december "
    "jan feb apr jun jul aug sep sept oct nov dec "
    "monday tuesday wednesday thursday friday saturday sunday".split()
)

# Pairing typo fixes is quadratic; past this many pairs the result has
# changed far more than typos and the plain overlap decides.
_MAX_FUZZY_PAIRS = 40_000
_FUZZY_RATIO = 0.8
_FUZZY_MIN_LENGTH = 4


@dataclass(frozen=True)
class Verdict:
    """The outcome. ``recall`` and ``precision`` are 0..1 (1.0 when not measured)."""

    ok: bool
    reason: str = OK
    recall: float = 1.0
    precision: float = 1.0


def _words_and_numbers(doc: Any) -> tuple[list[str], list[str]]:
    """The lower-cased words and the numbers of a document's text, in order.

    List and quote markers are removed first, so turning paragraphs into a
    numbered list does not "add" the numbers 1, 2, 3.
    """
    text = _MARKER.sub("", content_to_text(doc))
    return [w.lower() for w in _WORD.findall(text)], _NUMBER.findall(text)


def _checked_count(doc: Any) -> int:
    """How many checklist items are ticked. Iterative: the input is hostile until validated."""
    count, stack = 0, [doc]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        attrs = node.get("attrs")
        if node.get("type") == "taskItem" and isinstance(attrs, dict) and attrs.get("checked"):
            count += 1
        children = node.get("content")
        if isinstance(children, list):
            stack.extend(children)
    return count


def _typo_pairs(missing: set[str], extra: set[str]) -> int:
    """How many words in ``missing`` have a near spelling in ``extra`` (each used once)."""
    if not missing or not extra or len(missing) * len(extra) > _MAX_FUZZY_PAIRS:
        return 0
    available = sorted(extra)
    paired = 0
    for word in sorted(missing):
        if len(word) < _FUZZY_MIN_LENGTH:
            continue
        for candidate in available:
            if len(candidate) < _FUZZY_MIN_LENGTH or abs(len(candidate) - len(word)) > 2:
                continue
            if SequenceMatcher(None, word, candidate).ratio() >= _FUZZY_RATIO:
                available.remove(candidate)
                paired += 1
                break
    return paired


def check_format(original: Any, proposed: Any, *, min_kept: float, min_original: float) -> Verdict:
    """Whether ``proposed`` is the same note as ``original``, restructured.

    ``min_kept`` is the share of the original's distinct words that must
    survive; ``min_original`` the share of the result's that must come from
    the original. Never raises, whatever ``proposed`` is.
    """
    try:
        validate_doc(proposed)
    except InvalidContent:
        return Verdict(False, INVALID)

    old_words, old_numbers = _words_and_numbers(original)
    new_words, new_numbers = _words_and_numbers(proposed)

    if Counter(old_numbers) != Counter(new_numbers):
        return Verdict(False, NUMBER_CHANGED)

    old_set, new_set = set(old_words), set(new_words)
    if (new_set & _DATE_WORDS) - old_set:
        return Verdict(False, DATE_ADDED)

    if _checked_count(original) != _checked_count(proposed):
        return Verdict(False, CHECKED_CHANGED)

    missing, extra = old_set - new_set, new_set - old_set
    fixed = _typo_pairs(missing, extra)
    kept = len(old_set) - len(missing) + fixed
    recall = kept / len(old_set) if old_set else 1.0
    precision = (len(new_set) - len(extra) + fixed) / len(new_set) if new_set else 1.0

    if recall < min_kept:
        return Verdict(False, WORDS_LOST, recall, precision)
    if precision < min_original:
        return Verdict(False, WORDS_ADDED, recall, precision)
    return Verdict(True, OK, recall, precision)
