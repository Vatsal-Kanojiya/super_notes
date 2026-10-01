"""notes/format_guard.py: what a formatted note may and may not change.

Pure functions on plain documents, so no database. Each case is the kind of
thing a formatting model really does, named for what the guard must say.
"""

from django.test import SimpleTestCase

from notes.format_guard import (
    CHECKED_CHANGED,
    DATE_ADDED,
    INVALID,
    NUMBER_CHANGED,
    WORDS_ADDED,
    WORDS_LOST,
    check_format,
)

from .helpers import bullets, checklist_doc, doc, heading, para, task, tasks, text

KEPT, ORIGINAL = 0.9, 0.8


def check(original, proposed):
    return check_format(original, proposed, min_kept=KEPT, min_original=ORIGINAL)


BOOKING = (
    "Book the hotel before 12 March 2026. Pay 4,500 as the deposit and call Ravi on 98765-43210."
)
PACKING = "pack sunscreen towels charger passport"
MESSY = doc("Trip to Goa", BOOKING, PACKING)


class AllowedTests(SimpleTestCase):
    def test_the_same_document_passes(self):
        verdict = check(MESSY, MESSY)
        self.assertTrue(verdict.ok, verdict)
        self.assertEqual((verdict.recall, verdict.precision), (1.0, 1.0))

    def test_restructuring_into_a_heading_and_a_list_passes(self):
        proposed = doc(
            heading("Trip to Goa"),
            para("Book the hotel before 12 March 2026."),
            para("Pay 4,500 as the deposit and call Ravi on 98765-43210."),
            heading("Pack", 2),
            bullets("sunscreen", "towels", "charger", "passport"),
        )
        verdict = check(MESSY, proposed)
        # "Pack" is a new word: allowed by the precision floor.
        self.assertTrue(verdict.ok, verdict)

    def test_paragraphs_becoming_a_numbered_list_adds_no_numbers(self):
        original = doc("first buy milk", "second walk the dog", "third call mum")
        proposed = doc(
            {
                "type": "orderedList",
                "content": [
                    {"type": "listItem", "content": [para(line)]}
                    for line in ("first buy milk", "second walk the dog", "third call mum")
                ],
            }
        )
        self.assertTrue(check(original, proposed).ok)

    def test_a_typed_numbering_becoming_a_real_list_passes(self):
        original = doc("1. buy milk", "2) walk the dog", "- call mum")
        proposed = doc(
            {
                "type": "orderedList",
                "content": [
                    {"type": "listItem", "content": [para("buy milk")]},
                    {"type": "listItem", "content": [para("walk the dog")]},
                ],
            },
            bullets("call mum"),
        )
        self.assertTrue(check(original, proposed).ok)

    def test_bullets_becoming_an_unticked_checklist_passes(self):
        original = doc(bullets("milk", "eggs", "bread"))
        proposed = doc(tasks(task("milk"), task("eggs"), task("bread")))
        self.assertTrue(check(original, proposed).ok)

    def test_a_typo_fix_passes_even_in_a_short_note(self):
        original = doc("remember to buy the groceries tomorow")
        proposed = doc("Remember to buy the groceries tomorrow")
        verdict = check(original, proposed)
        self.assertTrue(verdict.ok, verdict)

    def test_case_and_punctuation_changes_pass(self):
        self.assertTrue(
            check(doc("hello world. this is fine"), doc("Hello world. This is fine!")).ok
        )

    def test_marks_split_the_text_without_changing_it(self):
        original = doc("meet at noon")
        proposed = doc(para(text("meet at "), text("noon", marks=[{"type": "bold"}])))
        self.assertTrue(check(original, proposed).ok)

    def test_ticked_items_stay_ticked(self):
        original = checklist_doc(("milk", True), ("eggs", False))
        proposed = doc(tasks(task("milk", True), task("eggs")))
        self.assertTrue(check(original, proposed).ok)

    def test_a_month_already_in_the_note_may_stay(self):
        self.assertTrue(check(doc("due in March"), doc(heading("Due"), para("in March"))).ok)

    def test_an_empty_note_stays_empty(self):
        self.assertTrue(check(doc(), doc()).ok)


