"""The evaluation fixtures load, validate, and keep the traits they exist for.

The eval is only as good as its data. These tests pin the properties the
questions depend on (near-duplicate pairs, checklists, long multi-chunk
notes) by key, so editing a fixture cannot quietly remove what a question
was written to test.
"""

import copy
import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from retrieval.eval import loader
from retrieval.eval.loader import FixtureError, parse_notes, parse_questions, validate_doc


def plain_text(node):
    if node.get("type") == "text":
        return node["text"]
    return " ".join(plain_text(child) for child in node.get("content", []))


def doc(*blocks):
    return {"type": "doc", "content": list(blocks)}


def para(text):
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


class ShippedFixtureTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.data = loader.load()
        cls.by_key = {note.key: note for note in cls.data.notes}

    def test_counts_are_about_thirty_each(self):
        self.assertEqual(len(self.data.notes), 30)
        self.assertGreaterEqual(len(self.data.questions), 28)

    def test_every_relevant_key_names_a_note(self):
        for question in self.data.questions:
            for key in question.relevant:
                self.assertIn(key, self.by_key, question.id)

    def test_every_kind_is_represented(self):
        kinds = {question.kind for question in self.data.questions}
        self.assertEqual(kinds, loader.QUESTION_KINDS)

    def test_there_are_no_answer_questions_for_the_relevance_floor(self):
        no_answer = [q for q in self.data.questions if not q.has_answer]
        self.assertGreaterEqual(len(no_answer), 2)
        self.assertTrue(all(q.kind == "no_answer" for q in no_answer))

    def test_multi_note_questions_name_two_notes(self):
        multi = [q for q in self.data.questions if q.kind == "multi_note"]
        self.assertGreaterEqual(len(multi), 2)
        self.assertTrue(all(len(q.relevant) >= 2 for q in multi))

    def test_near_duplicate_pairs_are_still_near_duplicates(self):
        # Goa trips, Atlas syncs, weekly groceries: same shape, different facts.
        for a, b, shared in [
            ("n01", "n02", "Goa trip plan"),
            ("n04", "n05", "Atlas weekly sync"),
            ("n06", "n07", "Groceries"),
        ]:
            self.assertIn(shared, self.by_key[a].title)
            self.assertIn(shared, self.by_key[b].title)
        # And the questions that must pick the right one of each pair.
        near = {q.id: q.relevant for q in self.data.questions if q.kind == "near_duplicate"}
        self.assertEqual(near["q17"], ("n02",))
        self.assertEqual(near["q18"], ("n01",))

    def test_checklists_are_checklists_with_open_items(self):
        for key in ("n06", "n07", "n08", "n24", "n28", "n29"):
            note = self.by_key[key]
            self.assertEqual(note.type, "checklist", key)
            flat = json.dumps(note.content)
            self.assertIn('"checked": false', flat, key)  # "what's left" needs something left
            self.assertIn('"checked": true', flat, key)

    def test_long_notes_span_several_chunks(self):
        # Chunks target ~1600-2000 characters; these must need more than one.
        for key in ("n03", "n16", "n20", "n21"):
            self.assertGreater(len(plain_text(self.by_key[key].content)), 2000, key)

    def test_short_notes_exist_too(self):
        self.assertLess(len(plain_text(self.by_key["n23"].content)), 200)

    def test_note_lookup(self):
        self.assertEqual(self.data.note("n09").title, "Dal makhani (Mummy's recipe)")
        with self.assertRaises(KeyError):
            self.data.note("n99")


