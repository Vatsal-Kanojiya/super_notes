"""Streamed answers from the real providers, with requests.post mocked -- no network.

Each vendor's happy path is a handwritten recording of its Server-Sent
Events (fixtures/streams/, written from the vendor's documented events),
fed to the provider in one piece, a byte at a time and in odd-sized chunks:
the result must not depend on where the network cut the stream. The
failures are built from the same events: an error event, a stream that is
cut off, a stop reason that is not a complete answer.
"""

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from assistant.chat import ChatError, ChatResult, TransientChatError
from assistant.chat.providers._http import MAX_SSE_LINE_BYTES, ServerSentEvent, parse_sse
from assistant.chat.providers.claude import ClaudeProvider
from assistant.chat.providers.gemini import GeminiProvider
from assistant.chat.providers.openai import OpenAIProvider

POST = "assistant.chat.providers._http.requests.post"
FIXTURES = Path(__file__).parent / "fixtures" / "streams"
ANSWER = "Your passport expires in March 2027 [1] — renew it early."


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def pieces(body: bytes, size: int) -> list[bytes]:
    return [body[i : i + size] for i in range(0, len(body), size)]


class StreamResponse:
    """What requests.post(..., stream=True) returns, as far as _http reads it."""

    def __init__(self, body=b"", status=200, chunk=None, fail_with=None):
        self.status_code = status
        self.body = body
        self.chunks = pieces(body, chunk) if chunk else [body]
        self.fail_with = fail_with
        self.closed = False

    @property
    def text(self):
        return self.body.decode("utf-8", errors="replace")

    def iter_content(self, chunk_size=None):
        assert chunk_size is None, "read as it arrives, not in fixed blocks"
        yield from self.chunks
        if self.fail_with is not None:
            raise self.fail_with

    def close(self):
        self.closed = True


def sse(*events, crlf=False) -> bytes:
    """Events as a vendor sends them: (name, data) pairs, or data alone."""
    nl = "\r\n" if crlf else "\n"
    out = []
    for event in events:
        name, data = event if isinstance(event, tuple) else (None, event)
        if not isinstance(data, str):
            data = json.dumps(data)
        out.append((f"event: {name}{nl}" if name else "") + f"data: {data}{nl}{nl}")
    return "".join(out).encode()


