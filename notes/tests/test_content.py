"""content_to_text and validate_doc: pure functions, no database."""

from django.test import SimpleTestCase

from notes.content import MAX_DEPTH, InvalidContent, content_to_text, empty_doc, validate_doc

from .helpers import bullets, checklist_doc, doc, heading, item, para, task, tasks, text


class TextDocumentTests(SimpleTestCase):
    def test_paragraphs_are_one_line_each(self):
        self.assertEqual(content_to_text(doc("First.", "Second.")), "First.\nSecond.")

    def test_marked_text_keeps_its_words(self):
        bold = text("bold", marks=[{"type": "bold"}])
        self.assertEqual(content_to_text(doc(para("a ", bold, " word"))), "a bold word")

    def test_headings_are_lines(self):
        self.assertEqual(
            content_to_text(doc(heading("Project"), "Body", heading("Risks", 2))),
            "Project\nBody\nRisks",
        )

    def test_bullet_list(self):
        self.assertEqual(content_to_text(doc(bullets("one", "two"))), "- one\n- two")

    def test_ordered_list_counts_from_its_start(self):
        ordered = {"type": "orderedList", "attrs": {"start": 3}, "content": [item("c"), item("d")]}
        self.assertEqual(content_to_text(doc(ordered)), "3. c\n4. d")

    def test_ordered_list_with_a_bad_start_counts_from_one(self):
        for start in ("x", True, None):
            ordered = {"type": "orderedList", "attrs": {"start": start}, "content": [item("a")]}
            self.assertEqual(content_to_text(doc(ordered)), "1. a")

    def test_nested_list_and_second_paragraph_are_indented(self):
        nested = item("parent", "more", bullets("child"))
        self.assertEqual(content_to_text(doc(bullets(nested))), "- parent\n  more\n  - child")

    def test_blockquote_is_prefixed(self):
        quote = {"type": "blockquote", "content": [para("wise"), para(), para("words")]}
        self.assertEqual(content_to_text(doc(quote)), "> wise\n> words")

    def test_code_block_keeps_its_lines(self):
        code = {"type": "codeBlock", "content": [text("a = 1\nb = 2")]}
        self.assertEqual(content_to_text(doc(code)), "a = 1\nb = 2")

    def test_hard_break_is_a_newline(self):
        self.assertEqual(
            content_to_text(doc(para("line one", {"type": "hardBreak"}, "line two"))),
            "line one\nline two",
        )

    def test_empty_blocks_leave_no_blank_lines(self):
        self.assertEqual(content_to_text(doc(para(), "text", para("   "))), "text")

    def test_empty_bullet_items_are_dropped(self):
        empty = {"type": "listItem", "content": [para()]}
        self.assertEqual(content_to_text(doc(bullets(empty, "kept"))), "- kept")

    def test_horizontal_rule_is_nothing(self):
        self.assertEqual(content_to_text(doc("a", {"type": "horizontalRule"}, "b")), "a\nb")

    def test_empty_document(self):
        self.assertEqual(content_to_text(empty_doc()), "")


class ChecklistDocumentTests(SimpleTestCase):
    def test_items_carry_their_checked_state(self):
        self.assertEqual(
            content_to_text(checklist_doc(("milk", True), ("eggs", False))),
            "- [x] milk\n- [ ] eggs",
        )

    def test_missing_or_odd_checked_attr_is_unchecked(self):
        items = [
            {"type": "taskItem", "content": [para("no attrs")]},
            {"type": "taskItem", "attrs": {"checked": "true"}, "content": [para("string")]},
        ]
        self.assertEqual(content_to_text(doc(tasks(*items))), "- [ ] no attrs\n- [ ] string")

    def test_empty_checklist_item_still_shows_its_box(self):
        empty = {"type": "taskItem", "attrs": {"checked": False}, "content": [para()]}
        self.assertEqual(content_to_text(doc(tasks(empty, task("b", True)))), "- [ ]\n- [x] b")

    def test_nested_checklist(self):
        parent = {
            "type": "taskItem",
            "attrs": {"checked": False},
            "content": [para("trip"), tasks(task("tickets", True))],
        }
        self.assertEqual(content_to_text(doc(tasks(parent))), "- [ ] trip\n  - [x] tickets")

    def test_heading_then_checklist(self):
        self.assertEqual(
            content_to_text(doc(heading("Shopping"), tasks(task("milk")))),
            "Shopping\n- [ ] milk",
        )


