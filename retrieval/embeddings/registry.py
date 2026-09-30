"""Which provider class a name resolves to.

Import paths as strings, resolved lazily, as in the reference's bill-scan
registry: a provider module is only imported once it is configured, so a
broken or heavy one can never stop the others from loading.
"""

from django.utils.module_loading import import_string

from .errors import EmbeddingError

PROVIDERS = {
    "fake": "retrieval.embeddings.providers.fake.FakeProvider",
    "openai": "retrieval.embeddings.providers.openai.OpenAIProvider",
    "gemini": "retrieval.embeddings.providers.gemini.GeminiProvider",
}


def get_provider(name: str):
    """Return a fresh instance of the provider registered under `name`."""
    path = PROVIDERS.get(name)
    if path is None:
        raise EmbeddingError(f"Unknown embedding provider: {name!r}")

    try:
        provider_class = import_string(path)
    except ImportError as exc:
        # A deployment problem, not a text that failed to embed -- but it
        # still surfaces as EmbeddingError, the only exception this
        # package's boundary promises for a permanent failure.
        raise EmbeddingError(f"Embedding provider {name!r} is not available: {exc}") from exc

    return provider_class()
