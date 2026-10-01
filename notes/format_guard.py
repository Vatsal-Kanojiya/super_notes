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
   price is one gone. Numbers are read from the document itself -- its text
   nodes, and the item numbers of an ordered list that starts anywhere but
   1 -- never from derived text (see ``_number_facts``, D523). The one
   allowance: a number typed as a list marker ("2) walk the dog") may become
   the same position in a real list that starts at 1, and back.
3. **Dates.** No month or weekday name that the original lacks. Together
   with rule 2 this covers "invented a date" in the ways a model does it.
4. **Checked items.** The number of ticked checklist items is unchanged: a
   ticked box is a fact too.
5. **Fact-bearing words.** Negations ("not", "never", "don't"...), number
   words ("five", "twice", "lakh"...) and relative dates ("today", "next",
   "ago"...) are numbers by another name: their multiset is exactly the
   original's (``FACT_WORDS``, D521). "Pay rent" becoming "Do not pay rent"
   fails here. A contraction is its long form first ("don't" is "do not"),
   and "next" and "last" count only before a time word in the same text
   node ("next week" is a date, "Next steps" a heading).
6. **Words kept.** At least ``min_kept`` of the original's distinct words are
   still there (recall). A dropped paragraph fails here.
7. **Words added.** At least ``min_original`` of the result's distinct words
   were in the original (precision), looser than rule 6 so a heading or a
   few connecting words are fine; and the new distinct words number at most
   ``max(new_words_floor, new_words_share x the original's distinct
   words)`` (D522), so in a long note, where a ratio alone would allow a
   whole invented paragraph, they still cannot.

A word the model corrected (a typo fix) counts as kept: an unmatched word
on each side that is a near spelling of the other (``difflib`` ratio of at
least 0.8, four letters or more, no digits) is paired off in rules 6 and 7.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from .content import (
    _TEXTBLOCKS,
    InvalidContent,
    _children,
    _inline_text,
    _is_inline,
    content_to_text,
    validate_doc,
)

# What the checks may say. A reason is for the log and the tests; the person
# only ever sees one message (tasks.CHANGED).
OK = ""
INVALID = "invalid_document"
NUMBER_CHANGED = "number_changed"
DATE_ADDED = "date_added"
FACT_WORD_CHANGED = "fact_word_changed"
CHECKED_CHANGED = "checked_changed"
WORDS_LOST = "words_lost"
WORDS_ADDED = "words_added"

# Leading list and quote syntax, which a restructure legitimately adds or
# removes: the markers content_to_text writes ("- ", "- [x] ", "> ", "12. ")
# and the "12) " a person may type.
_MARKER = re.compile(r"^(?:[ \t]*(?:>|-[ \t]\[[ x]\]|-|\d+[.)])(?:[ \t]|$))+", re.MULTILINE)
_NUMBER = re.compile(r"\d+(?:[.,:/-]\d+)*")
# A number typed as a list marker at the start of a line: "12. ", "250) ",
# also inside a typed quote ("> 3. ").
_TYPED_MARKER = re.compile(r"^[ \t]*(?:>[ \t]*)*(\d+)[.)](?:[ \t]|$)")
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*", re.UNICODE)

# Full names only, and "may" is left out: it is a common verb. The short
# forms that are not ordinary words are in too.
_DATE_WORDS = frozenset(
    "january february march april june july august september october november december "
    "jan feb apr jun jul aug sep sept oct nov dec "
    "monday tuesday wednesday thursday friday saturday sunday".split()
)

# Words that carry a fact as surely as a digit does (rule 5, D521): a
# negation flips a sentence, a number word is a number, a relative date is a
# date. Compared as a multiset, so one added or dropped is a change.
_NEGATIONS = "not no never don't doesn't didn't won't can't cannot isn't aren't wasn't without"
_NUMBER_WORDS = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty "
    "sixty seventy eighty ninety hundred thousand lakh crore million half once twice"
)
_RELATIVE_DATES = "today tomorrow yesterday tonight next last ago"
FACT_WORDS = frozenset(f"{_NEGATIONS} {_NUMBER_WORDS} {_RELATIVE_DATES}".split())
# A negation's contraction and its long form say the same thing, so they are
# compared as the long form ("don't" is "do not"; "can't" is "cannot").
_CONTRACTIONS = {
    "don't": ("do", "not"),
    "doesn't": ("does", "not"),
    "didn't": ("did", "not"),
    "won't": ("will", "not"),
    "can't": ("cannot",),
    "isn't": ("is", "not"),
    "aren't": ("are", "not"),
    "wasn't": ("was", "not"),
}
# "next" and "last" are a date only before one of these ("next week"); else
# they are ordinary words ("Next steps", "the last item").
_SEQUENCE_WORDS = frozenset({"next", "last"})
_TIME_WORDS = frozenset(
    "day week month year weekend morning evening night time "
    "monday tuesday wednesday thursday friday saturday sunday".split()
)

