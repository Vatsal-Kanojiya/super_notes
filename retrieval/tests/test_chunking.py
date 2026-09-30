"""The chunker (retrieval/chunking.py, plan §6.1).

Pure functions, so SimpleTestCase throughout: no database. Small chunk
sizes are passed explicitly wherever a test is about packing, so the
tests do not move when the CHUNK_* defaults are tuned.
"""

import hashlib

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from retrieval.chunking import _SENTENCES, MAX_DEPTH, chunk_note


def text(value):
    return {"type": "text", "text": value}


def para(value):
    return {"type": "paragraph", "content": [text(value)]}


def heading(level, value):
    return {"type": "heading", "attrs": {"level": level}, "content": [text(value)]}


def bullets(*items):
    return {
        "type": "bulletList",
        "content": [{"type": "listItem", "content": [para(item)]} for item in items],
    }


def tasks(*items):
    """items: (text, checked) pairs."""
    return {
        "type": "taskList",
        "content": [
            {"type": "taskItem", "attrs": {"checked": checked}, "content": [para(value)]}
            for value, checked in items
        ],
    }


def doc(*nodes):
    return {"type": "doc", "content": list(nodes)}


SMALL = {"target_chars": 120, "max_chars": 160, "overlap_chars": 40}


class HeadingPathTests(SimpleTestCase):
    def test_a_lower_heading_nests_and_a_new_top_heading_resets(self):
        chunks = chunk_note(
            "Plan",
            doc(
                heading(1, "Project"),
                para("Overview."),
                heading(2, "Risks"),
                para("Budget overrun."),
                heading(3, "Mitigation"),
                para("Weekly review."),
                heading(2, "Timeline"),
                para("Ship in May."),
                heading(1, "Personal"),
                para("Dentist."),
            ),
        )

        self.assertEqual(
            [(c.heading_path, c.text) for c in chunks],
            [
                ("Project", "Overview."),
                ("Project > Risks", "Budget overrun."),
                ("Project > Risks > Mitigation", "Weekly review."),
                ("Project > Timeline", "Ship in May."),
                ("Personal", "Dentist."),
            ],
        )

    def test_text_before_any_heading_has_an_empty_path(self):
        chunks = chunk_note("Plan", doc(para("Preamble."), heading(1, "A"), para("Body.")))

        self.assertEqual([c.heading_path for c in chunks], ["", "A"])

    def test_a_skipped_level_still_nests_under_the_last_higher_heading(self):
        chunks = chunk_note(
            "", doc(heading(1, "A"), heading(3, "C"), para("x"), heading(2, "B"), para("y"))
        )

        self.assertEqual([c.heading_path for c in chunks], ["A > C", "A > B"])

    def test_a_chunk_never_spans_two_sections(self):
        # Both sections would fit in one chunk by size; the heading splits them.
        chunks = chunk_note("", doc(heading(1, "A"), para("one"), heading(1, "B"), para("two")))

        self.assertEqual([c.text for c in chunks], ["one", "two"])

    def test_an_empty_section_keeps_its_heading_words(self):
        chunks = chunk_note("", doc(heading(1, "Ideas"), heading(1, "Done"), para("x")))

        self.assertEqual(
            [(c.heading_path, c.text) for c in chunks], [("Ideas", "Ideas"), ("Done", "x")]
        )

    def test_a_heading_followed_by_its_own_subheading_is_not_empty(self):
        chunks = chunk_note("", doc(heading(1, "Project"), heading(2, "Risks"), para("x")))

        self.assertEqual([(c.heading_path, c.text) for c in chunks], [("Project > Risks", "x")])

    def test_a_trailing_empty_heading_is_kept(self):
        chunks = chunk_note("", doc(para("x"), heading(2, "Later")))

        self.assertEqual([c.text for c in chunks], ["x", "Later"])

    def test_an_empty_heading_changes_nothing(self):
        chunks = chunk_note(
            "", doc(heading(1, "A"), {"type": "heading", "attrs": {"level": 1}}, para("x"))
        )

        self.assertEqual([c.heading_path for c in chunks], ["A"])

    def test_heading_whitespace_is_collapsed(self):
        chunks = chunk_note("", doc(heading(1, "  Big \n  plan "), para("x")))

        self.assertEqual(chunks[0].heading_path, "Big plan")


