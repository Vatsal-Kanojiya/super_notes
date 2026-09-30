"""The shape every provider implements.

A Protocol, not an ABC: nothing here needs to be inherited from, only
matched -- FakeProvider satisfies this without importing it.
"""

from typing import Literal, Protocol

# What the text is for. Gemini embeds a stored passage and a search query
# differently (its taskType), and Google recommends saying which;
# OpenAI and the fake ignore it.
Task = Literal["document", "query"]


class EmbeddingProvider(Protocol):
    name: str

    def embed(self, texts: list[str], model: str, dimensions: int, task: Task) -> list[list[float]]:
        """One vector per text, in the same order, each ``dimensions`` long.

        Called with at most EMBEDDING_BATCH_SIZE texts; the boundary does
        the batching and checks the lengths. Raises EmbeddingError or
        EmbeddingTransientError, nothing else.
        """
        ...
