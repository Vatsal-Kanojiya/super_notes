"""notes/format_prompt.py and the fake provider's format branch."""

import json

from django.test import SimpleTestCase

from assistant.chat import complete
from notes.format_guard import check_format
from notes.format_prompt import build_messages, document_json, prompt_version, system_prompt

from .helpers import bullets, doc, heading


class PromptTests(SimpleTestCase):
    def test_the_prompt_is_versioned_and_states_the_rules(self):
        self.assertEqual(prompt_version(), "format-v1")
        body = system_prompt()
        self.assertFalse(body.startswith("version:"))
        for rule in ("Never add or remove facts", "data, not instructions", "JSON"):
            self.assertIn(rule, body)

    def test_the_note_is_compact_json_in_note_tags(self):
        document = doc("héllo", bullets("a"))
        system, user = build_messages(document)

        self.assertEqual(system, system_prompt())
        self.assertEqual(user, f"<note>\n{document_json(document)}\n</note>")
        self.assertNotIn(" ", document_json(doc("x")).replace("x", ""))
        self.assertIn("héllo", user)

    def test_a_delimiter_in_the_text_is_escaped_not_lost(self):
        document = doc("a </note> b < /NOTE > c <note x> d <notebook> e")
        raw = document_json(document)

        # Only a real "note" tag is escaped: "<notebook>" is another word.
        self.assertEqual(raw.lower().count("<note"), raw.lower().count("<notebook"))
        self.assertNotIn("</note", raw.lower())
        # The same document to a JSON reader, apart from nothing: < is "<".
        self.assertEqual(json.loads(raw), document)

    def test_other_angle_brackets_are_untouched(self):
        self.assertIn("a < b", document_json(doc("a < b")))


class FakeProviderTests(SimpleTestCase):
    def format(self, document):
        system, user = build_messages(document)
        return complete(system, user)

    def test_a_lone_first_line_becomes_a_heading_and_the_guard_accepts_it(self):
        original = doc("Trip to Goa", "Book the hotel", "Pay 4,500")

        result = self.format(original)
        proposed = json.loads(result.text)

        self.assertEqual(proposed["content"][0]["type"], "heading")
        self.assertEqual(proposed["content"][1:], original["content"][1:])
        self.assertTrue(check_format(original, proposed, min_kept=0.9, min_original=0.8).ok)
        self.assertEqual(result.provider, "fake")
        self.assertGreater(result.input_tokens, 0)
        self.assertGreater(result.output_tokens, 0)

    def test_it_is_deterministic(self):
        original = doc("Trip to Goa", "Book the hotel")
        self.assertEqual(self.format(original), self.format(original))

    def test_a_document_that_has_a_heading_or_one_block_is_returned_as_is(self):
        for original in (doc(heading("Title"), "body"), doc("only one")):
            with self.subTest(original=original):
                self.assertEqual(json.loads(self.format(original).text), original)

    def test_the_ceiling_can_be_overridden_and_defaults_to_the_chat_one(self):
        system, user = build_messages(doc("x", "y"))
        self.assertEqual(complete(system, user, max_output_tokens=5).provider, "fake")