class RefusedTests(SimpleTestCase):
    def test_a_dropped_paragraph(self):
        proposed = doc("Trip to Goa", BOOKING)
        verdict = check(MESSY, proposed)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, WORDS_LOST)
        self.assertLess(verdict.recall, KEPT)

    def test_a_dropped_list_item(self):
        original = doc(bullets("milk", "eggs", "bread", "butter", "jam"))
        proposed = doc(bullets("milk", "eggs", "bread", "butter"))
        self.assertEqual(check(original, proposed).reason, WORDS_LOST)

    def test_an_invented_date(self):
        proposed = doc(
            "Trip to Goa",
            BOOKING,
            PACKING,
            "Flight on 20 April 2026",
        )
        self.assertEqual(check(MESSY, proposed).reason, NUMBER_CHANGED)

    def test_an_invented_numeric_date(self):
        original = doc("dentist appointment sometime next week")
        self.assertEqual(
            check(original, doc("dentist appointment sometime next week 12/05/2026")).reason,
            NUMBER_CHANGED,
        )

    def test_an_invented_month_with_no_number(self):
        original = doc("dentist appointment sometime next week")
        proposed = doc("dentist appointment sometime next week in October")
        self.assertEqual(check(original, proposed).reason, DATE_ADDED)

    def test_an_invented_weekday(self):
        original = doc("call the landlord about the leak tomorrow")
        proposed = doc("call the landlord about the leak on Friday")
        self.assertEqual(check(original, proposed).reason, DATE_ADDED)

    def test_a_changed_amount(self):
        proposed = doc(
            "Trip to Goa",
            BOOKING.replace("4,500", "45,000"),
            PACKING,
        )
        self.assertEqual(check(MESSY, proposed).reason, NUMBER_CHANGED)

    def test_a_dropped_number_in_a_long_note(self):
        # Words kept is far above 0.9, but a price vanished.
        original = doc("meeting notes " + "alpha beta gamma delta " * 20, "total 250")
        proposed = doc("meeting notes " + "alpha beta gamma delta " * 20, "total")
        self.assertEqual(check(original, proposed).reason, NUMBER_CHANGED)

    def test_a_reformatted_number_is_refused(self):
        self.assertEqual(check(doc("rent 4500"), doc("rent 4,500")).reason, NUMBER_CHANGED)

    def test_a_changed_checkbox_state(self):
        original = checklist_doc(("milk", True), ("eggs", False))
        proposed = checklist_doc(("milk", True), ("eggs", True))
        self.assertEqual(check(original, proposed).reason, CHECKED_CHANGED)

    def test_unticking_is_refused_too(self):
        original = checklist_doc(("milk", True))
        self.assertEqual(check(original, checklist_doc(("milk", False))).reason, CHECKED_CHANGED)

    def test_a_new_paragraph_of_prose(self):
        proposed = doc(
            *MESSY["content"],
            "Goa has beautiful beaches and delicious seafood restaurants everywhere nearby.",
        )
        verdict = check(MESSY, proposed)
        self.assertEqual(verdict.reason, WORDS_ADDED)

    def test_a_rewrite_in_other_words(self):
        original = doc("the quick brown fox jumps over the lazy dog")
        proposed = doc("a fast auburn animal leaps across a sleepy hound")
        self.assertFalse(check(original, proposed).ok)

    def test_an_empty_result_for_a_note_with_words(self):
        self.assertEqual(check(doc("alpha beta gamma"), doc()).reason, WORDS_LOST)

    def test_not_a_document(self):
        for bad in (None, [], "text", {"type": "paragraph"}, {"type": "doc", "content": "x"}):
            with self.subTest(bad=bad):
                self.assertEqual(check(MESSY, bad).reason, INVALID)

    def test_a_node_without_a_type(self):
        self.assertEqual(check(MESSY, {"type": "doc", "content": [{"text": "x"}]}).reason, INVALID)

    def test_a_deeply_nested_document_is_refused_without_recursing(self):
        node = {"type": "paragraph", "content": [text("x")]}
        for _ in range(500):
            node = {"type": "blockquote", "content": [node]}
        self.assertEqual(check(MESSY, {"type": "doc", "content": [node]}).reason, INVALID)

    def test_thresholds_are_the_callers(self):
        original = doc("alpha beta gamma delta epsilon zeta eta theta iota kappa")
        proposed = doc("alpha beta gamma delta epsilon zeta eta theta iota")
        self.assertTrue(check_format(original, proposed, min_kept=0.9, min_original=0.9).ok)
        self.assertFalse(check_format(original, proposed, min_kept=0.95, min_original=0.9).ok)
