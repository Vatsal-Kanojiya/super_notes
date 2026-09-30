"""The multi-turn evaluation fixtures load, validate, and cover every kind."""

from django.test import SimpleTestCase

from retrieval.eval import loader
from retrieval.eval.loader import FixtureError, parse_conversations


class ShippedConversationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.conversations = loader.load_conversations()
        cls.note_keys = {note.key for note in loader.load().notes}

    def test_about_fifteen_cases(self):
        self.assertGreaterEqual(len(self.conversations), 14)
        self.assertLessEqual(len(self.conversations), 20)

    def test_every_kind_is_present(self):
        kinds = {c.kind for c in self.conversations}
        self.assertEqual(kinds, loader.CONVERSATION_KINDS)

    def test_every_relevant_key_exists(self):
        for conversation in self.conversations:
            for turn in conversation.turns:
                for key in turn.relevant or ():
                    self.assertIn(key, self.note_keys, conversation.id)

    def test_last_turn_is_labelled_and_no_answer_matches(self):
        for conversation in self.conversations:
            self.assertIsNotNone(conversation.last.relevant, conversation.id)
            self.assertEqual(conversation.kind == "no_answer", not conversation.has_answer)

    def test_there_are_no_answer_cases(self):
        self.assertGreaterEqual(sum(not c.has_answer for c in self.conversations), 2)

    def test_standalone_is_present_and_rewrites_context_dependent_turns(self):
        for conversation in self.conversations:
            self.assertTrue(conversation.standalone.strip())
            if conversation.kind in ("pronoun", "ellipsis", "refinement"):
                self.assertNotEqual(conversation.standalone, conversation.last.question)


class ConversationValidationTests(SimpleTestCase):
    KEYS = {"n01", "n02"}

    def case(self, **overrides):
        entry = {
            "id": "c01",
            "kind": "pronoun",
            "turns": [
                {"question": "A?", "relevant": ["n01"]},
                {"question": "B?", "relevant": ["n01"]},
            ],
            "standalone": "B about A?",
        }
        entry.update(overrides)
        return entry

    def assertFixtureError(self, fragment, *cases):
        with self.assertRaises(FixtureError) as ctx:
            parse_conversations(list(cases), self.KEYS)
        self.assertIn(fragment, str(ctx.exception))

    def test_a_valid_case_parses(self):
        (conversation,) = parse_conversations([self.case()], self.KEYS)
        self.assertEqual(conversation.relevant, ("n01",))
        self.assertEqual(conversation.last.question, "B?")
        self.assertTrue(conversation.has_answer)

    def test_earlier_turn_label_is_optional(self):
        turns = [{"question": "A?"}, {"question": "B?", "relevant": ["n02"]}]
        (conversation,) = parse_conversations([self.case(turns=turns)], self.KEYS)
        self.assertIsNone(conversation.turns[0].relevant)

    def test_duplicate_id(self):
        self.assertFixtureError("duplicate id", self.case(), self.case())

    def test_unknown_note_key(self):
        turns = [{"question": "A?"}, {"question": "B?", "relevant": ["n9"]}]
        self.assertFixtureError("unknown notes ['n9']", self.case(turns=turns))

    def test_unknown_key_in_an_earlier_turn(self):
        turns = [{"question": "A?", "relevant": ["n9"]}, {"question": "B?", "relevant": ["n01"]}]
        self.assertFixtureError("turn 1", self.case(turns=turns))

    def test_needs_two_turns(self):
        self.assertFixtureError("at least 2", self.case(turns=[{"question": "A?", "relevant": []}]))

    def test_last_turn_needs_relevant(self):
        turns = [{"question": "A?", "relevant": ["n01"]}, {"question": "B?"}]
        self.assertFixtureError("last turn needs relevant", self.case(turns=turns))

    def test_no_answer_exactly_when_last_relevant_is_empty(self):
        self.assertFixtureError("no_answer", self.case(kind="no_answer"))
        turns = [{"question": "A?", "relevant": ["n01"]}, {"question": "B?", "relevant": []}]
        self.assertFixtureError("no_answer", self.case(turns=turns))
        (ok,) = parse_conversations([self.case(turns=turns, kind="no_answer")], self.KEYS)
        self.assertFalse(ok.has_answer)

    def test_standalone_must_be_non_empty(self):
        self.assertFixtureError("standalone", self.case(standalone="  "))
        missing = {k: v for k, v in self.case().items() if k != "standalone"}
        self.assertFixtureError("missing ['standalone']", missing)

    def test_other_fields(self):
        self.assertFixtureError("kind must be", self.case(kind="trivia"))
        self.assertFixtureError("id must be", self.case(id=""))
        with self.assertRaises(FixtureError):
            parse_conversations({"c01": {}}, self.KEYS)
        turns = [{"question": " "}, {"question": "B?", "relevant": ["n01"]}]
        self.assertFixtureError("question must be", self.case(turns=turns))
        turns = [{"question": "A?"}, {"question": "B?", "relevant": ["n01", "n01"]}]
        self.assertFixtureError("twice", self.case(turns=turns))
        turns = [{"question": "A?"}, {"question": "B?", "relevant": "n01"}]
        self.assertFixtureError("list of note keys", self.case(turns=turns))
