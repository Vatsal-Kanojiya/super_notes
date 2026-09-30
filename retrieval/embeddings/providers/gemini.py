"""The Gemini provider: the Generative Language API's ``batchEmbedContents``.

Plain ``requests``, not the google-genai SDK (D37). Request and response
shapes, from Google's Gemini API reference (``models.batchEmbedContents``):

* ``POST https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents``
  with the key in the ``x-goog-api-key`` header (not the ``?key=`` query
  parameter, which would put it into proxy and access logs).
* request ``{"requests": [{"model": "models/{model}", "content": {"parts":
  [{"text": ...}]}, "taskType": "RETRIEVAL_DOCUMENT" | "RETRIEVAL_QUERY",
  "outputDimensionality": n}, ...]}`` -- every entry repeats the model,
  and it must match the one in the URL.
* response ``{"embeddings": [{"values": [float, ...]}, ...]}``, in request
  order.
* at most 100 requests per batch (EMBEDDING_BATCH_SIZE defaults to 64).

Two things that differ from OpenAI:

* ``taskType`` matters: documents and queries are embedded for their role,
  which Google reports improves retrieval. The boundary passes the task.
* Only the full 3072-dimension output comes back normalised; a truncated
  one (1536 here) must be normalised by the caller, so it is, below.

Checked in this session: the URL exists (a made-up key gets 400 "API key
not valid", not 404). Not checked, and flagged in BUILD_LOG: the request
body and response against a real key, and the 100-per-batch limit -- both
from memory of the docs. The opt-in live test settles them.
"""

import os

from ..errors import EmbeddingError
from ._common import l2_normalise, post_json
from .base import Task

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

TASK_TYPES: dict[Task, str] = {"document": "RETRIEVAL_DOCUMENT", "query": "RETRIEVAL_QUERY"}


class GeminiProvider:
    name = "gemini"

    def embed(self, texts, model, dimensions, task="document"):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise EmbeddingError("GEMINI_API_KEY is not set")

        model_name = model if model.startswith("models/") else f"models/{model}"
        body = post_json(
            "Gemini",
            f"{BASE_URL}/{model_name}:batchEmbedContents",
            headers={"x-goog-api-key": api_key},
            payload={
                "requests": [
                    {
                        "model": model_name,
                        "content": {"parts": [{"text": text}]},
                        "taskType": TASK_TYPES[task],
                        "outputDimensionality": dimensions,
                    }
                    for text in texts
                ]
            },
        )

        embeddings = body.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise EmbeddingError("Gemini returned a different number of embeddings than inputs")
        try:
            return [l2_normalise([float(v) for v in item["values"]]) for item in embeddings]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError("Gemini returned an unexpected response shape") from exc
