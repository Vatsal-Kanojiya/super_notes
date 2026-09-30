"""The embedding boundary, registry and providers (plan §6.2).

No network: the real providers are tested with ``requests.post`` mocked,
as the reference mocks each vendor SDK. The only network calls are the two
opt-in live tests at the bottom, which need both a key and
LIVE_PROVIDER_TESTS=1 (D40).
"""

import math
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import requests
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from retrieval.embeddings import (
    EmbeddingError,
    EmbeddingTransientError,
    embed_query,
    embed_texts,
    embedding_model_id,
)
from retrieval.embeddings.providers._common import l2_normalise
from retrieval.embeddings.providers.fake import FakeProvider, _vector
from retrieval.embeddings.providers.gemini import GeminiProvider
from retrieval.embeddings.providers.openai import OpenAIProvider
from retrieval.embeddings.registry import get_provider

POST = "retrieval.embeddings.providers._common.requests.post"


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def http_response(status=200, body=None, text=""):
    response = MagicMock()
    response.status_code = status
    if isinstance(body, Exception):
        response.json.side_effect = body
    else:
        response.json.return_value = body
    response.text = text
    return response


class TestRunnerTests(SimpleTestCase):
    def test_the_runner_forces_the_fake_provider(self):
        # Whatever .env says: config/test_runner.py, D11.
        self.assertEqual(settings.EMBEDDING_PROVIDER, "fake")


class FakeProviderTests(SimpleTestCase):
    def test_vectors_are_deterministic_unit_length_and_sized(self):
        first = FakeProvider().embed(["Hello world", "Other"], "", 64, "document")
        second = FakeProvider().embed(["Hello world", "Other"], "", 64, "query")

        self.assertEqual(first, second)
        for vector in first:
            self.assertEqual(len(vector), 64)
            self.assertAlmostEqual(math.sqrt(sum(v * v for v in vector)), 1.0)

    def test_texts_sharing_words_are_closer(self):
        query, near, far = FakeProvider().embed(
            [
                "when does my passport expire",
                "Passport renewal: expire date is in March",
                "Grocery list: milk, eggs, bread",
            ],
            "",
            1536,
            "document",
        )

        self.assertGreater(cosine(query, near), cosine(query, far))

    def test_case_and_punctuation_do_not_matter(self):
        a, b = FakeProvider().embed(["Buy MILK!", "buy milk"], "", 256, "document")

        self.assertAlmostEqual(cosine(a, b), 1.0)

    def test_text_without_words_still_gets_a_vector(self):
        vector = _vector("!!! ???", 32)

        self.assertAlmostEqual(sum(v * v for v in vector), 1.0)

    def test_words_that_cancel_out_fall_back_to_a_non_zero_vector(self):
        # With one slot every word collides; find two with opposite signs.
        words = [f"w{i}" for i in range(50)]
        signs = {w: _vector(w, 1)[0] for w in words}
        plus = next(w for w in words if signs[w] > 0)
        minus = next(w for w in words if signs[w] < 0)

        vector = _vector(f"{plus} {minus}", 1)

        self.assertEqual(vector, [1.0])

    def test_l2_normalise_leaves_a_zero_vector_alone(self):
        self.assertEqual(l2_normalise([0.0, 0.0]), [0.0, 0.0])


class RegistryTests(SimpleTestCase):
    def test_names_resolve_to_providers(self):
        self.assertIsInstance(get_provider("fake"), FakeProvider)
        self.assertIsInstance(get_provider("openai"), OpenAIProvider)
        self.assertIsInstance(get_provider("gemini"), GeminiProvider)

    def test_unknown_provider_is_an_embedding_error(self):
        with self.assertRaisesMessage(EmbeddingError, "Unknown embedding provider"):
            get_provider("nope")

    def test_a_provider_that_cannot_be_imported_is_an_embedding_error(self):
        # None in sys.modules makes the import fail, like a missing module.
        with (
            patch.dict(sys.modules, {"retrieval.embeddings.providers.gemini": None}),
            self.assertRaisesMessage(EmbeddingError, "not available"),
        ):
            get_provider("gemini")