class ValidationTests(SimpleTestCase):
    def note(self, **overrides):
        entry = {"key": "n01", "type": "text", "title": "T", "content": doc(para("x"))}
        entry.update(overrides)
        return entry

    def question(self, **overrides):
        entry = {"id": "q01", "question": "Q?", "relevant": ["n01"], "kind": "keyword"}
        entry.update(overrides)
        return entry

    def assertFixtureError(self, fragment, func, *args):
        with self.assertRaises(FixtureError) as ctx:
            func(*args)
        self.assertIn(fragment, str(ctx.exception))

    def test_a_valid_note_and_question_parse(self):
        (note,) = parse_notes([self.note()])
        (question,) = parse_questions([self.question()], {"n01"})
        self.assertEqual(note.key, "n01")
        self.assertEqual(question.relevant, ("n01",))
        self.assertTrue(question.has_answer)

    def test_duplicate_note_key(self):
        self.assertFixtureError("duplicate key", parse_notes, [self.note(), self.note()])

    def test_duplicate_question_id(self):
        self.assertFixtureError(
            "duplicate id", parse_questions, [self.question(), self.question()], {"n01"}
        )

    def test_unknown_note_key_in_a_question(self):
        self.assertFixtureError(
            "unknown notes ['n4']", parse_questions, [self.question(relevant=["n4"])], {"n01"}
        )

    def test_top_level_must_be_a_doc(self):
        for content in (
            {"type": "paragraph", "content": []},
            [para("x")],
            {"type": "doc"},
            {"type": "doc", "content": []},
        ):
            with self.subTest(content=content):
                self.assertRaises(FixtureError, parse_notes, [self.note(content=content)])

    def test_malformed_nodes_are_refused(self):
        bad = [
            ({"type": "image"}, "unknown node type"),
            ("text", "must be an object"),
            ({"type": "text", "text": ""}, "non-empty text"),
            (
                {"type": "paragraph", "content": [{"type": "text", "text": "a", "content": []}]},
                "has no content",
            ),
            (
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "a", "marks": [{"type": "link"}]}],
                },
                "unknown mark",
            ),
            ({"type": "heading", "attrs": {"level": 9}}, "attrs.level"),
            ({"type": "heading", "attrs": {"level": True}}, "attrs.level"),
            ({"type": "taskList", "content": [{"type": "taskItem", "attrs": {}}]}, "attrs.checked"),
            ({"type": "paragraph", "attrs": []}, "attrs must be an object"),
            ({"type": "paragraph", "content": "x"}, "content must be a list"),
            ({"type": "doc", "content": [para("x")]}, "unknown node type 'doc'"),
        ]
        for node, fragment in bad:
            with self.subTest(node=node):
                self.assertFixtureError(fragment, validate_doc, doc(node))

    def test_errors_name_the_path(self):
        node = {"type": "bulletList", "content": [{"type": "listItem", "content": [{}]}]}
        self.assertFixtureError("bulletList > listItem", validate_doc, doc(node), "n05")

    def test_note_fields(self):
        self.assertFixtureError(
            "missing ['title']",
            parse_notes,
            [{"key": "n", "type": "text", "content": doc(para("x"))}],
        )
        self.assertFixtureError("type must be", parse_notes, [self.note(type="drawing")])
        self.assertFixtureError("title", parse_notes, [self.note(title="  ")])
        self.assertFixtureError("key must be", parse_notes, [self.note(key="")])
        self.assertFixtureError("expected an object", parse_notes, ["n01"])
        self.assertFixtureError("expected a list", parse_notes, {"n01": {}})

    def test_a_checklist_needs_a_task_item(self):
        self.assertFixtureError("taskItem", parse_notes, [self.note(type="checklist")])

    def test_question_fields(self):
        cases = [
            (self.question(id=""), "id must be"),
            (self.question(question=""), "question must be"),
            (self.question(kind="trivia"), "kind must be"),
            (self.question(relevant="n01"), "list of note keys"),
            (self.question(relevant=["n01", "n01"]), "twice"),
            (self.question(relevant=[]), "no_answer"),
            (self.question(kind="no_answer"), "no_answer"),
            (self.question(kind="multi_note"), "two or more"),
        ]
        for entry, fragment in cases:
            with self.subTest(entry=entry):
                self.assertFixtureError(fragment, parse_questions, [entry], {"n01"})
        self.assertFixtureError("expected a list", parse_questions, {}, {"n01"})

    def test_a_no_answer_question_parses(self):
        (question,) = parse_questions([self.question(relevant=[], kind="no_answer")], set())
        self.assertFalse(question.has_answer)

    def test_load_reads_files_and_reports_bad_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes, questions = Path(tmp, "notes.json"), Path(tmp, "questions.json")
            notes.write_text(json.dumps([self.note()]))
            questions.write_text(json.dumps([self.question()]))
            self.assertEqual(len(loader.load(notes, questions).questions), 1)

            questions.write_text("[{")
            self.assertFixtureError("questions.json: not valid JSON", loader.load, notes, questions)

    def test_the_shipped_notes_are_not_mutated_by_validation(self):
        entry = self.note(content=doc(para("x")))
        before = copy.deepcopy(entry)
        parse_notes([entry])
        self.assertEqual(entry, before)
