"""The shape every provider implements.

A Protocol, not an ABC: nothing here needs to be inherited from, only
matched -- FakeProvider satisfies this without knowing it exists.

runtime_checkable only so a test can check that every registered provider
matches it (the check sees that the members exist, not their signatures).
"""

from typing import Protocol, runtime_checkable

from ..types import ChatResult


@runtime_checkable
class ChatProvider(Protocol):
    name: str

    def complete(
        self, system: str, user: str, model: str, max_output_tokens: int
    ) -> ChatResult: ...
