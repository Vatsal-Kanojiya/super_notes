"""The default provider. No network, no keys, no cost.

Every environment that has not set CHAT_PROVIDER runs on this, and the test
runner forces it. Unlike the reference's fixed fake bill, its answer depends
on the prompt: it quotes the first sentence of excerpt [1] (and of [2], when
there is one) with their citation markers, the way a grounded answer would.
That keeps an end-to-end test meaningful -- the citations it parses point at
real excerpts, so a test can check they map back to the right notes.
"""

import json
import re

from django.conf import settings

from ..types import ChatResult

# The delimiters assistant/prompt.py writes. Excerpt text cannot contain a
# closing tag (prompt.neutralise), so a lazy match finds each excerpt whole.
_EXCERPT = re.compile(r'<excerpt n="(\d+)"[^>]*>\n(.*?)\n</excerpt>', re.DOTALL)
# The note notes/format_prompt.py wraps its document in. Greedy: the document is
# JSON, so the last closing tag is the real one.
_NOTE = re.compile(r"<note>\n(.*)\n</note>", re.DOTALL)
_SENTENCE = re.compile(r"(.+?[.!?])(?=\s|$)")

# The answer quotes at most this many excerpts.
CITED = 2
# A "sentence" with no full stop in sight is cut here.
MAX_SENTENCE_CHARS = 200


def approximate_tokens(text: str) -> int:
    """About four characters a token -- the usual rule of thumb for English."""
    return max(1, (len(text) + 3) // 4)


def first_sentence(text: str) -> str:
    text = " ".join(text.split())
    match = _SENTENCE.match(text)
    sentence = match.group(1) if match else text
    return sentence[:MAX_SENTENCE_CHARS]


def format_document(doc: dict) -> dict:
    """What the fake "formats": a lone first line becomes the heading.

    The one visible, deterministic restructuring that keeps every word, so
    an end-to-end format job passes its guardrail. A document that already
    has a heading, or has nothing after its first paragraph, is returned as it is.
    """
    blocks = doc.get("content") if isinstance(doc, dict) else None
    if not isinstance(blocks, list) or len(blocks) < 2:
        return doc
    if any(isinstance(b, dict) and b.get("type") == "heading" for b in blocks):
        return doc
    first = blocks[0]
    if isinstance(first, dict) and first.get("type") == "paragraph":
        heading = {**first, "type": "heading", "attrs": {"level": 2}}
        return {**doc, "content": [heading, *blocks[1:]]}
    return doc


class FakeProvider:
    name = "fake"

    def complete(
        self, system: str, user: str, model: str = "", max_output_tokens: int = 0
    ) -> ChatResult:
        # Deterministic on purpose -- no clock, no randomness -- so a test
        # asserting on this result cannot flake.
        note = _NOTE.match(user)
        excerpts = _EXCERPT.findall(user)[:CITED]
        if note:
            # A "format my note" request (notes/format_prompt.py): answer with the
            # document, restructured, as the prompt demands.
            try:
                text = json.dumps(format_document(json.loads(note.group(1))), ensure_ascii=False)
            except ValueError:
                text = "not json"
        elif excerpts:
            text = " ".join(f"{first_sentence(body)} [{n}]" for n, body in excerpts)
        else:
            text = settings.ASK_NO_ANSWER_TEXT

        return ChatResult(
            text=text,
            provider=self.name,
            model="fake",
            input_tokens=approximate_tokens(system) + approximate_tokens(user),
            output_tokens=approximate_tokens(text),
        )
