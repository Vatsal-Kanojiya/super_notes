"""The summary prompt: the versioned rules, and the text as the user message.

As notes/format_prompt.py: the rules live in prompts/summary.md (a text diff
with a new version line when they change; each job stores the version that
made its summary), and the document travels inside ``<document>`` tags,
which only means "data" if nothing in it can close the tag early.
"""

import html
import re
from functools import cache
from pathlib import Path

from django.conf import settings

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "summary.md"

# What the model says when there is nothing to summarise (rule 7).
EMPTY = "EMPTY"

# An opening or closing "document" tag in any case and spacing.
_DELIMITER_TAG = re.compile(r"<(\s*/?\s*document\b)", re.IGNORECASE)
_LABEL = re.compile(r"^\s*(?:\*\*)?summary(?:\*\*)?\s*:\s*(?:\*\*)?\s*", re.IGNORECASE)


@cache
def _load() -> tuple[str, str]:
    """(version, body) of prompts/summary.md, read once per process."""
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    first_line, _, body = raw.partition("\n")
    key, _, version = first_line.partition(":")
    if key.strip() != "version" or not version.strip():
        raise ValueError(f"{PROMPT_PATH} must start with a 'version: <name>' line")
    return version.strip(), body.strip()


def prompt_version() -> str:
    """The version line of prompts/summary.md, e.g. "summary-v1"."""
    return _load()[0]


def system_prompt() -> str:
    return _load()[1]


def neutralise(text: str) -> str:
    """Make a ``<document`` tag inside the text inert; everything else is as written."""
    return _DELIMITER_TAG.sub(r"&lt;\1", text)


def cut(text: str) -> str:
    """The text a summary is made from: at most SUMMARY_MAX_INPUT_CHARS of its start."""
    return text.strip()[: settings.SUMMARY_MAX_INPUT_CHARS]


def build_messages(title: str, text: str) -> tuple[str, str]:
    """``(system, user)`` for ``assistant.chat.complete``."""
    label = html.escape(" ".join((title or "").split()), quote=True)
    return system_prompt(), f'<document title="{label}">\n{neutralise(cut(text))}\n</document>'


def clean_summary(text: str) -> str:
    """The model's reply as stored: no "Summary:" label, no wrapping quotes, capped.

    ``""`` when there is nothing (an empty reply, or the EMPTY marker).
    """
    text = _LABEL.sub("", text.strip(), count=1).strip()
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if text.upper().rstrip(".") == EMPTY:
        return ""
    return text[: settings.SUMMARY_MAX_CHARS].rstrip()
