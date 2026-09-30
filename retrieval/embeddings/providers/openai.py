"""The OpenAI provider: ``POST https://api.openai.com/v1/embeddings``.

Plain ``requests``, not the openai SDK (D37). Request and response shapes,
from OpenAI's API reference for the embeddings endpoint:

* request ``{"model", "input": [str, ...], "dimensions", "encoding_format":
  "float"}`` with ``Authorization: Bearer $OPENAI_API_KEY``. ``dimensions``
  is honoured by the text-embedding-3 models only; the returned vectors
  are already unit length at any size.
* response ``{"object": "list", "data": [{"object": "embedding", "index":
  i, "embedding": [float, ...]}, ...], "model", "usage"}``. ``data`` is
  put back in ``index`` order rather than trusted to arrive in it.
* limits: at most 2048 inputs per request and 8192 tokens per input, far
  above one batch of chunks. An empty string is rejected with a 400, which
  is why the package boundary refuses blank text before any call.

Checked in this session: the URL and the auth header (a made-up key gets
401 "Incorrect API key"). Not checked against a real key (BUILD_LOG, Phase
3a); the opt-in live test in retrieval/tests/test_embeddings.py does that.
"""

import os

from ..errors import EmbeddingError
from ._common import post_json

URL = "https://api.openai.com/v1/embeddings"


class OpenAIProvider:
    name = "openai"

    def embed(self, texts, model, dimensions, task="document"):
        # task is ignored: OpenAI embeds queries and passages alike.
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise EmbeddingError("OPENAI_API_KEY is not set")

        body = post_json(
            "OpenAI",
            URL,
            headers={"Authorization": f"Bearer {api_key}"},
            payload={
                "model": model,
                "input": list(texts),
                "dimensions": dimensions,
                "encoding_format": "float",
            },
        )

        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingError("OpenAI returned a different number of embeddings than inputs")
        try:
            ordered = sorted(data, key=lambda item: item["index"])
            return [list(item["embedding"]) for item in ordered]
        except (KeyError, TypeError) as exc:
            raise EmbeddingError("OpenAI returned an unexpected response shape") from exc
