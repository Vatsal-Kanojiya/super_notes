"""The default provider. No network, no keys, no cost.

Every environment that has not set CHAT_PROVIDER runs on this, and the test
runner forces it. Unlike the reference's fixed fake bill, its answer depends
on the prompt: it quotes the first sentence of excerpt [1] (and of [2], when
there is one) with their citation markers, the way a grounded answer would.
That keeps an end-to-end test meaningful -- the citations it parses point at
real excerpts, so a test can check they map back to the right notes.
"""

import re

from django.conf import settings

from ..types import ChatResult

# The delimiters assistant/prompt.py writes. Excerpt text cannot contain a
# closing tag (prompt.neutralise), so a lazy match finds each excerpt whole.
_EXCERPT = re.compile(r'<excerpt n="(\d+)"[^>]*>\n(.*?)\n</excerpt>', re.DOTALL)
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


class FakeProvider:
    name = "fake"

    def complete(
        self, system: str, user: str, model: str = "", max_output_tokens: int = 0
    ) -> ChatResult:
        # Deterministic on purpose -- no clock, no randomness -- so a test
        # asserting on this result cannot flake.
        excerpts = _EXCERPT.findall(user)[:CITED]
        if excerpts:
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