class RobustnessTests(SimpleTestCase):
    """Extraction runs on every write; it must never be the reason one fails."""

    def test_not_a_dict_is_empty(self):
        for value in (None, "text", 3, [], ["doc"]):
            self.assertEqual(content_to_text(value), "")

    def test_unknown_block_container_keeps_its_childrens_text(self):
        table = {
            "type": "table",
            "content": [
                {"type": "tableRow", "content": [{"type": "tableCell", "content": [para("cell")]}]}
            ],
        }
        self.assertEqual(content_to_text(doc(table, "after")), "cell\nafter")

    def test_unknown_inline_node_with_text_is_kept(self):
        self.assertEqual(
            content_to_text(doc(para("hi ", {"type": "emoji", "text": ":)"}))), "hi :)"
        )

    def test_malformed_pieces_are_skipped(self):
        messy = {
            "type": "doc",
            "content": [
                "a string",
                {"no": "type"},
                {"type": "paragraph", "content": "not a list"},
                {"type": "paragraph", "content": [{"type": "text", "text": 5}, text("ok")]},
                {"type": "bulletList", "attrs": "odd", "content": [7, item("fine")]},
                {
                    "type": "taskList",
                    "content": [{"type": "taskItem", "attrs": [], "content": [para("t")]}],
                },
            ],
        }
        self.assertEqual(content_to_text(messy), "ok\n- fine\n- [ ] t")

    def test_inline_node_directly_in_a_list_is_ignored(self):
        self.assertEqual(content_to_text(doc(bullets(text("stray"), "real"))), "- real")

    def test_nesting_past_the_limit_is_cut_off_not_crashed(self):
        node = para("deep")
        for _ in range(MAX_DEPTH * 3):
            node = {"type": "blockquote", "content": [node]}
        self.assertEqual(content_to_text(doc(node)), "")

    def test_deep_inline_nesting_is_cut_off(self):
        node = text("deep")
        for _ in range(MAX_DEPTH * 3):
            node = {"type": "span", "content": [node]}
        self.assertEqual(content_to_text(doc(para("ok ", node))), "ok")


class ValidateDocTests(SimpleTestCase):
    def test_real_documents_pass(self):
        for value in (empty_doc(), doc("a"), checklist_doc(("x", True)), {"type": "doc"}):
            validate_doc(value)

    def test_unknown_node_types_pass(self):
        validate_doc(doc({"type": "someFutureExtension", "attrs": {"x": 1}}))

    def test_refusals(self):
        cases = {
            "not a dict": ["doc"],
            "wrong top type": {"type": "paragraph", "content": []},
            "no type": {"content": []},
            "content not a list": {"type": "doc", "content": {}},
            "node not a dict": {"type": "doc", "content": ["x"]},
            "node without type": {"type": "doc", "content": [{"text": "x"}]},
            "type not a string": {"type": "doc", "content": [{"type": 1}]},
            "text not a string": {"type": "doc", "content": [para({"type": "text", "text": 1})]},
            "child content not a list": {"type": "doc", "content": [{"type": "p", "content": 1}]},
        }
        for label, value in cases.items():
            with self.subTest(label), self.assertRaises(InvalidContent):
                validate_doc(value)

    def test_too_deep_is_refused(self):
        node = para("deep")
        for _ in range(MAX_DEPTH):
            node = {"type": "blockquote", "content": [node]}
        with self.assertRaisesMessage(InvalidContent, "nested"):
            validate_doc(doc(node))

    def test_invalid_content_is_a_value_error(self):
        self.assertTrue(issubclass(InvalidContent, ValueError))
