"""Splitting a note into the chunks that get embedded (plan §6.1, docs/RAG.md).

Pure functions: a note's title and TipTap JSON in, an ordered list of
Chunk out, with no database, no clock and no randomness -- the same input
always gives the same chunks and the same hashes, which is what lets the
indexer reuse an unchanged chunk's embedding instead of paying for it again.

The document is walked as a tree, not flattened to text first, so chunks
follow the note's own structure:

* A heading starts a new section and extends ``heading_path`` ("Project >
  Risks"). A chunk never spans two sections, so every chunk has exactly one
  path.
* Paragraphs, list items, checklist items and code blocks become *blocks*.
  Blocks are packed into chunks up to CHUNK_TARGET_CHARS. A list or
  checklist item is never cut; only a single block longer than
  CHUNK_MAX_CHARS is split, and then on line, sentence and word boundaries,
  in that order (D34).
* Consecutive chunks overlap by up to CHUNK_OVERLAP_CHARS of *whole*
  blocks -- or, when the last block is a paragraph too long for that,
  its last whole sentences. An item is never carried over in part.

Bad content never raises: a note is whatever a client saved, and one
malformed node must cost that node, not the whole note's index. Unknown
node types are read for whatever text they contain.
"""

import hashlib
import itertools
import re
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

# Between a title and the headings in a breadcrumb, and between headings.
PATH_SEPARATOR = " > "

# TipTap nests lists inside lists; nothing legitimate goes this deep. The
# cap turns a hostile, deeply nested document into truncated text instead
# of a RecursionError.
MAX_DEPTH = 32

LIST_TYPES = frozenset({"bulletList", "orderedList", "taskList"})
INLINE_TYPES = frozenset({"text", "hardBreak"})

# Tried in order when one block is too long to fit a chunk: lines, then
# sentences, then words. A single word longer than a chunk is cut by
# length, the only place anything is cut mid-token.
_LINES = re.compile(r"\n+")
# A sentence ends at ., ! or ? and whitespace -- unless what precedes is a
# common abbreviation or a single-letter initial ("e.g. this", "Dr. Who",
# "J. Smith"), which only *look* like ends (DECISIONS D82). Fixed-width
# negative lookbehinds, one per abbreviation, since `re` has no
# variable-width ones. "etc." and "No." can genuinely end a sentence; not
# splitting there only makes a chunk slightly longer, never wrong.
_ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "Dr.", "Mr.", "Mrs.", "Ms.", "vs.", "approx.", "No.")
_SENTENCES = re.compile(
    r"(?<=[.!?])"
    + "".join(rf"(?<!\b{re.escape(abbr)})" for abbr in _ABBREVIATIONS)
    + r"(?<!\b[A-Z]\.)"
    + r"\s+"
)
_WORDS = re.compile(r"\s+")
_SEPARATORS = ((_LINES, "\n"), (_SENTENCES, " "), (_WORDS, " "))


@dataclass(frozen=True, slots=True)
class Chunk:
    """One piece of a note, ready to embed.

    ``text`` is what a person is shown (the citation excerpt).
    ``embed_text`` is what the provider is sent: the same text behind a
    "Title > Heading > Subheading" line, because a chunk that says "move
    it to Friday" means nothing without knowing which note it is from.
    ``content_hash`` is over ``embed_text``, so renaming a note or a
    heading changes the hash and re-embeds (D35).
    """

    ordinal: int
    text: str
    heading_path: str
    embed_text: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class _Block:
    text: str
    heading_path: str
    # Prose may lend its trailing sentences to the next chunk's overlap;
    # an item or a code block only ever travels whole.
    prose: bool


def chunk_note(title, content, *, target_chars=None, max_chars=None, overlap_chars=None):
    """Chunk one note. ``content`` is its TipTap document (a dict).

    The sizes default to the CHUNK_* settings; tests pass small ones.
    """
    target, maximum, overlap = _sizes(target_chars, max_chars, overlap_chars)
    title = _one_line(title) if isinstance(title, str) else ""

    walker = _Walker()
    walker.block(content, 0, "")
    walker.finish()

    chunks = []
    # Consecutive, not global: a path only changes at a heading, and two
    # separate sections that happen to share a name stay separate chunks.
    for heading_path, section in itertools.groupby(walker.blocks, key=lambda b: b.heading_path):
        for text in _pack(list(section), target, maximum, overlap):
            embed_text = _embed_text(title, heading_path, text)
            chunks.append(
                Chunk(
                    ordinal=len(chunks),
                    text=text,
                    heading_path=heading_path,
                    embed_text=embed_text,
                    content_hash=hashlib.sha256(embed_text.encode("utf-8")).hexdigest(),
                )
            )
    return chunks