class RenderingTests(SimpleTestCase):
    def test_checklist_items_render_their_state(self):
        chunks = chunk_note("", doc(tasks(("Book venue", True), ("Call Sam", False))))

        self.assertEqual(chunks[0].text, "[x] Book venue\n[ ] Call Sam")

    def test_only_a_real_true_checks_an_item(self):
        # A client that sends "true" as a string gets an unchecked box, not
        # a crash and not a false tick.
        chunks = chunk_note("", doc(tasks(("a", "true"), ("b", 1))))

        self.assertEqual(chunks[0].text, "[ ] a\n[ ] b")

    def test_lists_render_markers_and_nested_items_are_indented(self):
        nested = {
            "type": "bulletList",
            "content": [
                {"type": "listItem", "content": [para("parent"), bullets("child one", "child two")]}
            ],
        }
        ordered = {
            "type": "orderedList",
            "attrs": {"start": 3},
            "content": [{"type": "listItem", "content": [para(v)]} for v in ("c", "d")],
        }

        chunks = chunk_note("", doc(nested, ordered))

        self.assertEqual(chunks[0].text, "- parent\n  - child one\n  - child two\n3. c\n4. d")

    def test_blockquote_code_and_hard_break(self):
        chunks = chunk_note(
            "",
            doc(
                {"type": "blockquote", "content": [para("quoted")]},
                {"type": "codeBlock", "content": [text("def f():\n    return 1\n")]},
                {
                    "type": "paragraph",
                    "content": [text("line one"), {"type": "hardBreak"}, text("two")],
                },
            ),
        )

        self.assertEqual(chunks[0].text, "> quoted\ndef f():\n    return 1\nline one\ntwo")

    def test_prose_whitespace_is_collapsed(self):
        chunks = chunk_note("", doc(para("  lots   of\tspace  ")))

        self.assertEqual(chunks[0].text, "lots of space")

    def test_unknown_containers_are_read_for_their_text(self):
        table = {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [
                        {"type": "tableCell", "content": [para("Flight")]},
                        {"type": "tableCell", "content": [para("AI 101")]},
                    ],
                }
            ],
        }
        chunks = chunk_note("", doc(table, {"type": "callout", "content": [text("Note this")]}))

        self.assertEqual(chunks[0].text, "Flight\nAI 101\nNote this")

    def test_horizontal_rules_and_images_add_nothing(self):
        chunks = chunk_note(
            "", doc(para("a"), {"type": "horizontalRule"}, {"type": "image", "attrs": {"src": "x"}})
        )

        self.assertEqual(chunks[0].text, "a")