class BoundaryTests(SimpleTestCase):
    def test_embed_texts_uses_the_fake_by_default(self):
        vectors = embed_texts(["one", "two"])

        self.assertEqual(len(vectors), 2)
        self.assertEqual(len(vectors[0]), settings.EMBEDDING_DIMENSIONS)

    def test_embed_query_matches_the_document_vector_for_the_fake(self):
        self.assertEqual(embed_query("milk"), embed_texts(["milk"])[0])

    def test_no_texts_means_no_call(self):
        with patch("retrieval.embeddings.get_provider") as get:
            self.assertEqual(embed_texts([]), [])
        get.assert_not_called()

    def test_blank_or_non_text_input_is_refused_before_any_call(self):
        for texts in (["ok", ""], ["  \n"], [None], [3]):
            with (
                self.subTest(texts=texts),
                patch("retrieval.embeddings.get_provider") as get,
                self.assertRaises(EmbeddingError),
            ):
                embed_texts(texts)
            get.assert_not_called()

    @override_settings(EMBEDDING_BATCH_SIZE=2, EMBEDDING_DIMENSIONS=3)
    def test_texts_are_sent_in_batches_and_come_back_in_order(self):
        provider = MagicMock()
        provider.embed.side_effect = lambda texts, model, dims, task: [
            [float(len(t)), 0.0, 0.0] for t in texts
        ]
        with patch("retrieval.embeddings.get_provider", return_value=provider):
            vectors = embed_texts(["a", "bb", "ccc", "dddd", "eeeee"])

        self.assertEqual(
            [call.args[0] for call in provider.embed.call_args_list],
            [
                ["a", "bb"],
                ["ccc", "dddd"],
                ["eeeee"],
            ],
        )
        self.assertEqual([v[0] for v in vectors], [1.0, 2.0, 3.0, 4.0, 5.0])

    @override_settings(
        EMBEDDING_PROVIDER="openai",
        EMBEDDING_MODELS={"openai": "text-embedding-3-small"},
        EMBEDDING_DIMENSIONS=3,
    )
    def test_the_configured_model_dimensions_and_task_reach_the_provider(self):
        provider = MagicMock()
        provider.embed.return_value = [[1.0, 0.0, 0.0]]
        with patch("retrieval.embeddings.get_provider", return_value=provider) as get:
            embed_query("q")

        get.assert_called_once_with("openai")
        provider.embed.assert_called_once_with(["q"], "text-embedding-3-small", 3, "query")

    @override_settings(EMBEDDING_DIMENSIONS=3)
    def test_bad_vectors_are_embedding_errors(self):
        cases = {
            "wrong count": [[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
            "wrong size": [[1.0, 0.0]],
            "not a vector": ["abc"],
            "not numbers": [["a", "b", "c"]],
            "not finite": [[float("nan"), 0.0, 0.0]],
        }
        for label, result in cases.items():
            provider = MagicMock()
            provider.embed.return_value = result
            with (
                self.subTest(label),
                patch("retrieval.embeddings.get_provider", return_value=provider),
                self.assertRaises(EmbeddingError),
            ):
                embed_texts(["one"])

    @override_settings(EMBEDDING_DIMENSIONS=3)
    def test_numeric_strings_are_accepted_as_floats(self):
        provider = MagicMock()
        provider.embed.return_value = [["1", 0, 0.5]]
        with patch("retrieval.embeddings.get_provider", return_value=provider):
            self.assertEqual(embed_texts(["x"]), [[1.0, 0.0, 0.5]])

    def test_model_id_names_provider_model_and_size(self):
        self.assertEqual(embedding_model_id(), f"fake@{settings.EMBEDDING_DIMENSIONS}")
        with override_settings(
            EMBEDDING_PROVIDER="gemini",
            EMBEDDING_MODELS={"gemini": "gemini-embedding-001"},
            EMBEDDING_DIMENSIONS=1536,
        ):
            self.assertEqual(embedding_model_id(), "gemini/gemini-embedding-001@1536")


class HttpErrorTranslationMixin:
    """Shared by both vendors: the same HTTP failures, the same exceptions."""

    def call(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def assert_raises_for(self, exc_class, *, status=None, body=None, side_effect=None):
        with patch(POST) as post:
            if side_effect is not None:
                post.side_effect = side_effect
            else:
                post.return_value = http_response(status, body, text="plain error")
            with self.assertRaises(exc_class) as caught:
                self.call()
        # The key never leaks into a message that ends up in the logs.
        self.assertNotIn("sk-test", str(caught.exception))
        return caught.exception

    def test_auth_and_bad_request_failures_are_not_retried(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                exc = self.assert_raises_for(
                    EmbeddingError, status=status, body={"error": {"message": "nope"}}
                )
                self.assertNotIsInstance(exc, EmbeddingTransientError)
                self.assertIn(str(status), str(exc))
                self.assertIn("nope", str(exc))

    def test_rate_limits_and_server_errors_are_transient(self):
        for status in (429, 500, 502, 503):
            with self.subTest(status=status):
                self.assert_raises_for(
                    EmbeddingTransientError, status=status, body={"error": {"message": "busy"}}
                )

    def test_connection_failures_and_timeouts_are_transient(self):
        for error in (requests.ConnectionError("down"), requests.Timeout("slow")):
            with self.subTest(error=type(error).__name__):
                self.assert_raises_for(EmbeddingTransientError, side_effect=error)

    def test_other_request_failures_are_not_retried(self):
        self.assert_raises_for(EmbeddingError, side_effect=requests.TooManyRedirects("loop"))

    def test_a_non_json_error_body_is_quoted_briefly(self):
        exc = self.assert_raises_for(EmbeddingError, status=400, body=ValueError("no json"))

        self.assertIn("plain error", str(exc))

    def test_a_non_json_success_body_is_an_embedding_error(self):
        self.assert_raises_for(EmbeddingError, status=200, body=ValueError("no json"))

    def test_a_non_object_success_body_is_an_embedding_error(self):
        self.assert_raises_for(EmbeddingError, status=200, body=["a", "list"])

    def test_a_wrong_number_of_embeddings_is_an_embedding_error(self):
        self.assert_raises_for(EmbeddingError, status=200, body={})


@patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"})
class OpenAIProviderTests(HttpErrorTranslationMixin, SimpleTestCase):
    def call(self):
        return OpenAIProvider().embed(["a", "b"], "text-embedding-3-small", 3, "document")

    def test_request_shape(self):
        body = {
            "object": "list",
            "data": [
                {"object": "embedding", "index": 0, "embedding": [1.0, 0.0, 0.0]},
                {"object": "embedding", "index": 1, "embedding": [0.0, 1.0, 0.0]},
            ],
        }
        with patch(POST, return_value=http_response(200, body)) as post:
            vectors = self.call()

        self.assertEqual(vectors, [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "https://api.openai.com/v1/embeddings")
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer sk-test"})
        self.assertEqual(
            kwargs["json"],
            {
                "model": "text-embedding-3-small",
                "input": ["a", "b"],
                "dimensions": 3,
                "encoding_format": "float",
            },
        )
        self.assertIsNotNone(kwargs["timeout"])

    def test_results_are_reordered_by_index(self):
        body = {
            "data": [
                {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                {"index": 0, "embedding": [1.0, 0.0, 0.0]},
            ]
        }
        with patch(POST, return_value=http_response(200, body)):
            self.assertEqual(self.call(), [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])

    def test_a_malformed_item_is_an_embedding_error(self):
        self.assert_raises_for(EmbeddingError, status=200, body={"data": [{"index": 0}, {}]})

    def test_an_exhausted_quota_is_not_retried(self):
        # OpenAI's 429 for "no credit left" will not fix itself by waiting.
        exc = self.assert_raises_for(
            EmbeddingError,
            status=429,
            body={"error": {"message": "quota", "code": "insufficient_quota"}},
        )

        self.assertNotIsInstance(exc, EmbeddingTransientError)

    def test_a_key_quoted_back_by_the_vendor_is_redacted(self):
        exc = self.assert_raises_for(
            EmbeddingError,
            status=401,
            body={"error": {"message": "Incorrect API key provided: sk-test.", "code": None}},
        )

        self.assertIn("[redacted]", str(exc))

    def test_a_missing_key_fails_before_any_call(self):
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": ""}),
            patch(POST) as post,
            self.assertRaisesMessage(EmbeddingError, "OPENAI_API_KEY"),
        ):
            self.call()
        post.assert_not_called()

    @override_settings(
        EMBEDDING_PROVIDER="openai",
        EMBEDDING_MODELS={"openai": "text-embedding-3-small"},
        EMBEDDING_DIMENSIONS=4,
    )
    def test_a_model_ignoring_the_dimensions_is_caught_at_the_boundary(self):
        body = {"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]}
        with (
            patch(POST, return_value=http_response(200, body)),
            self.assertRaisesMessage(EmbeddingError, "3-dimension"),
        ):
            embed_texts(["a"])

    @override_settings(
        EMBEDDING_PROVIDER="openai",
        EMBEDDING_MODELS={"openai": "text-embedding-3-small"},
        EMBEDDING_DIMENSIONS=2,
        EMBEDDING_BATCH_SIZE=2,
    )
    def test_the_boundary_batches_real_calls(self):
        def answer(url, json, headers, timeout):
            data = [{"index": i, "embedding": [1.0, 0.0]} for i in range(len(json["input"]))]
            return http_response(200, {"data": data})

        with patch(POST, side_effect=answer) as post:
            vectors = embed_texts(["a", "b", "c"])

        self.assertEqual(len(vectors), 3)
        self.assertEqual(
            [c.kwargs["json"]["input"] for c in post.call_args_list], [["a", "b"], ["c"]]
        )


@patch.dict(os.environ, {"GEMINI_API_KEY": "sk-test"})
class GeminiProviderTests(HttpErrorTranslationMixin, SimpleTestCase):
    def call(self, task="document", model="gemini-embedding-001"):
        return GeminiProvider().embed(["a", "b"], model, 3, task)

    def test_request_shape(self):
        body = {"embeddings": [{"values": [3.0, 4.0, 0.0]}, {"values": [0.0, 0.0, 2.0]}]}
        with patch(POST, return_value=http_response(200, body)) as post:
            vectors = self.call()

        # Truncated Gemini vectors are not unit length; the provider fixes that.
        self.assertEqual(vectors, [[0.6, 0.8, 0.0], [0.0, 0.0, 1.0]])
        self.assertEqual(
            post.call_args.args[0],
            "https://generativelanguage.googleapis.com/v1beta/"
            "models/gemini-embedding-001:batchEmbedContents",
        )
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"], {"x-goog-api-key": "sk-test"})
        self.assertEqual(
            kwargs["json"]["requests"][0],
            {
                "model": "models/gemini-embedding-001",
                "content": {"parts": [{"text": "a"}]},
                "taskType": "RETRIEVAL_DOCUMENT",
                "outputDimensionality": 3,
            },
        )
        self.assertEqual(len(kwargs["json"]["requests"]), 2)
        self.assertIsNotNone(kwargs["timeout"])

    def test_queries_are_embedded_as_queries(self):
        body = {"embeddings": [{"values": [1.0, 0.0, 0.0]}] * 2}
        with patch(POST, return_value=http_response(200, body)) as post:
            self.call(task="query")

        self.assertEqual(
            post.call_args.kwargs["json"]["requests"][0]["taskType"], "RETRIEVAL_QUERY"
        )

    def test_a_model_given_with_its_prefix_is_not_prefixed_twice(self):
        body = {"embeddings": [{"values": [1.0, 0.0, 0.0]}] * 2}
        with patch(POST, return_value=http_response(200, body)) as post:
            self.call(model="models/gemini-embedding-001")

        self.assertIn("/models/gemini-embedding-001:", post.call_args.args[0])
        self.assertEqual(
            post.call_args.kwargs["json"]["requests"][0]["model"], "models/gemini-embedding-001"
        )

    def test_a_malformed_item_is_an_embedding_error(self):
        self.assert_raises_for(
            EmbeddingError, status=200, body={"embeddings": [{"values": ["x"]}, {}]}
        )

    def test_a_gemini_error_status_is_quoted(self):
        exc = self.assert_raises_for(
            EmbeddingError,
            status=400,
            body={"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}},
        )

        self.assertIn("bad", str(exc))

    def test_a_missing_key_fails_before_any_call(self):
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": ""}),
            patch(POST) as post,
            self.assertRaisesMessage(EmbeddingError, "GEMINI_API_KEY"),
        ):
            self.call()
        post.assert_not_called()


# -- Opt-in live tests ------------------------------------------------------
#
# Each spends a fraction of a cent. Skipped unless LIVE_PROVIDER_TESTS=1 *and*
# the vendor's key is set: a key alone is not consent, because .env feeds
# os.environ and a developer's key there must not make the suite spend
# money (D11, D40). override_settings beats the runner's forced "fake".

LIVE = os.environ.get("LIVE_PROVIDER_TESTS") == "1"


class LiveProviderMixin:
    def check_live(self):
        vectors = embed_texts(
            [
                "Passport renewal: the passport expires in March 2027.",
                "Grocery list: milk, eggs, bread.",
            ]
        )
        query = embed_query("When does my passport expire?")

        for vector in [*vectors, query]:
            self.assertEqual(len(vector), 1536)
            self.assertAlmostEqual(math.sqrt(sum(v * v for v in vector)), 1.0, places=3)
        self.assertGreater(cosine(query, vectors[0]), cosine(query, vectors[1]))


@unittest.skipUnless(LIVE and os.environ.get("OPENAI_API_KEY"), "live OpenAI test is opt-in")
@override_settings(
    EMBEDDING_PROVIDER="openai",
    EMBEDDING_MODELS={"openai": "text-embedding-3-small"},
    EMBEDDING_DIMENSIONS=1536,
)
class OpenAILiveTests(LiveProviderMixin, SimpleTestCase):
    def test_live_embeddings(self):
        self.check_live()


@unittest.skipUnless(LIVE and os.environ.get("GEMINI_API_KEY"), "live Gemini test is opt-in")
@override_settings(
    EMBEDDING_PROVIDER="gemini",
    EMBEDDING_MODELS={"gemini": "gemini-embedding-001"},
    EMBEDDING_DIMENSIONS=1536,
)
class GeminiLiveTests(LiveProviderMixin, SimpleTestCase):
    def test_live_embeddings(self):
        self.check_live()