def _sizes(target, maximum, overlap):
    target = settings.CHUNK_TARGET_CHARS if target is None else target
    maximum = settings.CHUNK_MAX_CHARS if maximum is None else maximum
    overlap = settings.CHUNK_OVERLAP_CHARS if overlap is None else overlap
    # A configuration mistake, not bad content -- so this one does raise,
    # loudly and on the first note, instead of quietly chunking wrongly.
    if not (0 < target <= maximum) or not (0 <= overlap < target):
        raise ImproperlyConfigured(
            "Chunk sizes need 0 < CHUNK_TARGET_CHARS <= CHUNK_MAX_CHARS and "
            "0 <= CHUNK_OVERLAP_CHARS < CHUNK_TARGET_CHARS"
        )
    return target, maximum, overlap


def _embed_text(title, heading_path, text):
    prefix = PATH_SEPARATOR.join(part for part in (title, heading_path) if part)
    return f"{prefix}\n\n{text}" if prefix else text


# -- Walking the document ---------------------------------------------------


class _Walker:
    """Turns a TipTap tree into an ordered list of _Block.

    Every accessor checks the type it gets: a node may be missing "type",
    have "content" that is not a list, or "attrs" that is not a dict, and
    each of those just contributes nothing.
    """

    def __init__(self):
        self.blocks: list[_Block] = []
        self._headings: list[tuple[int, str]] = []
        # A heading nothing has been written under yet. If the next heading
        # closes its section, its own words become a block, so a note of
        # bare headings is still findable.
        self._pending_level: int | None = None

    @property
    def _path(self):
        return PATH_SEPARATOR.join(text for _, text in self._headings)

    def finish(self):
        self._flush_pending(level=0)

    def block(self, node, depth, prefix):
        if depth > MAX_DEPTH or not isinstance(node, dict):
            return
        kind = node.get("type")
        content = _content(node)

        if kind == "heading":
            self._heading(_heading_level(node), _one_line(_inline(content, depth)))
        elif kind == "paragraph":
            self._emit(prefix, _clean(_inline(content, depth)), prose=True)
        elif kind in LIST_TYPES:
            self._list(node, depth, prefix, indent="")
        elif kind == "blockquote":
            for child in content:
                self.block(child, depth + 1, prefix + "> ")
        elif kind == "codeBlock":
            # Verbatim: indentation and line breaks are the content.
            self._emit(prefix, _inline(content, depth).strip("\n"), prose=False)
        elif kind == "text":
            # Inline text straight under a block container: malformed, but
            # the words are still the user's.
            self._emit(prefix, _clean(_inline([node], depth)), prose=True)
        elif any(_is_block(child) for child in content):
            # "doc", and any container this module does not know (a table,
            # a future extension): read its children as blocks.
            for child in content:
                self.block(child, depth + 1, prefix)
        else:
            # An unknown leaf-ish node: keep whatever text it holds.
            self._emit(prefix, _clean(_inline(content, depth)), prose=True)

    def _list(self, node, depth, prefix, indent):
        kind = node.get("type")
        number = _int_attr(node, "start", 1)
        for item in _content(node):
            if depth > MAX_DEPTH or not isinstance(item, dict):
                continue
            if kind == "taskList" or item.get("type") == "taskItem":
                marker = "[x] " if _attrs(item).get("checked") is True else "[ ] "
            elif kind == "orderedList":
                marker = f"{number}. "
                number += 1
            else:
                marker = "- "

            # The item's own words are one block, whatever their length
            # short of the max; a nested list's items are blocks of their
            # own, indented, so one long sub-list cannot make its parent
            # item unsplittable-and-huge.
            own, nested = [], []
            for child in _content(item):
                if isinstance(child, dict) and child.get("type") in LIST_TYPES:
                    nested.append(child)
                else:
                    own.append(_clean(_inline([child], depth + 1)))
            text = "\n".join(part for part in own if part)
            if text:
                self._emit(prefix, indent + marker + text, prose=False)
            for child in nested:
                self._list(child, depth + 2, prefix, indent + "  ")

    def _heading(self, level, text):
        if not text:
            return
        self._flush_pending(level)
        while self._headings and self._headings[-1][0] >= level:
            self._headings.pop()
        self._headings.append((level, text))
        self._pending_level = level

    def _flush_pending(self, level):
        # Only when the pending heading's section really ended empty: a
        # heading followed by its own subheading is not empty, its words
        # are already in every chunk's path below it.
        if self._pending_level is not None and level <= self._pending_level:
            self.blocks.append(_Block(self._headings[-1][1], self._path, prose=True))
        self._pending_level = None

    def _emit(self, prefix, text, prose):
        if not text.strip():
            return
        self._pending_level = None
        self.blocks.append(_Block(prefix + text.rstrip(), self._path, prose))