class PackingTests(SimpleTestCase):
    def test_small_blocks_share_a_chunk(self):
        chunks = chunk_note("", doc(para("one"), para("two"), para("three")))

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].text, "one\ntwo\nthree")

    def test_no_chunk_is_longer_than_the_max(self):
        body = [para(f"Paragraph {i} " + "word " * (i * 7 % 40)) for i in range(60)]
        body += [bullets(*[f"item {i} " + "x" * (i * 13 % 150) for i in range(30)])]

        chunks = chunk_note("", doc(*body), **SMALL)

        self.assertGreater(len(chunks), 10)
        self.assertTrue(all(len(c.text) <= SMALL["max_chars"] for c in chunks))

    def test_list_items_are_never_split(self):
        items = [f"Item {i}: " + "detail " * (i % 9 + 3) for i in range(25)]

        chunks = chunk_note("", doc(bullets(*items)), **SMALL)

        self.assertGreater(len(chunks), 3)
        whole_items = {f"- {item.strip()}" for item in items}
        for chunk in chunks:
            for line in chunk.text.split("\n"):
                self.assertIn(line, whole_items)
        # And every item made it into some chunk.
        seen = {line for c in chunks for line in c.text.split("\n")}
        self.assertEqual(seen, whole_items)

    def test_checklist_items_are_never_split(self):
        items = [(f"Task {i} " + "step " * (i % 7 + 2), i % 2 == 0) for i in range(25)]

        chunks = chunk_note("", doc(tasks(*items)), **SMALL)

        whole = {f"[{'x' if done else ' '}] {value.strip()}" for value, done in items}
        for chunk in chunks:
            for line in chunk.text.split("\n"):
                self.assertIn(line, whole)

    def test_consecutive_chunks_overlap_by_whole_items(self):
        items = [f"Item number {i:02d}" for i in range(20)]

        chunks = chunk_note("", doc(bullets(*items)), **SMALL)

        for previous, current in zip(chunks, chunks[1:], strict=False):
            first_line = current.text.split("\n")[0]
            # The next chunk opens with a whole item the previous one ended with.
            self.assertIn(first_line, previous.text.split("\n"))

    def test_overlap_repeats_at_most_the_overlap_budget(self):
        items = [f"Item number {i:02d}" for i in range(20)]

        chunks = chunk_note("", doc(bullets(*items)), **SMALL)

        for previous, current in zip(chunks, chunks[1:], strict=False):
            prev_lines = previous.text.split("\n")
            shared = [line for line in current.text.split("\n") if line in prev_lines]
            self.assertGreater(len(shared), 0)
            self.assertLessEqual(len("\n".join(shared)), SMALL["overlap_chars"])

    def test_zero_overlap_repeats_nothing(self):
        items = [f"Item number {i:02d}" for i in range(20)]

        chunks = chunk_note(
            "", doc(bullets(*items)), target_chars=120, max_chars=160, overlap_chars=0
        )

        lines = [line for c in chunks for line in c.text.split("\n")]
        self.assertEqual(len(lines), len(set(lines)))

    def test_a_long_paragraph_lends_its_last_sentences_as_overlap(self):
        first = "Alpha one is here. Alpha two is here. Alpha three is here. Alpha four ends it."
        second = "Beta " * 20

        chunks = chunk_note(
            "",
            doc(para(first), para(second.strip())),
            target_chars=120,
            max_chars=160,
            overlap_chars=40,
        )

        self.assertEqual(chunks[0].text, first)
        # Whole sentences only, never the whole paragraph again.
        # The last two sentences are exactly the 40-character budget.
        self.assertTrue(chunks[1].text.startswith("Alpha three is here. Alpha four ends it.\nBeta"))

    def test_an_item_too_long_to_carry_is_not_carried(self):
        long_item = "- " + "y" * 100

        chunks = chunk_note(
            "",
            doc(bullets("y" * 100, "z" * 100)),
            target_chars=120,
            max_chars=160,
            overlap_chars=40,
        )

        self.assertEqual([c.text for c in chunks], [long_item, "- " + "z" * 100])

    def test_a_small_run_is_grown_rather_than_repeated(self):
        # "tiny" alone fits the overlap budget, so closing a chunk on it
        # would only repeat it; the chunk grows past the target instead.
        chunks = chunk_note(
            "",
            doc(para("tiny"), para("w" * 110)),
            target_chars=100,
            max_chars=160,
            overlap_chars=40,
        )

        self.assertEqual([c.text for c in chunks], ["tiny\n" + "w" * 110])

    def test_overlap_is_dropped_when_it_would_push_past_the_max(self):
        chunks = chunk_note(
            "",
            doc(para("a" * 90), para("b" * 30), para("c" * 150)),
            target_chars=120,
            max_chars=160,
            overlap_chars=40,
        )

        self.assertEqual(chunks[-1].text, "c" * 150)
        self.assertTrue(all(len(c.text) <= 160 for c in chunks))


class SentenceSplitTests(SimpleTestCase):
    """Abbreviations and initials are not sentence ends (D82)."""

    def split(self, text):
        return _SENTENCES.split(text)

    def test_ordinary_sentences_still_split(self):
        self.assertEqual(self.split("One. Two! Three? Four."), ["One.", "Two!", "Three?", "Four."])

    def test_abbreviations_do_not_split(self):
        for text in (
            "Use a tool, e.g. a hammer.",
            "That is, i.e. the plan.",
            "Dr. Who arrived.",
            "Mr. Smith and Mrs. Jones met Ms. Lee.",
            "Cats vs. dogs is settled.",
            "It costs approx. five pounds.",
            "See No. 5 for details.",
            "Pens, paper, etc. are on the desk.",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.split(text), [text])

    def test_initials_do_not_split(self):
        self.assertEqual(self.split("Ask J. R. R. Tolkien."), ["Ask J. R. R. Tolkien."])

    def test_a_real_end_after_an_abbreviation_sentence_still_splits(self):
        self.assertEqual(
            self.split("Dr. Who left. Mr. Smith stayed."), ["Dr. Who left.", "Mr. Smith stayed."]
        )

    def test_a_word_merely_ending_like_an_abbreviation_still_splits(self):
        # "casino." ends in "no." but is not "No."; \b and case keep them apart.
        self.assertEqual(self.split("It is a casino. Go."), ["It is a casino.", "Go."])
        self.assertEqual(self.split("The Demo. Go."), ["The Demo.", "Go."])

    def test_long_paragraph_keeps_abbreviations_inside_chunks(self):
        sentence = "Ask Dr. Who about it, e.g. on Friday."
        chunks = chunk_note("", doc(para(" ".join([sentence] * 12))), **SMALL)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertTrue(chunk.text.startswith("Ask Dr. Who"), chunk.text)
            self.assertTrue(chunk.text.endswith("Friday."), chunk.text)

    def test_output_is_deterministic(self):
        body = doc(para(" ".join(["Ask Dr. Who, e.g. on Friday."] * 12)))
        first = chunk_note("", body, **SMALL)
        self.assertEqual(first, chunk_note("", body, **SMALL))


