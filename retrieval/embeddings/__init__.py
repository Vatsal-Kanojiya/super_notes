"""The embedding boundary.

embed_texts (for stored chunks) and embed_query (for a search) are the only
things the rest of the app calls. They return plain lists of floats or
raise one of two exceptions -- EmbeddingError (give up) or
EmbeddingTransientError (retry later) -- and no provider type or vendor
error ever crosses this line.
"""

import math

from django.conf import settings

from .errors import EmbeddingError, EmbeddingTransientError
from .registry import get_provider

__all__ = [
    "EmbeddingError",
    "EmbeddingTransientError",
    "embed_query",
    "embed_texts",
    "embedding_model_id",
]


def embed_texts(texts: list[str]) -> list[list[float]]:
    """One vector per text, in order, for storing (a chunk's embed_text)."""
    return _embed(list(texts), "document")


def embed_query(text: str) -> list[float]:
    """The vector for a search query. Comparable with embed_texts' vectors,
    though a provider may embed it differently (Gemini's taskType)."""
    return _embed([text], "query")[0]


def embedding_model_id() -> str:
    """Names the vector space the current settings produce, e.g.
    ``openai/text-embedding-3-small@1536``.

    Stored beside each chunk's vector: two vectors are only comparable when
    this matches, so a chunk whose id differs from the current one must be
    re-embedded even if its content_hash is unchanged (plan §6.3, D36).
    """
    provider_name = settings.EMBEDDING_PROVIDER
    model = settings.EMBEDDING_MODELS.get(provider_name, "")
    name = f"{provider_name}/{model}" if model else provider_name
    return f"{name}@{settings.EMBEDDING_DIMENSIONS}"


def _embed(texts, task):
    if not texts:
        return []
    # Checked here, before any call: a vendor rejects an empty input (or
    # bills for it), and a non-string is a caller's bug that should not
    # reach the network.
    for text in texts:
        if not isinstance(text, str) or not text.strip():
            raise EmbeddingError("Cannot embed empty or non-text input")

    provider_name = settings.EMBEDDING_PROVIDER
    provider = get_provider(provider_name)
    # .get(), not [] -- "fake" has no entry in EMBEDDING_MODELS (it has no
    # real model to name), and must not KeyError on the everyday default.
    model = settings.EMBEDDING_MODELS.get(provider_name, "")
    dimensions = settings.EMBEDDING_DIMENSIONS
    batch_size = max(1, settings.EMBEDDING_BATCH_SIZE)

    vectors = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        result = provider.embed(batch, model, dimensions, task)
        if len(result) != len(batch):
            raise EmbeddingError(
                f"{provider_name} returned {len(result)} vectors for {len(batch)} texts"
            )
        vectors.extend(_checked(vector, dimensions, provider_name) for vector in result)
    return vectors


def _checked(vector, dimensions, provider_name):
    """The vector as floats, or EmbeddingError.

    The vector column has a fixed width. A wrong-sized vector (a model that
    ignored the dimensions parameter, a setting changed without a
    re-index) would otherwise fail at INSERT, or worse, be compared with
    vectors from another space.
    """
    if not isinstance(vector, list | tuple):
        raise EmbeddingError(f"{provider_name} returned something that is not a vector")
    if len(vector) != dimensions:
        raise EmbeddingError(
            f"{provider_name} returned a {len(vector)}-dimension vector; "
            f"EMBEDDING_DIMENSIONS is {dimensions}"
        )
    try:
        floats = [float(value) for value in vector]
    except (TypeError, ValueError) as exc:
        raise EmbeddingError(f"{provider_name} returned a non-numeric vector") from exc
    if not all(math.isfinite(value) for value in floats):
        raise EmbeddingError(f"{provider_name} returned a vector with NaN or infinity")
    return floats
