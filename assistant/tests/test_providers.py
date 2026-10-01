"""The real chat providers, with requests.post mocked -- no network in tests.

Each provider also has one opt-in live test, skipped unless the vendor's key
is set *and* LIVE_PROVIDER_TESTS=1 (D40). It goes through complete() with
CHAT_PROVIDER overridden, so it proves the whole boundary against the real
API, and asks for a one-word answer so a run costs a fraction of a cent.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

import requests
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from assistant.chat import BilledChatError, ChatError, TransientChatError, complete
from assistant.chat.providers.claude import ClaudeProvider
from assistant.chat.providers.gemini import GeminiProvider
from assistant.chat.providers.openai import OpenAIProvider

POST = "assistant.chat.providers._http.requests.post"


def http(status=200, data=None, text=""):
    response = MagicMock(status_code=status, text=text or str(data))
    if isinstance(data, Exception):
        response.json.side_effect = data
    else:
        response.json.return_value = data
    return response


# Real calls spend money, and a key in .env reaches the process environment,
# so a key alone must not turn them on: LIVE_PROVIDER_TESTS=1 as well (D40).
LIVE = os.environ.get("LIVE_PROVIDER_TESTS") == "1"


class ErrorTranslationMixin:
    """The same HTTP failures, translated the same way by every provider."""

    provider_class = None
    env = {}
    ok_body = {}

    def call(self):
        with patch.dict(os.environ, self.env):
            return self.provider_class().complete("system", "user", "model-x", 100)

    def test_transient_statuses_are_retryable(self):
        for status in (408, 429, 500, 503, 529):
            with self.subTest(status=status), patch(POST, return_value=http(status, {})):
                with self.assertRaises(TransientChatError):
                    self.call()

    def test_permanent_statuses_are_chat_errors(self):
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status), patch(POST, return_value=http(status, {})):
                with self.assertRaises(ChatError) as caught:
                    self.call()
                # Rejected before generating: nothing billed (D500).
                self.assertNotIsInstance(caught.exception, BilledChatError)

    def test_connection_failures_and_timeouts_are_retryable(self):
        for exc in (requests.ConnectionError("down"), requests.Timeout("slow")):
            with self.subTest(exc=exc), patch(POST, side_effect=exc):
                with self.assertRaises(TransientChatError):
                    self.call()

    def test_other_request_errors_are_chat_errors(self):
        with patch(POST, side_effect=requests.exceptions.InvalidURL("bad")):
            with self.assertRaises(ChatError):
                self.call()

    def test_a_body_that_is_not_json_is_a_chat_error(self):
        with patch(POST, return_value=http(200, ValueError("no json"), text="<html>")):
            with self.assertRaises(ChatError) as caught:
                self.call()
        self.assertNotIsInstance(caught.exception, BilledChatError)

    def test_a_missing_key_is_a_chat_error_before_any_request(self):
        names = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
        cleared = dict.fromkeys(names, "")
        with patch.dict(os.environ, cleared), patch(POST) as mock_post:
            with self.assertRaisesMessage(ChatError, "is not set"):
                self.provider_class().complete("s", "u", "m", 10)
        mock_post.assert_not_called()

    def test_the_timeout_is_bounded(self):
        with patch(POST, return_value=http(200, self.ok_body)) as mock_post:
            self.call()
        self.assertEqual(mock_post.call_args.kwargs["timeout"], (5, settings.CHAT_TIMEOUT_SECONDS))


def claude_body(stop_reason="end_turn", content=None, usage=None):
    return {
        "model": "claude-haiku-4-5-20251001",
        "stop_reason": stop_reason,
        "content": content if content is not None else [{"type": "text", "text": "Friday [1]."}],
        "usage": usage if usage is not None else {"input_tokens": 120, "output_tokens": 8},
    }


class ClaudeProviderTests(ErrorTranslationMixin, SimpleTestCase):
    provider_class = ClaudeProvider
    env = {"ANTHROPIC_API_KEY": "sk-ant-test"}
    ok_body = claude_body()

    def test_request_shape(self):
        with patch(POST, return_value=http(200, claude_body())) as mock_post:
            self.call()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://api.anthropic.com/v1/messages")
        self.assertEqual(
            kwargs["headers"],
            {
                "x-api-key": "sk-ant-test",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        self.assertEqual(
            kwargs["json"],
            {
                "model": "model-x",
                "max_tokens": 100,
                "system": "system",
                "messages": [{"role": "user", "content": "user"}],
            },
        )

    def test_text_and_usage_are_extracted(self):
        content = [
            {"type": "thinking", "thinking": ""},
            {"type": "text", "text": "Friday "},
            {"type": "text", "text": "[1]."},
        ]
        with patch(POST, return_value=http(200, claude_body(content=content))):
            result = self.call()
        self.assertEqual(result.text, "Friday [1].")
        self.assertEqual(result.provider, "claude")
        self.assertEqual(result.model, "claude-haiku-4-5-20251001")
        self.assertEqual((result.input_tokens, result.output_tokens), (120, 8))

    def test_missing_usage_counts_as_zero_and_model_falls_back(self):
        body = claude_body(usage={})
        del body["model"]
        with patch(POST, return_value=http(200, body)):
            result = self.call()
        self.assertEqual(
            (result.model, result.input_tokens, result.output_tokens), ("model-x", 0, 0)
        )

    def test_stop_reasons(self):
        for stop_reason, message in (
            ("refusal", "declined"),
            ("max_tokens", "cut off"),
            ("pause_turn", "stopped unexpectedly"),
        ):
            with self.subTest(stop_reason=stop_reason):
                body = claude_body(stop_reason=stop_reason)
                with patch(POST, return_value=http(200, body)):
                    with self.assertRaisesMessage(BilledChatError, message) as caught:
                        self.call()
                # Generated, so billed, with what it cost (D500).
                self.assertEqual(
                    caught.exception.cost(),
                    {
                        "provider": "claude",
                        "model": "claude-haiku-4-5-20251001",
                        "input_tokens": 120,
                        "output_tokens": 8,
                    },
                )

    def test_no_text_is_a_chat_error(self):
        with patch(POST, return_value=http(200, claude_body(content=[]))):
            with self.assertRaisesMessage(BilledChatError, "no text") as caught:
                self.call()
        self.assertEqual(caught.exception.output_tokens, 8)

    def test_a_vendor_error_body_is_kept_short(self):
        with patch(POST, return_value=http(400, {}, text="x" * 5000)):
            with self.assertRaises(ChatError) as caught:
                self.call()
        self.assertLess(len(str(caught.exception)), 400)


def openai_body(status="completed", output=None, **extra):
    body = {
        "model": "gpt-5-mini-2025-08-07",
        "status": status,
        "output": output
        if output is not None
        else [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": "Friday [1]."}]},
        ],
        "usage": {"input_tokens": 90, "output_tokens": 40},
    }
    body.update(extra)
    return body


class OpenAIProviderTests(ErrorTranslationMixin, SimpleTestCase):
    provider_class = OpenAIProvider
    env = {"OPENAI_API_KEY": "sk-test"}
    ok_body = openai_body()

    def test_request_shape(self):
        with patch(POST, return_value=http(200, openai_body())) as mock_post:
            self.call()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://api.openai.com/v1/responses")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual(
            kwargs["json"],
            {
                "model": "model-x",
                "instructions": "system",
                "input": "user",
                "max_output_tokens": 100,
                "reasoning": {"effort": "low"},
                "store": False,
            },
        )

    def test_text_and_usage_are_extracted(self):
        with patch(POST, return_value=http(200, openai_body())):
            result = self.call()
        self.assertEqual(result.text, "Friday [1].")
        self.assertEqual((result.provider, result.model), ("openai", "gpt-5-mini-2025-08-07"))
        self.assertEqual((result.input_tokens, result.output_tokens), (90, 40))

    def test_failures(self):
        refusal = [{"type": "message", "content": [{"type": "refusal", "refusal": "No."}]}]
        cases = (
            (openai_body(status="failed", error={"message": "boom"}), "boom"),
            (openai_body(status="failed"), "unknown error"),
            (
                openai_body(status="incomplete", incomplete_details={"reason": "content_filter"}),
                "declined",
            ),
            (
                openai_body(
                    status="incomplete", incomplete_details={"reason": "max_output_tokens"}
                ),
                "cut off",
            ),
            (openai_body(output=refusal), "declined"),
            (openai_body(output=[{"type": "reasoning"}]), "no text"),
        )
        for body, message in cases:
            with self.subTest(message=message), patch(POST, return_value=http(200, body)):
                with self.assertRaisesMessage(BilledChatError, message) as caught:
                    self.call()
                cost = caught.exception.cost()
                self.assertEqual((cost["provider"], cost["model"]), ("openai", body["model"]))
                self.assertEqual((cost["input_tokens"], cost["output_tokens"]), (90, 40))

    def test_a_failed_response_with_a_transient_error_is_retried(self):
        # D512: a server error or a rate limit is worth a retry, not a failed ask.
        cases = (
            ({"code": "server_error", "message": "oops"}, TransientChatError),
            ({"code": "rate_limit_exceeded", "message": "slow"}, TransientChatError),
            ({"type": "server_error", "code": None, "message": "x"}, TransientChatError),
            ({"code": "invalid_prompt", "message": "no"}, ChatError),
        )
        for error, expected in cases:
            body = openai_body(status="failed", error=error)
            with self.subTest(error=error), patch(POST, return_value=http(200, body)):
                with self.assertRaises(expected):
                    self.call()


def gemini_body(finish_reason="STOP", parts=None, **extra):
    body = {
        "modelVersion": "gemini-2.5-flash-lite",
        "candidates": [
            {
                "finishReason": finish_reason,
                "content": {
                    "role": "model",
                    "parts": parts if parts is not None else [{"text": "Friday [1]."}],
                },
            }
        ],
        "usageMetadata": {"promptTokenCount": 70, "candidatesTokenCount": 6},
    }
    body.update(extra)
    return body


class GeminiProviderTests(ErrorTranslationMixin, SimpleTestCase):
    provider_class = GeminiProvider
    env = {"GEMINI_API_KEY": "gm-test"}
    ok_body = gemini_body()

    def test_request_shape(self):
        with patch(POST, return_value=http(200, gemini_body())) as mock_post:
            self.call()
        args, kwargs = mock_post.call_args
        self.assertEqual(
            args[0],
            "https://generativelanguage.googleapis.com/v1beta/models/model-x:generateContent",
        )
        # The key travels in a header, never in the URL.
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "gm-test")
        self.assertNotIn("gm-test", args[0])
        self.assertEqual(
            kwargs["json"],
            {
                "systemInstruction": {"parts": [{"text": "system"}]},
                "contents": [{"role": "user", "parts": [{"text": "user"}]}],
                "generationConfig": {"maxOutputTokens": 100},
            },
        )

    def test_the_older_key_name_is_accepted(self):
        env = {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": "old-key"}
        with patch.dict(os.environ, env), patch(POST, return_value=http(200, gemini_body())) as p:
            GeminiProvider().complete("s", "u", "m", 10)
        self.assertEqual(p.call_args.kwargs["headers"]["x-goog-api-key"], "old-key")

    def test_a_model_name_cannot_rewrite_the_url(self):
        with patch(POST, return_value=http(200, gemini_body())) as mock_post:
            with patch.dict(os.environ, self.env):
                GeminiProvider().complete("s", "u", "../../x?key=1", 10)
        self.assertIn("/models/..%2F..%2Fx%3Fkey%3D1:generateContent", mock_post.call_args[0][0])

    def test_text_and_usage_are_extracted_without_thoughts(self):
        parts = [{"text": "thinking...", "thought": True}, {"text": "Friday "}, {"text": "[1]."}]
        body = gemini_body(parts=parts)
        body["usageMetadata"]["thoughtsTokenCount"] = 30
        with patch(POST, return_value=http(200, body)):
            result = self.call()
        self.assertEqual(result.text, "Friday [1].")
        self.assertEqual((result.provider, result.model), ("gemini", "gemini-2.5-flash-lite"))
        self.assertEqual((result.input_tokens, result.output_tokens), (70, 36))

    def test_failures(self):
        cases = (
            ({"promptFeedback": {"blockReason": "SAFETY"}}, "declined"),
            ({"candidates": []}, "no answer"),
            (gemini_body(finish_reason="MAX_TOKENS"), "cut off"),
            (gemini_body(finish_reason="SAFETY"), "declined"),
            (gemini_body(finish_reason="RECITATION"), "declined"),
            (gemini_body(parts=[]), "no text"),
        )
        for body, message in cases:
            with self.subTest(message=message), patch(POST, return_value=http(200, body)):
                with self.assertRaisesMessage(BilledChatError, message) as caught:
                    self.call()
                usage = body.get("usageMetadata") or {}
                self.assertEqual(caught.exception.input_tokens, usage.get("promptTokenCount", 0))
                self.assertEqual(caught.exception.provider, "gemini")


LIVE_SYSTEM = "Reply with exactly one word."
LIVE_USER = "Say: ready"


class LiveProviderTests(SimpleTestCase):
    """Real API calls. Opt in with LIVE_PROVIDER_TESTS=1 and the vendor's key."""

    def ask(self, provider):
        with override_settings(CHAT_PROVIDER=provider, CHAT_MAX_OUTPUT_TOKENS=512):
            result = complete(LIVE_SYSTEM, LIVE_USER)
        self.assertEqual(result.provider, provider)
        self.assertTrue(result.text)
        self.assertGreater(result.input_tokens, 0)
        self.assertGreater(result.output_tokens, 0)

    @unittest.skipUnless(LIVE and os.environ.get("ANTHROPIC_API_KEY"), "live Claude test is opt-in")
    def test_claude_live(self):
        self.ask("claude")

    @unittest.skipUnless(LIVE and os.environ.get("OPENAI_API_KEY"), "live OpenAI test is opt-in")
    def test_openai_live(self):
        self.ask("openai")

    @unittest.skipUnless(
        LIVE and (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")),
        "live Gemini test is opt-in",
    )
    def test_gemini_live(self):
        self.ask("gemini")
