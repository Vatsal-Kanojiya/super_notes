"""Plain text from a TipTap document, and a light check of its shape.

A note's ``content`` is the editor's own JSON (ProseMirror's document model,
as TipTap serialises it). The server never renders it, but it needs its
words: ``content_text`` feeds keyword search (notes/search.py) and is what a
person sees in the admin. So this walks the tree and writes one line per
block, keeping just enough structure to read naturally::

    Shopping
    - [x] milk
    - [ ] eggs
    1. first
      - nested
    > quoted

Two rules shape the code:

* **Never raise while extracting.** The content was validated on the way in,
  but a document written by a newer client, or by the admin shell, can hold
  node types this module has never heard of. An unknown node contributes its
  text and children; anything malformed contributes nothing. Extraction runs
  inside every note write, so a crash here would make a note unsaveable.
* **Validation is shallow on purpose.** It checks the envelope
  (``{"type": "doc", "content": [...]}``), that every node is an object with
  a string ``type``, and a depth limit. It does not check which node may
  contain which -- that is the editor's schema, and duplicating it here would
  make every new TipTap extension a server release.

Pure functions, no Django: see notes/tests/test_content.py.
"""

from __future__ import annotations

from typing import Any

# Deeper than any real document (a list nested fifty levels is already
# absurd), shallow enough that the walk can never hit Python's recursion
# limit, whatever a client sends.
MAX_DEPTH = 100

# Nodes that hold a list of items; each item's first line gets a marker.
_LISTS = {"bulletList", "orderedList", "taskList"}
# Blocks whose children are inline: all their text is one run, whatever
# inline nodes (mentions, emoji, future extensions) it is made of.
_TEXTBLOCKS = {"paragraph", "heading", "codeBlock"}


class InvalidContent(ValueError):
    """The document is not a TipTap document. The message is safe to show."""


def empty_doc() -> dict:
    """What a new note holds when the client sends no content."""
    return {"type": "doc", "content": []}


def validate_doc(doc: Any) -> None:
    """Refuse anything that is not shaped like a TipTap document.

    Raises InvalidContent naming the first problem found.
    """
    if not isinstance(doc, dict) or doc.get("type") != "doc":
        raise InvalidContent('Content must be a document: {"type": "doc", "content": [...]}.')
    if not isinstance(doc.get("content", []), list):
        raise InvalidContent("A document's content must be a list of nodes.")

    # Iterative, so a hostile document cannot exhaust the stack before the
    # depth check gets to see it.
    stack = [(child, 1) for child in doc.get("content", [])]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise InvalidContent(f"Content is nested more than {MAX_DEPTH} levels deep.")
        if not isinstance(node, dict) or not isinstance(node.get("type"), str):
            raise InvalidContent('Every node must be an object with a string "type".')
        if "text" in node and not isinstance(node["text"], str):
            raise InvalidContent('A text node\'s "text" must be a string.')
        children = node.get("content", [])
        if not isinstance(children, list):
            raise InvalidContent("A node's content must be a list of nodes.")
        stack.extend((child, depth + 1) for child in children)


def content_to_text(doc: Any) -> str:
    """The document's words, one line per block. Never raises."""
    if not isinstance(doc, dict):
        return ""
    lines = _children_lines(doc, depth=0)
    return "\n".join(line for line in (ln.rstrip() for ln in lines) if line.strip())


# The walk. Each block turns into a list of lines; containers (lists,
# quotes, the document itself) prefix and concatenate their children's.


def _children(node: dict) -> list:
    children = node.get("content")
    return [c for c in children if isinstance(c, dict)] if isinstance(children, list) else []


def _is_inline(node: dict) -> bool:
    """Text, a hard break, or a leaf: never something with block children."""
    return (
        isinstance(node.get("text"), str) or node.get("type") == "hardBreak" or not _children(node)
    )


def _inline_text(node: dict, depth: int) -> str:
    """Concatenate the text under a node; a hard break is a newline."""
    if depth > MAX_DEPTH:
        return ""
    if node.get("type") == "hardBreak":
        return "\n"
    text = node.get("text")
    if isinstance(text, str):
        return text
    return "".join(_inline_text(child, depth + 1) for child in _children(node))


def _children_lines(node: dict, depth: int) -> list[str]:
    lines: list[str] = []
    for child in _children(node):
        lines.extend(_block_lines(child, depth + 1))
    return lines


def _block_lines(node: dict, depth: int) -> list[str]:
    if depth > MAX_DEPTH:
        return []
    kind = node.get("type")

    if kind in _LISTS:
        return _list_lines(node, depth)
    if kind == "blockquote":
        return ["> " + line for line in _children_lines(node, depth) if line.strip()]

    # A textblock, or an unknown node holding only inline nodes: its text,
    # split on hard breaks and code newlines.
    if kind in _TEXTBLOCKS or all(_is_inline(c) for c in _children(node)):
        return _inline_text(node, depth).split("\n")
    # Anything else with block children is a container we do not know by
    # name (a table, a details block...): keep its children's lines.
    return _children_lines(node, depth)


def _list_lines(node: dict, depth: int) -> list[str]:
    kind = node.get("type")
    attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
    start = attrs.get("start", 1)
    number = start if isinstance(start, int) and not isinstance(start, bool) else 1

    lines: list[str] = []
    for item in _children(node):
        if kind == "orderedList":
            marker = f"{number}. "
            number += 1
        elif kind == "taskList":
            item_attrs = item.get("attrs") if isinstance(item.get("attrs"), dict) else {}
            marker = "- [x] " if item_attrs.get("checked") is True else "- [ ] "
        else:
            marker = "- "

        item_lines = _children_lines(item, depth + 1) if not _is_inline(item) else []
        if not any(line.strip() for line in item_lines):
            if kind != "taskList":
                continue
            # An empty checklist item still says something: an unticked box.
            item_lines = [""]
        # Continuation lines (a second paragraph, a nested list) are
        # indented under the item, as a person would write them.
        lines.append(marker + item_lines[0])
        lines.extend("  " + line for line in item_lines[1:])
    return lines
