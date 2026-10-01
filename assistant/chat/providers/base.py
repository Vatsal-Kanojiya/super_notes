"""The shape every provider implements.

A Protocol, not an ABC: nothing here needs to be inherited from, only
matched -- FakeProvider satisfies this without knowing it exists.

runtime_checkable only so a test can check that every registered provider
matches it (the check sees that the members exist, not their signatures).

``stream`` is optional (DECISIONS D360): a provider that has it matches
StreamingChatProvider as well, and one that has not is streamed by the
boundary as a single delta of its ``complete`` (assistant/chat/__init__.py).
"""

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from ..types import ChatResult


@runtime_checkable
class ChatProvider(Protocol):
    name: str

    def complete(
        self, system: str, user: str, model: str, max_output_tokens: int
    ) -> ChatResult: ...


@runtime_checkable
class StreamingChatProvider(ChatProvider, Protocol):
    def stream(
        self, system: str, user: str, model: str, max_output_tokens: int
    ) -> Iterator[str | ChatResult]:
        """Yield the answer's text as it is written, then one ChatResult, last.

        The result is what ``complete`` would have returned for the same
        answer: its text is the deltas joined and stripped, with the usage.
        Raises ChatError or TransientChatError exactly as ``complete`` does --
        before the first delta or after any number of them.
        """
        ...