# Pairing typo fixes is quadratic; past this many pairs the result has
# changed far more than typos and the plain overlap decides.
_MAX_FUZZY_PAIRS = 40_000
_FUZZY_RATIO = 0.8
_FUZZY_MIN_LENGTH = 4
# More fact words added than this are not typo fixes, whatever they look like.
_MAX_FACT_TYPOS = 5


@dataclass(frozen=True)
class Verdict:
    """The outcome. ``recall`` and ``precision`` are 0..1 (1.0 when not measured)."""

    ok: bool
    reason: str = OK
    recall: float = 1.0
    precision: float = 1.0


def _words(doc: Any) -> list[str]:
    """The lower-cased words of a document's text, in order.

    List and quote markers are removed first (a checklist's "[x]" is not a
    word), and a negation's contraction is its long form ("don’t" is "do
    not"), so a rewrite between the two neither loses nor adds a word.
    """
    text = _MARKER.sub("", content_to_text(doc))
    return _expanded(_WORD.findall(text))


def _expanded(raw_words: list[str]) -> list[str]:
    """Lower-cased, curly apostrophes straightened, negation contractions spelt out."""
    words = []
    for word in raw_words:
        word = word.lower().replace("\u2019", "'")
        words.extend(_CONTRACTIONS.get(word, (word,)))
    return words


def _number_facts(doc: Any) -> tuple[Counter, Counter, Counter]:
    """``(numbers, typed markers, implicit ordinals)`` of a document (rule 2).

    ``numbers`` is every number in the text nodes -- a typed "250) " too --
    plus each item number of an ordered list whose ``start`` is not 1 (a
    list that starts at 12 says "12"). ``typed markers`` are the numbers
    typed as a list marker at a line's start; ``implicit ordinals`` the item
    numbers of ordered lists that start at 1, which say nothing a plain list
    would not. Iterative over containers; a textblock's text is joined
    across its text nodes (marks split them) and split on hard breaks.
    """
    numbers: Counter = Counter()
    markers: Counter = Counter()
    ordinals: Counter = Counter()
    stack = [doc] if isinstance(doc, dict) else []
    while stack:
        node = stack.pop()
        kind = node.get("type")
        children = _children(node)
        if node is not doc and (kind in _TEXTBLOCKS or all(_is_inline(c) for c in children)):
            for line in _inline_text(node, 0).split("\n"):
                numbers.update(_NUMBER.findall(line))
                typed = _TYPED_MARKER.match(line)
                if typed:
                    markers[typed.group(1)] += 1
            continue
        if kind == "orderedList":
            attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
            start = attrs.get("start", 1)
            if not isinstance(start, int) or isinstance(start, bool):
                start = 1
            positions = [str(start + i) for i in range(len(children))]
            (ordinals if start == 1 else numbers).update(positions)
        stack.extend(children)
    return numbers, markers, ordinals


def _numbers_match(original: Any, proposed: Any) -> bool:
    """Rule 2: the same numbers, a typed marker and a real list's position aside."""
    old, old_markers, old_ordinals = _number_facts(original)
    new, new_markers, new_ordinals = _number_facts(proposed)
    # A typed "2) " that became item 2 of a real list, or the other way.
    may_go = (new_ordinals - old_ordinals) & old_markers
    may_come = (old_ordinals - new_ordinals) & new_markers
    return not ((old - new) - may_go) and not ((new - old) - may_come)


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
            if _near(word, candidate):
                available.remove(candidate)
                paired += 1
                break
    return paired


