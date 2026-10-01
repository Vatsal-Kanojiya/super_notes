"""Reading an image's text: the boundary and each provider's read_image (DECISIONS D343).

requests.post is mocked, as in test_providers.py: no network in tests.
"""

import base64
import os
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from assistant import chat
from assistant.chat import ChatError, ChatResult, ImageTextNotSupported, TransientChatError
from assistant.chat.providers.claude import ClaudeProvider
from assistant.chat.providers.fake import FAKE_IMAGE_TEXT, FakeProvider
from assistant.chat.providers.gemini import GeminiProvider
from assistant.chat.providers.openai import OpenAIProvider
from assistant.prompt import load_prompt

from .test_providers import POST, claude_body, http

IMAGE = b"\x89PNG\r\n\x1a\nfake image bytes"
ENCODED = base64.standard_b64encode(IMAGE).decode("ascii")


def read(provider, env):
    with patch.dict(os.environ, env):
        return provider.read_image("system", "Transcribe.", IMAGE, "image/png", "model-x", 100)


class ClaudeReadImageTests(SimpleTestCase):
    env = {"ANTHROPIC_API_KEY": "sk-ant-test"}

    def test_the_image_goes_as_a_base64_block_before_the_text(self):
        with patch(POST, return_value=http(200, claude_body())) as post:
            result = read(ClaudeProvider(), self.env)

        body = post.call_args.kwargs["json"]
        self.assertEqual(post.call_args.args[0], "https://api.anthropic.com/v1/messages")
        self.assertEqual(body["system"], "system")
        self.assertEqual(body["max_tokens"], 100)
        self.assertEqual(
            body["messages"],
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": ENCODED,
                            },
                        },
                        {"type": "text", "text": "Transcribe."},
                    ],
                }
            ],
        )
        self.assertEqual((result.text, result.provider), ("Friday [1].", "claude"))
        self.assertEqual((result.input_tokens, result.output_tokens), (120, 8))

    def test_errors_translate_as_for_complete(self):
        with patch(POST, return_value=http(429, {})), self.assertRaises(TransientChatError):
            read(ClaudeProvider(), self.env)
        with patch(POST, return_value=http(400, {})), self.assertRaises(ChatError):
            read(ClaudeProvider(), self.env)
        refusal = claude_body(stop_reason="refusal", content=[])
        with patch(POST, return_value=http(200, refusal)), self.assertRaises(ChatError):
            read(ClaudeProvider(), self.env)


def openai_body(text="Hello"):
    return {
        "status": "completed",
        "model": "gpt-5-mini-2025",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 900, "output_tokens": 3},
    }


class OpenAIReadImageTests(SimpleTestCase):
    env = {"OPENAI_API_KEY": "sk-test"}

    def test_the_image_goes_as_an_input_image_data_url(self):
        with patch(POST, return_value=http(200, openai_body())) as post:
            result = read(OpenAIProvider(), self.env)

        body = post.call_args.kwargs["json"]
        self.assertEqual(post.call_args.args[0], "https://api.openai.com/v1/responses")
        self.assertEqual(body["instructions"], "system")
        self.assertFalse(body["store"])
        self.assertEqual(
            body["input"],
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": f"data:image/png;base64,{ENCODED}"},
                        {"type": "input_text", "text": "Transcribe."},
                    ],
                }
            ],
        )
        self.assertEqual((result.text, result.model), ("Hello", "gpt-5-mini-2025"))
        self.assertEqual((result.input_tokens, result.output_tokens), (900, 3))

    def test_an_incomplete_answer_is_a_chat_error(self):
        body = {**openai_body(), "status": "incomplete", "incomplete_details": {"reason": "x"}}
        with patch(POST, return_value=http(200, body)), self.assertRaises(ChatError):
            read(OpenAIProvider(), self.env)
        with patch(POST, return_value=http(503, {})), self.assertRaises(TransientChatError):
            read(OpenAIProvider(), self.env)


def gemini_body(text="Hello", finish="STOP"):
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}],
        "usageMetadata": {"promptTokenCount": 300, "candidatesTokenCount": 2},
        "modelVersion": "gemini-2.5-flash-lite",
    }


class GeminiReadImageTests(SimpleTestCase):
    env = {"GEMINI_API_KEY": "g-test"}

    def test_the_image_goes_as_inline_data_before_the_text(self):
        with patch(POST, return_value=http(200, gemini_body())) as post:
            result = read(GeminiProvider(), self.env)

        body = post.call_args.kwargs["json"]
        self.assertTrue(post.call_args.args[0].endswith("/models/model-x:generateContent"))
        self.assertEqual(body["systemInstruction"], {"parts": [{"text": "system"}]})
        self.assertEqual(
            body["contents"],
            [
                {
                    "role": "user",
                    "parts": [
                        {"inline_data": {"mime_type": "image/png", "data": ENCODED}},
                        {"text": "Transcribe."},
                    ],
                }
            ],
        )
        self.assertEqual(body["generationConfig"], {"maxOutputTokens": 100})
        self.assertEqual((result.text, result.input_tokens), ("Hello", 300))

    def test_a_blocked_answer_is_a_chat_error(self):
        with (
            patch(POST, return_value=http(200, gemini_body(finish="SAFETY"))),
            self.assertRaises(ChatError),
        ):
            read(GeminiProvider(), self.env)


class NoImages:
    name = "plain"

    def complete(self, system, user, model, max_output_tokens):  # pragma: no cover
        raise AssertionError("not called")


class BoundaryTests(SimpleTestCase):
    def test_the_fake_reads_fixed_text(self):
        result = chat.extract_image_text(IMAGE, "image/png")
        self.assertEqual((result.text, result.provider), (FAKE_IMAGE_TEXT, "fake"))
        self.assertGreater(result.input_tokens, 0)

    def test_the_prompt_file_is_sent_with_the_configured_model_and_ceiling(self):
        reply = ChatResult(text="  Hi  ", provider="fake", model="fake")
        with (
            override_settings(ATTACHMENT_IMAGE_TEXT_MAX_OUTPUT_TOKENS=321),
            patch.object(FakeProvider, "read_image", return_value=reply) as read_image,
        ):
            result = chat.extract_image_text(IMAGE, "image/webp")

        system, user, image, mime_type, _, ceiling = read_image.call_args.args
        self.assertEqual(system, load_prompt("image_text")[1])
        self.assertEqual(user, chat.IMAGE_TEXT_INSTRUCTION)
        self.assertEqual((image, mime_type, ceiling), (IMAGE, "image/webp", 321))
        self.assertEqual(result.text, "Hi")

    def test_no_text_comes_back_empty(self):
        reply = ChatResult(text=" [no text]\n", provider="fake", model="fake")
        with patch.object(FakeProvider, "read_image", return_value=reply):
            self.assertEqual(chat.extract_image_text(IMAGE, "image/png").text, "")

    def test_a_provider_without_read_image_is_refused(self):
        with (
            patch("assistant.chat.get_provider", return_value=NoImages()),
            self.assertRaises(ImageTextNotSupported) as caught,
        ):
            chat.extract_image_text(IMAGE, "image/png")
        self.assertIsInstance(caught.exception, ChatError)

    def test_every_real_provider_can_read_images(self):
        for provider in (ClaudeProvider, OpenAIProvider, GeminiProvider, FakeProvider):
            with self.subTest(provider=provider.name):
                self.assertTrue(callable(getattr(provider, "read_image", None)))

    def test_the_prompt_is_versioned(self):
        self.assertEqual(load_prompt("image_text")[0], "image-text-v1")
        self.assertIn(chat.NO_TEXT, load_prompt("image_text")[1])