class LongBlockTests(SimpleTestCase):
    def test_a_long_paragraph_splits_on_sentences(self):
        sentences = [f"Sentence {i} says something useful." for i in range(20)]

        chunks = chunk_note("", doc(para(" ".join(sentences))), **SMALL)

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.text), SMALL["max_chars"])
            self.assertTrue(chunk.text.endswith("."), chunk.text)
        joined = " ".join(c.text for c in chunks)
        for sentence in sentences:
            self.assertIn(sentence, joined)

    def test_a_sentence_with_no_punctuation_splits_on_words(self):
        words = [f"word{i}" for i in range(80)]

        chunks = chunk_note("", doc(para(" ".join(words))), **SMALL)

        found = [w for c in chunks for w in c.text.split()]
        # Every word survives whole; overlap may repeat some.
        self.assertEqual(set(found), set(words))
        self.assertTrue(all(len(c.text) <= SMALL["max_chars"] for c in chunks))

    def test_one_enormous_word_is_cut_by_length(self):
        chunks = chunk_note("", doc(para("x" * 1000)), **SMALL)

        self.assertEqual("".join(c.text for c in chunks), "x" * 1000)
        self.assertTrue(all(len(c.text) <= SMALL["target_chars"] for c in chunks))

    def test_a_long_code_block_splits_on_lines(self):
        lines = [f"    value_{i} = compute({i})" for i in range(30)]

        chunks = chunk_note(
            "", doc({"type": "codeBlock", "content": [text("\n".join(lines))]}), **SMALL
        )

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            for line in chunk.text.split("\n"):
                self.assertIn(line, lines)

    def test_a_list_item_longer_than_the_max_is_the_only_item_ever_split(self):
        item = " ".join(f"Clause {i} of a very long item." for i in range(15))

        chunks = chunk_note("", doc(bullets(item, "short")), **SMALL)

        self.assertTrue(chunks[0].text.startswith("- Clause 0"))
        self.assertEqual(chunks[-1].text.split("\n")[-1], "- short")
        self.assertTrue(all(len(c.text) <= SMALL["max_chars"] for c in chunks))


class EmbedTextAndHashTests(SimpleTestCase):
    def test_embed_text_is_title_and_path_then_text(self):
        chunk = chunk_note("Trip", doc(heading(1, "Day 2"), para("Move it to Friday.")))[0]

        self.assertEqual(chunk.text, "Move it to Friday.")
        self.assertEqual(chunk.embed_text, "Trip > Day 2\n\nMove it to Friday.")

    def test_without_headings_the_prefix_is_the_title(self):
        chunk = chunk_note("  Trip \n plans ", doc(para("x")))[0]

        self.assertEqual(chunk.embed_text, "Trip plans\n\nx")

    def test_without_title_or_headings_embed_text_is_the_text(self):
        chunk = chunk_note("", doc(para("x")))[0]

        self.assertEqual(chunk.embed_text, "x")

    def test_a_non_string_title_is_treated_as_empty(self):
        chunk = chunk_note(None, doc(para("x")))[0]

        self.assertEqual(chunk.embed_text, "x")

    def test_content_hash_is_sha256_of_embed_text(self):
        chunk = chunk_note("Trip", doc(para("x")))[0]

        self.assertEqual(chunk.content_hash, hashlib.sha256(b"Trip\n\nx").hexdigest())

    def test_a_title_change_changes_the_hash_but_not_the_text(self):
        before = chunk_note("Trip", doc(para("x")))[0]
        after = chunk_note("Holiday", doc(para("x")))[0]

        self.assertEqual(before.text, after.text)
        self.assertNotEqual(before.content_hash, after.content_hash)

    def test_an_edit_changes_only_the_edited_chunks_hash(self):
        base = [heading(1, "A"), para("alpha"), heading(1, "B"), para("beta")]
        edited = [heading(1, "A"), para("alpha"), heading(1, "B"), para("beta!")]

        before = chunk_note("T", doc(*base))
        after = chunk_note("T", doc(*edited))

        self.assertEqual(before[0].content_hash, after[0].content_hash)
        self.assertNotEqual(before[1].content_hash, after[1].content_hash)