class ParseSseTests(SimpleTestCase):
    def parse(self, body: bytes, size: int | None = None):
        return list(parse_sse(pieces(body, size) if size else [body]))

    def test_events_are_the_same_however_the_bytes_are_chunked(self):
        for name in ("claude_answer.sse", "openai_answer.sse", "gemini_answer.sse"):
            body = fixture(name)
            whole = self.parse(body)
            self.assertGreater(len(whole), 2)
            for size in (1, 2, 3, 7, 64):
                with self.subTest(name=name, size=size):
                    self.assertEqual(self.parse(body, size), whole)

    def test_a_character_split_between_chunks_arrives_whole(self):
        body = "data: —₹\n\n".encode()
        self.assertEqual(self.parse(body, 1), [ServerSentEvent("message", "—₹")])

    def test_fields_comments_and_line_endings(self):
        body = (
            b": keep-alive\r\n"
            b"id: 7\r\nretry: 1000\r\n"
            b"event: first\r\ndata: one\r\ndata:two\r\n\r\n"
            b": another keep-alive\n\n"
            b"event: no-data\n\n"
            b'data: {"a": 1}\n\n'
        )
        self.assertEqual(
            self.parse(body),
            [ServerSentEvent("first", "one\ntwo"), ServerSentEvent("message", '{"a": 1}')],
        )

    def test_an_event_the_stream_ends_inside_of_is_dropped(self):
        self.assertEqual(
            self.parse(b'data: {"a": 1}\n\ndata: {"b"'), [ServerSentEvent("message", '{"a": 1}')]
        )
        self.assertEqual(self.parse(b"data: complete line, no blank line\n"), [])

    def test_a_line_that_never_ends_is_refused(self):
        endless = (b"x" * 65536 for _ in range(MAX_SSE_LINE_BYTES // 65536 + 2))
        with self.assertRaises(ChatError):
            list(parse_sse(endless))


class StreamingMixin:
    """What every vendor's stream must do, run against each one."""

    provider_class = None
    env = {}
    fixture_name = ""
    model = "model-x"

    def stream(self, response):
        with patch.dict(os.environ, self.env), patch(POST, return_value=response) as post:
            items = list(self.provider_class().stream("system", "user", self.model, 100))
        self.post = post
        return items

    def deltas_and_result(self, items):
        self.assertIsInstance(items[-1], ChatResult)
        self.assertTrue(all(isinstance(item, str) for item in items[:-1]))
        return items[:-1], items[-1]

    def test_the_answer_streams_whole_however_it_is_chunked(self):
        for size in (None, 1, 5, 333):
            with self.subTest(chunk=size):
                response = StreamResponse(fixture(self.fixture_name), chunk=size)
                deltas, result = self.deltas_and_result(self.stream(response))
                self.assertEqual(deltas, ["Your passport", ANSWER.removeprefix("Your passport")])
                self.assertEqual(result.text, ANSWER)
                self.assertEqual(result.provider, self.provider_class.name)
                self.assert_usage(result)
                self.assertTrue(response.closed)

    def test_the_request_asks_for_a_stream_with_the_usual_timeouts(self):
        self.stream(StreamResponse(fixture(self.fixture_name)))
        kwargs = self.post.call_args.kwargs
        self.assertIs(kwargs["stream"], True)
        self.assertEqual(kwargs["timeout"], (5, settings.CHAT_TIMEOUT_SECONDS))
        self.assert_stream_request(self.post.call_args)

    def test_error_statuses_are_translated_before_any_delta(self):
        for status, error in (
            (429, TransientChatError),
            (529, TransientChatError),
            (503, TransientChatError),
            (400, ChatError),
            (401, ChatError),
        ):
            with self.subTest(status=status):
                response = StreamResponse(b'{"error": "nope"}', status=status)
                with self.assertRaises(error):
                    self.stream(response)
                self.assertTrue(response.closed)

    def test_connection_failures_are_retryable(self):
        for exc in (requests.ConnectionError("down"), requests.Timeout("slow")):
            with (
                self.subTest(exc=exc),
                patch.dict(os.environ, self.env),
                patch(POST, side_effect=exc),
            ):
                with self.assertRaises(TransientChatError):
                    list(self.provider_class().stream("system", "user", self.model, 100))

    def test_a_dropped_connection_mid_stream_is_retryable_after_the_deltas_so_far(self):
        body = fixture(self.fixture_name)
        # Up to the end of the event with the first delta: whole events only,
        # the vendor's last one missing.
        start = body.index(b"Your passport")
        ends = [i + len(end) for end in (b"\n\n", b"\r\n\r\n") if (i := body.find(end, start)) > 0]
        cut = body[: min(ends)]
        for failure in (
            requests.exceptions.ChunkedEncodingError("connection broken"),
            requests.ConnectionError("read timed out"),
            None,  # the stream simply ends, without its last event
        ):
            with self.subTest(failure=failure):
                response = StreamResponse(cut, fail_with=failure)
                received = []
                with self.assertRaises(TransientChatError):
                    with patch.dict(os.environ, self.env), patch(POST, return_value=response):
                        for item in self.provider_class().stream("s", "u", self.model, 100):
                            received.append(item)
                self.assertIn("Your passport", received)
                self.assertTrue(response.closed)

    def test_an_event_that_is_not_json_is_a_chat_error(self):
        with self.assertRaises(ChatError):
            self.stream(StreamResponse(b"data: {not json\n\n"))

    def test_closing_the_stream_early_closes_the_connection(self):
        response = StreamResponse(fixture(self.fixture_name))
        with patch.dict(os.environ, self.env), patch(POST, return_value=response):
            items = self.provider_class().stream("system", "user", self.model, 100)
            self.assertEqual(next(items), "Your passport")
            items.close()
        self.assertTrue(response.closed)

    def test_a_missing_key_fails_before_any_request(self):
        names = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
        with patch.dict(os.environ, dict.fromkeys(names, "")), patch(POST) as post:
            with self.assertRaises(ChatError):
                list(self.provider_class().stream("system", "user", self.model, 100))
        post.assert_not_called()


class ClaudeStreamTests(StreamingMixin, SimpleTestCase):
    provider_class = ClaudeProvider
    env = {"ANTHROPIC_API_KEY": "sk-ant-test"}
    fixture_name = "claude_answer.sse"

    def assert_usage(self, result):
        self.assertEqual(result.model, "claude-haiku-4-5-20251001")
        self.assertEqual((result.input_tokens, result.output_tokens), (120, 42))

    def assert_stream_request(self, call):
        self.assertEqual(call.args[0], "https://api.anthropic.com/v1/messages")
        self.assertEqual(
            call.kwargs["json"],
            {
                "model": "model-x",
                "max_tokens": 100,
                "system": "system",
                "messages": [{"role": "user", "content": "user"}],
                "stream": True,
            },
        )

    def events(self, *tail):
        start = {"type": "message_start", "message": {"model": "m", "usage": {"input_tokens": 5}}}
        delta = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Half"}}
        return sse(("message_start", start), ("content_block_delta", delta), *tail)

    def test_an_error_event_is_translated_like_its_status(self):
        cases = {
            "overloaded_error": TransientChatError,
            "rate_limit_error": TransientChatError,
            "api_error": TransientChatError,
            "invalid_request_error": ChatError,
            "authentication_error": ChatError,
        }
        for kind, error in cases.items():
            with self.subTest(kind=kind):
                body = self.events(
                    ("error", {"type": "error", "error": {"type": kind, "message": "x"}})
                )
                received = []
                with self.assertRaises(error) as caught:
                    with (
                        patch.dict(os.environ, self.env),
                        patch(POST, return_value=StreamResponse(body)),
                    ):
                        received.extend(ClaudeProvider().stream("s", "u", "m", 100))
                self.assertEqual(received, ["Half"])
                self.assertIn(kind, str(caught.exception))

    def test_stop_reasons_are_checked_as_complete_does(self):
        for stop_reason in ("max_tokens", "refusal", "tool_use"):
            with self.subTest(stop_reason=stop_reason):
                body = self.events(
                    (
                        "message_delta",
                        {"type": "message_delta", "delta": {"stop_reason": stop_reason}},
                    ),
                    ("message_stop", {"type": "message_stop"}),
                )
                with self.assertRaises(ChatError):
                    self.stream(StreamResponse(body))

    def test_no_text_is_a_chat_error(self):
        body = sse(
            ("message_start", {"type": "message_start", "message": {}}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
            ("message_stop", {"type": "message_stop"}),
        )
        with self.assertRaisesMessage(ChatError, "no text"):
            self.stream(StreamResponse(body))


class OpenAIStreamTests(StreamingMixin, SimpleTestCase):
    provider_class = OpenAIProvider
    env = {"OPENAI_API_KEY": "sk-test"}
    fixture_name = "openai_answer.sse"

    def assert_usage(self, result):
        self.assertEqual(result.model, "gpt-5-mini-2025-08-07")
        self.assertEqual((result.input_tokens, result.output_tokens), (130, 40))

    def assert_stream_request(self, call):
        self.assertEqual(call.args[0], "https://api.openai.com/v1/responses")
        body = call.kwargs["json"]
        self.assertIs(body["stream"], True)
        self.assertEqual(body["max_output_tokens"], 100)
        self.assertIs(body["store"], False)

    def events(self, *tail):
        delta = {"type": "response.output_text.delta", "delta": "Half"}
        return sse(("response.output_text.delta", delta), *tail)

    def test_the_end_events_are_checked_as_complete_does(self):
        cases = [
            ("response.failed", {"status": "failed", "error": {"message": "boom"}}, "failed"),
            (
                "response.incomplete",
                {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
                "cut off",
            ),
            (
                "response.incomplete",
                {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}},
                "declined",
            ),
        ]
        for name, response, message in cases:
            with self.subTest(name=name, message=message):
                body = self.events((name, {"type": name, "response": response}))
                with self.assertRaisesMessage(ChatError, message):
                    self.stream(StreamResponse(body))

    def test_a_failed_end_event_with_a_transient_error_is_retried(self):
        # D512: response.failed with a server error is what a 5xx would have been.
        cases = [
            ({"code": "server_error", "message": "oops"}, TransientChatError),
            ({"code": "rate_limit_exceeded", "message": "slow"}, TransientChatError),
            ({"code": "invalid_prompt", "message": "no"}, ChatError),
        ]
        for error, expected in cases:
            with self.subTest(error=error):
                response = {"status": "failed", "error": error}
                body = self.events(
                    ("response.failed", {"type": "response.failed", "response": response})
                )
                with self.assertRaises(expected):
                    self.stream(StreamResponse(body))

    def test_a_refusal_is_a_chat_error(self):
        refusal = {"type": "response.refusal.delta", "delta": "I can't help with that."}
        with self.assertRaisesMessage(ChatError, "declined"):
            self.stream(StreamResponse(self.events(("response.refusal.delta", refusal))))

    def test_an_error_event_is_translated_by_its_code(self):
        cases = [
            (
                {"type": "error", "code": "rate_limit_exceeded", "message": "slow down"},
                TransientChatError,
            ),
            ({"type": "error", "code": "server_error", "message": "oops"}, TransientChatError),
            (
                {"type": "error", "error": {"type": "server_error", "code": None, "message": "x"}},
                TransientChatError,
            ),
            ({"type": "error", "code": "invalid_prompt", "message": "no"}, ChatError),
        ]
        for data, error in cases:
            with self.subTest(data=data):
                with self.assertRaises(error):
                    self.stream(StreamResponse(self.events(("error", data))))


class GeminiStreamTests(StreamingMixin, SimpleTestCase):
    provider_class = GeminiProvider
    env = {"GEMINI_API_KEY": "g-test"}
    fixture_name = "gemini_answer.sse"
    model = "gemini/../x"

    def assert_usage(self, result):
        self.assertEqual(result.model, "gemini-2.5-flash-lite")
        # Thoughts are billed as output, as in complete().
        self.assertEqual((result.input_tokens, result.output_tokens), (110, 20))

    def assert_stream_request(self, call):
        self.assertEqual(
            call.args[0],
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini%2F..%2Fx:streamGenerateContent?alt=sse",
        )
        self.assertEqual(call.kwargs["headers"]["x-goog-api-key"], "g-test")
        self.assertEqual(call.kwargs["json"]["generationConfig"], {"maxOutputTokens": 100})

    def chunk(self, text, finish_reason=None):
        candidate = {"content": {"parts": [{"text": text}], "role": "model"}}
        if finish_reason:
            candidate["finishReason"] = finish_reason
        return {"candidates": [candidate]}

    def test_finish_reasons_are_checked_as_complete_does(self):
        for reason in ("MAX_TOKENS", "SAFETY", "RECITATION"):
            with self.subTest(reason=reason):
                body = sse(self.chunk("Half"), self.chunk(" more", reason), crlf=True)
                with self.assertRaises(ChatError):
                    self.stream(StreamResponse(body))

    def test_a_blocked_prompt_is_a_chat_error(self):
        body = sse({"promptFeedback": {"blockReason": "SAFETY"}})
        with self.assertRaisesMessage(ChatError, "SAFETY"):
            self.stream(StreamResponse(body))

    def test_usage_may_arrive_after_the_finish_reason(self):
        body = sse(
            self.chunk("Done.", "STOP"),
            {"usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 2}},
        )
        _, result = self.deltas_and_result(self.stream(StreamResponse(body)))
        self.assertEqual((result.text, result.input_tokens, result.output_tokens), ("Done.", 9, 2))

    def test_an_error_event_is_translated_like_its_status(self):
        cases = [
            ({"code": 503, "status": "UNAVAILABLE", "message": "overloaded"}, TransientChatError),
            ({"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota"}, TransientChatError),
            ({"status": "INTERNAL", "message": "x"}, TransientChatError),
            ({"code": 400, "status": "INVALID_ARGUMENT", "message": "bad"}, ChatError),
            ({"message": "who knows"}, ChatError),
        ]
        for error, expected in cases:
            with self.subTest(error=error):
                body = sse(self.chunk("Half"), {"error": error})
                with self.assertRaises(expected):
                    self.stream(StreamResponse(body))


# The handwritten recordings above are checked against the real APIs here:
# opt in with LIVE_PROVIDER_TESTS=1 and the vendor's key (D40), as for complete().
LIVE = os.environ.get("LIVE_PROVIDER_TESTS") == "1"


class LiveStreamTests(SimpleTestCase):
    """Real streamed calls through chat.stream. One tiny paid call each."""

    def stream(self, provider):
        from assistant.chat import stream

        with override_settings(CHAT_PROVIDER=provider, CHAT_MAX_OUTPUT_TOKENS=512):
            *deltas, result = stream("Reply with exactly one word.", "Say: ready")
        self.assertTrue(deltas)
        self.assertEqual("".join(deltas).strip(), result.text)
        self.assertEqual(result.provider, provider)
        self.assertGreater(result.input_tokens, 0)
        self.assertGreater(result.output_tokens, 0)

    @unittest.skipUnless(LIVE and os.environ.get("ANTHROPIC_API_KEY"), "live Claude test is opt-in")
    def test_claude_live(self):
        self.stream("claude")

    @unittest.skipUnless(LIVE and os.environ.get("OPENAI_API_KEY"), "live OpenAI test is opt-in")
    def test_openai_live(self):
        self.stream("openai")

    @unittest.skipUnless(
        LIVE and (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")),
        "live Gemini test is opt-in",
    )
    def test_gemini_live(self):
        self.stream("gemini")
