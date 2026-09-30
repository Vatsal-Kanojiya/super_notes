"""The chat boundary, the registry and the fake provider."""

from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from assistant.chat import ChatError, ChatResult, TransientChatError, complete
from assistant.chat.providers.base import ChatProvider
from assistant.chat.providers.fake import FakeProvider, approximate_tokens, first_sentence
from assistant.chat.registry import PROVIDERS, get_provider
from assistant.citations import parse_citations
from assistant.prompt import Excerpt, build_messages


def excerpt(n, text):
    return Excerpt(n=n, note_id=10 * n, chunk_id=n, title=f"Note {n}", heading_path="", text=text)


class BoundaryTests(SimpleTestCase):
    def test_the_test_runner_forces_the_fake_provider(self):
        # DECISIONS D11: whatever .env says, the suite never spends money.
        self.assertEqual(settings.CHAT_PROVIDER, "fake")

    def test_complete_dispatches_to_the_configured_provider_with_its_model(self):
        result = ChatResult(text="ok", provider="claude", model="m")
        with (
            override_settings(CHAT_PROVIDER="claude", CHAT_MAX_OUTPUT_TOKENS=123),
            patch("assistant.chat.providers.claude.ClaudeProvider.complete") as mock_complete,
        ):
            mock_complete.return_value = result
            self.assertIs(complete("sys", "user"), result)
        mock_complete.assert_called_once_with("sys", "user", settings.CHAT_MODELS["claude"], 123)

    def test_the_fake_provider_needs_no_model_entry(self):
        self.assertNotIn("fake", settings.CHAT_MODELS)
        self.assertEqual(complete("sys", "no excerpts").provider, "fake")

    def test_transient_is_not_a_chat_error(self):
        # So `except ChatError` in the task cannot swallow a retry (D54).
        self.assertFalse(issubclass(TransientChatError, ChatError))


class RegistryTests(SimpleTestCase):
    def test_every_registered_provider_imports(self):
        for name in PROVIDERS:
            with self.subTest(name=name):
                provider = get_provider(name)
                self.assertEqual(provider.name, name)
                self.assertIsInstance(provider, ChatProvider)

    def test_unknown_name_is_a_chat_error(self):
        with self.assertRaisesMessage(ChatError, "Unknown chat provider: 'nope'"):
            get_provider("nope")

    def test_a_provider_that_cannot_be_imported_is_a_chat_error(self):
        with (
            patch.dict(PROVIDERS, {"broken": "assistant.chat.providers.missing.Provider"}),
            self.assertRaisesMessage(ChatError, "'broken' is not available"),
        ):
            get_provider("broken")


class FakeProviderTests(SimpleTestCase):
    def test_answers_with_the_first_sentence_of_the_top_two_excerpts_cited(self):
        excerpts = [
            excerpt(1, "The launch moved to Friday. Marketing is not ready."),
            excerpt(2, "Ask Priya about the venue! She booked it."),
            excerpt(3, "Unrelated."),
        ]
        system, user = build_messages("When is the launch?", excerpts)
        result = complete(system, user)
        self.assertEqual(
            result.text, "The launch moved to Friday. [1] Ask Priya about the venue! [2]"
        )
        self.assertEqual((result.provider, result.model), ("fake", "fake"))
        # End to end: the parsed citations lead back to the right notes.
        citations = parse_citations(result.text, excerpts)
        self.assertEqual([c["note_id"] for c in citations], [10, 20])

    def test_one_excerpt_is_cited_alone(self):
        _, user = build_messages("Q", [excerpt(1, "Only this.")])
        self.assertEqual(FakeProvider().complete("s", user).text, "Only this. [1]")

    def test_no_excerpts_gives_the_not_in_your_notes_answer(self):
        _, user = build_messages("Q", [])
        self.assertEqual(FakeProvider().complete("s", user).text, settings.ASK_NO_ANSWER_TEXT)

    def test_is_deterministic_with_approximate_token_counts(self):
        system, user = build_messages("Q", [excerpt(1, "Same every time.")])
        first = complete(system, user)
        self.assertEqual(first, complete(system, user))
        self.assertEqual(first.input_tokens, approximate_tokens(system) + approximate_tokens(user))
        self.assertEqual(first.output_tokens, approximate_tokens(first.text))

    def test_helpers(self):
        self.assertEqual(approximate_tokens(""), 1)
        self.assertEqual(approximate_tokens("abcde"), 2)
        self.assertEqual(first_sentence("No full stop\nat all"), "No full stop at all")
        self.assertEqual(len(first_sentence("x" * 500)), 200)
        self.assertEqual(first_sentence("Version 2.5 ships. Then more."), "Version 2.5 ships.")