class DeterminismTests(SimpleTestCase):
    def test_same_input_gives_identical_chunks(self):
        body = doc(
            heading(1, "Project"),
            *[para(f"Paragraph {i}. " * (i % 5 + 1)) for i in range(30)],
            tasks(*[(f"task {i}", i % 3 == 0) for i in range(20)]),
        )

        first = chunk_note("Title", body, **SMALL)
        second = chunk_note("Title", body, **SMALL)

        self.assertEqual(first, second)
        self.assertEqual([c.ordinal for c in first], list(range(len(first))))
        self.assertEqual(len({c.content_hash for c in first}), len(first))

    def test_chunks_are_immutable(self):
        chunk = chunk_note("", doc(para("x")))[0]

        with self.assertRaises(AttributeError):
            chunk.text = "y"


class BadInputTests(SimpleTestCase):
    def test_empty_documents_give_no_chunks(self):
        for content in (
            None,
            {},
            {"type": "doc"},
            doc(),
            doc(para("")),
            doc(para("   \n ")),
            doc({"type": "paragraph"}),
            doc(bullets("")),
        ):
            with self.subTest(content=content):
                self.assertEqual(chunk_note("Title", content), [])

    def test_malformed_documents_never_raise(self):
        cases = [
            "a string",
            ["a", "list"],
            42,
            {"type": "doc", "content": "not a list"},
            {"type": "doc", "content": [None, 3, "x", []]},
            doc({"type": "paragraph", "content": [{"type": "text", "text": 5}]}),
            doc({"type": "paragraph", "content": [{"text": "no type"}]}),
            doc({"type": "heading", "attrs": "nope", "content": [text("H")]}),
            doc({"type": "heading", "attrs": {"level": "2"}, "content": [text("H")]}),
            doc({"type": "heading", "attrs": {"level": True}, "content": [text("H")]}),
            doc({"type": "heading", "attrs": {"level": 99}, "content": [text("H")]}),
            doc(
                {
                    "type": "orderedList",
                    "attrs": {"start": "x"},
                    "content": [None, {"type": "listItem"}],
                }
            ),
            doc(
                {
                    "type": "taskList",
                    "content": [{"type": "taskItem", "attrs": None, "content": [para("t")]}],
                }
            ),
            doc({"type": "bulletList", "content": "nope"}),
            doc({"type": 7, "content": [text("odd")]}),
        ]
        for content in cases:
            with self.subTest(content=content):
                chunks = chunk_note("T", content)
                self.assertIsInstance(chunks, list)

    def test_what_is_readable_in_a_malformed_document_survives(self):
        chunks = chunk_note(
            "",
            doc(
                None,
                {"type": "paragraph", "content": [text("kept"), {"type": "text", "text": 5}]},
                text("stray inline"),
            ),
        )

        self.assertEqual(chunks[0].text, "kept\nstray inline")

    def test_a_deeply_nested_document_is_truncated_not_a_crash(self):
        node = para("bottom")
        for _ in range(MAX_DEPTH * 50):
            node = {"type": "blockquote", "content": [node]}

        self.assertEqual(chunk_note("", doc(node)), [])

    def test_deeply_nested_lists_do_not_crash(self):
        node = bullets("leaf")
        for _ in range(MAX_DEPTH * 50):
            node = {
                "type": "bulletList",
                "content": [{"type": "listItem", "content": [para("x"), node]}],
            }

        chunks = chunk_note("", doc(node))

        self.assertTrue(chunks)


class SettingsTests(SimpleTestCase):
    @override_settings(CHUNK_TARGET_CHARS=30, CHUNK_MAX_CHARS=40, CHUNK_OVERLAP_CHARS=0)
    def test_sizes_default_to_the_settings(self):
        chunks = chunk_note("", doc(*[para(f"paragraph {i}") for i in range(6)]))

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c.text) <= 40 for c in chunks))

    def test_inconsistent_sizes_are_a_configuration_error(self):
        for sizes in (
            {"target_chars": 0, "max_chars": 10, "overlap_chars": 0},
            {"target_chars": 20, "max_chars": 10, "overlap_chars": 0},
            {"target_chars": 20, "max_chars": 30, "overlap_chars": 20},
            {"target_chars": 20, "max_chars": 30, "overlap_chars": -1},
        ):
            with self.subTest(sizes=sizes), self.assertRaises(ImproperlyConfigured):
                chunk_note("", doc(para("x")), **sizes)
