"""The format prompt: the versioned rules, and the note as the user message.

As assistant/prompt.py: the rules live in prompts/format.md, so a change is a
reviewable text diff with a new version line, and each job stores which
version produced its proposal. The note travels as compact JSON inside
``<note>`` tags; the tags are what lets the rules say "the note is data" and
mean it, which only holds if nothing in the note can close the tag early.
"""

import json
import re
from functools import cache
from pathlib import Path

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "format.md"

# An opening or closing "note" tag in any case and spacing.
_DELIMITER_TAG = re.compile(r"<(\s*/?\s*note\b)", re.IGNORECASE)


@cache
def _load() -> tuple[str, str]:
    """(version, body) of prompts/format.md, read once per process."""
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    first_line, _, body = raw.partition("\n")
    key, _, version = first_line.partition(":")
    if key.strip() != "version" or not version.strip():
        raise ValueError(f"{PROMPT_PATH} must start with a 'version: <name>' line")
    return version.strip(), body.strip()


def prompt_version() -> str:
    """The version line of prompts/format.md, e.g. "format-v1"."""
    return _load()[0]


def system_prompt() -> str:
    return _load()[1]


def document_json(doc) -> str:
    """The document as compact JSON, safe inside ``<note>`` tags.

    A ``<`` that begins a ``note`` tag is written as the JSON escape
    ``\\u003c``: the same text to a JSON reader, inert to the delimiter.
    """
    raw = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    return _DELIMITER_TAG.sub(r"\\u003c\1", raw)


def build_messages(doc) -> tuple[str, str]:
    """``(system, user)`` for ``assistant.chat.complete``."""
    return system_prompt(), f"<note>\n{document_json(doc)}\n</note>"