def _content(node):
    content = node.get("content")
    return content if isinstance(content, list) else []


def _attrs(node):
    attrs = node.get("attrs")
    return attrs if isinstance(attrs, dict) else {}


def _int_attr(node, name, default):
    value = _attrs(node).get(name)
    # bool is an int subclass; True is not a heading level.
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _heading_level(node):
    return min(max(_int_attr(node, "level", 1), 1), 6)


def _is_block(node):
    return isinstance(node, dict) and node.get("type") not in INLINE_TYPES


def _inline(nodes, depth):
    """The text of inline nodes; hardBreak becomes a newline."""
    parts = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        kind = node.get("type")
        if kind == "text":
            text = node.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif kind == "hardBreak":
            parts.append("\n")
        elif depth < MAX_DEPTH:
            # A paragraph inside a list item, or an inline node this module
            # does not know that wraps text of its own.
            inner = _inline(_content(node), depth + 1)
            if inner:
                parts.append(inner if not parts else "\n" + inner)
    return "".join(parts)


def _clean(text):
    """Prose whitespace: runs of spaces collapsed, line breaks kept."""
    lines = (" ".join(line.split()) for line in text.replace("\r\n", "\n").split("\n"))
    return "\n".join(line for line in lines if line)


def _one_line(text):
    return " ".join(text.split())


# -- Packing blocks into chunks ---------------------------------------------


def _pack(section, target, maximum, overlap):
    """Pack one section's blocks into chunk texts, each at most ``maximum``."""
    chunks = []
    current: list[_Block] = []
    # Whether ``current`` holds anything besides what was carried over from
    # the previous chunk; a chunk of nothing but overlap is never emitted.
    fresh = False

    for block in section:
        for piece in _pieces(block, target, maximum):
            if fresh and _length(current + [piece]) > target:
                # The carry-over is sized so that it plus this piece still
                # fits the max.
                carried = _overlap(current, min(overlap, maximum - len(piece.text) - 1))
                if carried != current:
                    chunks.append(_join(current))
                    current, fresh = carried, False
                # else: everything so far is small enough to be the overlap,
                # so a new chunk would only repeat it. Grow this one instead;
                # it stays under the max by the same sizing.
            current.append(piece)
            fresh = True

    if fresh:
        chunks.append(_join(current))
    return chunks


def _pieces(block, target, maximum):
    if len(block.text) <= maximum:
        return [block]
    # Split to the target, not the max, so the pieces pack like any
    # other block.
    return [_Block(text, block.heading_path, block.prose) for text in _split(block.text, target)]


def _split(text, limit, level=0):
    """Split text into pieces of at most ``limit`` on the coarsest boundary
    that works: lines, then sentences, then words, then raw length."""
    if len(text) <= limit:
        return [text]
    if level == len(_SEPARATORS):
        return [text[i : i + limit] for i in range(0, len(text), limit)]

    pattern, joiner = _SEPARATORS[level]
    parts = [part for part in pattern.split(text) if part.strip()]
    if len(parts) <= 1:
        return _split(text, limit, level + 1)

    pieces, buffer = [], ""
    for part in parts:
        for sub in _split(part, limit, level + 1):
            candidate = f"{buffer}{joiner}{sub}" if buffer else sub
            if len(candidate) <= limit:
                buffer = candidate
            else:
                if buffer:
                    pieces.append(buffer)
                buffer = sub
    if buffer:
        pieces.append(buffer)
    return pieces


def _overlap(blocks, budget):
    """The tail of ``blocks`` to repeat at the start of the next chunk.

    Whole blocks from the end while they fit the budget. If not even the
    last one fits and it is prose, its last whole sentences instead. Never
    part of an item.
    """
    if budget <= 0:
        return []
    carried: list[_Block] = []
    for block in reversed(blocks):
        if _length([block] + carried) > budget:
            break
        carried.insert(0, block)
    if carried or not blocks[-1].prose:
        return carried

    last = blocks[-1]
    sentences = [s for s in _SENTENCES.split(last.text) if s.strip()]
    tail: list[str] = []
    for sentence in reversed(sentences[1:]):  # never the whole paragraph
        if len(" ".join([sentence] + tail)) > budget:
            break
        tail.insert(0, sentence)
    return [_Block(" ".join(tail), last.heading_path, prose=True)] if tail else []


def _join(blocks):
    return "\n".join(block.text for block in blocks)


def _length(blocks):
    return len(_join(blocks))