def _near(word: str, candidate: str) -> bool:
    """A near spelling: what a typo fix looks like (see the module docstring)."""
    return (
        len(word) >= _FUZZY_MIN_LENGTH
        and len(candidate) >= _FUZZY_MIN_LENGTH
        and abs(len(candidate) - len(word)) <= 2
        and SequenceMatcher(None, word, candidate).ratio() >= _FUZZY_RATIO
    )


def _text_nodes(doc: Any) -> list[str]:
    """The ``text`` of every text node, in no particular order. Iterative."""
    texts, stack = [], [doc]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("text"), str):
            texts.append(node["text"])
        children = node.get("content")
        if isinstance(children, list):
            stack.extend(children)
    return texts


def _fact_words(doc: Any) -> list[str]:
    """Rule 5's fact-bearing words of a document, contractions expanded (D521).

    Read per text node, so "next" counts only when the very next word of its
    own text node is a time word.
    """
    facts = []
    for text in _text_nodes(doc):
        words = _expanded(_WORD.findall(text))
        for index, word in enumerate(words):
            if word not in FACT_WORDS:
                continue
            if word in _SEQUENCE_WORDS:
                following = words[index + 1] if index + 1 < len(words) else ""
                if following not in _TIME_WORDS:
                    continue
            facts.append(word)
    return facts


def _facts_match(original: Any, proposed: Any, old_words: list[str], new_words: list[str]) -> bool:
    """Rule 5: the fact-bearing words, as a multiset, are the original's.

    One allowance, for a typo fix (D525): a fact word the result adds may stand for
    a misspelt original word that is gone from the result and is not itself
    a fact word ("tomorow" -> "tomorrow"; never "seventy" -> "seven").
    """
    old_facts = Counter(_fact_words(original))
    new_facts = Counter(_fact_words(proposed))
    if old_facts - new_facts:
        return False
    added = new_facts - old_facts
    if not added:
        return True
    if sum(added.values()) > _MAX_FACT_TYPOS:
        return False  # Not typo fixes; and pairing them all would be slow.
    new_set = set(new_words)
    misspelt = Counter(w for w in old_words if w not in new_set and w not in FACT_WORDS)
    for fact, count in sorted(added.items()):
        for _ in range(count):
            match = next((w for w in sorted(misspelt) if misspelt[w] and _near(w, fact)), None)
            if match is None:
                return False
            misspelt[match] -= 1
    return True


def check_format(
    original: Any,
    proposed: Any,
    *,
    min_kept: float,
    min_original: float,
    new_words_floor: int = 8,
    new_words_share: float = 0.05,
) -> Verdict:
    """Whether ``proposed`` is the same note as ``original``, restructured.

    ``min_kept`` is the share of the original's distinct words that must
    survive; ``min_original`` the share of the result's that must come from
    the original. At most ``max(new_words_floor, new_words_share x the
    original's distinct words)`` distinct words may be new. Never raises,
    whatever ``proposed`` is.
    """
    try:
        validate_doc(proposed)
    except InvalidContent:
        return Verdict(False, INVALID)

    old_words, new_words = _words(original), _words(proposed)

    if not _numbers_match(original, proposed):
        return Verdict(False, NUMBER_CHANGED)

    old_set, new_set = set(old_words), set(new_words)
    if (new_set & _DATE_WORDS) - old_set:
        return Verdict(False, DATE_ADDED)

    if _checked_count(original) != _checked_count(proposed):
        return Verdict(False, CHECKED_CHANGED)

    if not _facts_match(original, proposed, old_words, new_words):
        return Verdict(False, FACT_WORD_CHANGED)

    missing, extra = old_set - new_set, new_set - old_set
    fixed = _typo_pairs(missing, extra)
    kept = len(old_set) - len(missing) + fixed
    recall = kept / len(old_set) if old_set else 1.0
    precision = (len(new_set) - len(extra) + fixed) / len(new_set) if new_set else 1.0

    if recall < min_kept:
        return Verdict(False, WORDS_LOST, recall, precision)
    new_count = len(extra) - fixed
    if precision < min_original or new_count > max(new_words_floor, new_words_share * len(old_set)):
        return Verdict(False, WORDS_ADDED, recall, precision)
    return Verdict(True, OK, recall, precision)
