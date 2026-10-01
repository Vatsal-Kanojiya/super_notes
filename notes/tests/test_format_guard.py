"""notes/format_guard.py: what a formatted note may and may not change.

Pure functions on plain documents, so no database. Each case is the kind of
thing a formatting model really does, named for what the guard must say.
"""

from django.test import SimpleTestCase

from notes.format_guard import (
    CHECKED_CHANGED,
    DATE_ADDED,
    FACT_WORD_CHANGED,
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


def ordered(*lines, start=None):
    node = {
        "type": "orderedList",
        "content": [{"type": "listItem", "content": [para(line)]} for line in lines],
    }
    if start is not None:
        node["attrs"] = {"start": start}
    return node


LONG_WORDS = (
    "alpha beta gamma delta epsilon zeta theta iota kappa lambda omicron sigma "
    "tau upsilon omega apple banana cherry grape lemon mango melon olive peach "
    "pear plum quince raisin tomato carrot celery garlic ginger onion pepper potato "
    "radish spinach turnip yam basil chive cumin dill fennel mint oregano parsley "
    "rosemary sage thyme anise clove nutmeg saffron vanilla almond cashew hazelnut "
    "pecan pistachio walnut barley millet oats quinoa rice rye sorghum wheat bulgur "
    "couscous farro lentil chickpea soybean pea bean kidney navy pinto lima mung "
    "adzuki fava edamame tofu tempeh seitan miso tahini hummus falafel pita naan "
    "roti paratha dosa idli vada sambar rasam chutney pickle papad halwa kheer "
    "ladoo barfi jalebi"
)


class FactWordTests(SimpleTestCase):
    """Rule 5 (D521): negations, number words and relative dates are facts."""

    def test_an_added_negation_is_refused(self):
        verdict = check(doc("Pay rent"), doc("Do not pay rent"))
        self.assertEqual(verdict.reason, FACT_WORD_CHANGED)

    def test_a_dropped_negation_is_refused(self):
        original = doc("don’t call the plumber about the kitchen sink")
        proposed = doc("Call the plumber about the kitchen sink")
        self.assertEqual(check(original, proposed).reason, FACT_WORD_CHANGED)

    def test_a_changed_number_word_is_refused(self):
        original = doc("buy five tickets for the concert on the weekend")
        proposed = doc("Buy six tickets for the concert on the weekend")
        self.assertEqual(check(original, proposed).reason, FACT_WORD_CHANGED)

    def test_an_added_relative_date_is_refused(self):
        original = doc("call the landlord about the leak in the bathroom")
        proposed = doc("Call the landlord about the leak in the bathroom tomorrow")
        self.assertEqual(check(original, proposed).reason, FACT_WORD_CHANGED)

    def test_a_repeated_negation_counts_as_a_multiset(self):
        original = doc("not the red one", "the blue one")
        proposed = doc("not the red one", "not the blue one")
        self.assertEqual(check(original, proposed).reason, FACT_WORD_CHANGED)

    def test_a_typo_fix_still_passes(self):
        original = doc("dont forget the umbrela and the raincoat")
        proposed = doc("dont forget the umbrella and the raincoat")
        self.assertTrue(check(original, proposed).ok)

    def test_a_typo_fixed_into_a_fact_word_passes(self):
        original = doc("remember to buy the groceries tomorow")
        self.assertTrue(check(original, doc("Remember to buy the groceries tomorrow")).ok)

    def test_a_fact_word_never_stands_for_another_one(self):
        original = doc("seventy chairs for the hall on the weekend")
        proposed = doc("seven chairs for the hall on the weekend")
        self.assertEqual(check(original, proposed).reason, FACT_WORD_CHANGED)

    def test_next_or_last_without_a_time_word_is_an_ordinary_word(self):
        original = doc("call the plumber about the kitchen sink and the leaking tap")
        for added in ("Next steps", "Last item"):
            with self.subTest(added=added):
                proposed = doc(heading(added), para(original["content"][0]["content"][0]["text"]))
                self.assertTrue(check(original, proposed).ok)

    def test_an_added_next_week_is_refused(self):
        original = doc("call the plumber about the kitchen sink and the leaking tap")
        proposed = doc("call the plumber next week about the kitchen sink and the leaking tap")
        self.assertEqual(check(original, proposed).reason, FACT_WORD_CHANGED)

    def test_next_week_becoming_last_week_is_refused(self):
        original = doc("the plumber came next week about the kitchen sink")
        proposed = doc("the plumber came last week about the kitchen sink")
        self.assertEqual(check(original, proposed).reason, FACT_WORD_CHANGED)

    def test_next_before_a_weekday_counts(self):
        original = doc("call the plumber about the kitchen sink on Monday")
        proposed = doc("call the plumber about the kitchen sink next Monday")
        self.assertEqual(check(original, proposed).reason, FACT_WORD_CHANGED)

    def test_a_contraction_and_its_long_form_are_the_same(self):
        pairs = (
            (
                "do not pay the rent until the landlord fixes the boiler",
                "don't pay the rent until the landlord fixes the boiler",
            ),
            (
                "he can’t come to the meeting about the boiler repair",
                "he cannot come to the meeting about the boiler repair",
            ),
            (
                "the parcel wasn't delivered to the office this afternoon",
                "the parcel was not delivered to the office this afternoon",
            ),
        )
        for before, after in pairs:
            with self.subTest(before=before):
                self.assertTrue(check(doc(before), doc(after)).ok)
                self.assertTrue(check(doc(after), doc(before)).ok)

    def test_an_added_contracted_negation_is_still_refused(self):
        original = doc("pay the rent before the landlord fixes the boiler")
        proposed = doc("don't pay the rent before the landlord fixes the boiler")
        self.assertEqual(check(original, proposed).reason, FACT_WORD_CHANGED)

    def test_fact_words_kept_as_they_are_pass(self):
        original = doc("never pay twice", "next week: call two plumbers, not one")
        proposed = doc(
            heading("Plumbing"),
            bullets("never pay twice"),
            para("next week: call two plumbers, not one"),
        )
        self.assertTrue(check(original, proposed).ok)


class NewWordsCapTests(SimpleTestCase):
    """Rule 7's absolute cap (D522)."""

    def test_an_invented_paragraph_in_a_long_note_is_refused(self):
        original = doc(LONG_WORDS)
        invented = "Remember that grocery prices rise during festive season so shop early"
        proposed = doc(LONG_WORDS, invented)
        verdict = check(original, proposed)
        # The ratio alone lets it through (precision is far above 0.8)...
        self.assertGreater(verdict.precision, ORIGINAL)
        # ... the cap does not.
        self.assertEqual(verdict.reason, WORDS_ADDED)

    def test_a_few_headings_in_a_long_note_pass(self):
        original = doc(LONG_WORDS)
        half = len(LONG_WORDS.split()) // 2
        first, second = " ".join(LONG_WORDS.split()[:half]), " ".join(LONG_WORDS.split()[half:])
        proposed = doc(heading("Greek letters"), para(first), heading("Pantry list"), para(second))
        self.assertTrue(check(original, proposed).ok)

    def test_typo_fixes_do_not_count_as_new(self):
        typos = LONG_WORDS.replace("banana", "bananna").replace("cherry", "chery")
        typos = typos.replace("grape", "graep").replace("lemon", "lemmon")
        typos = typos.replace("mango", "mangoo").replace("melon", "mellon")
        typos = typos.replace("olive", "olivee").replace("peach", "peech")
        typos = typos.replace("potato", "potatoe")
        self.assertTrue(check(doc(typos), doc(LONG_WORDS)).ok)

    def test_the_floor_and_share_are_the_callers(self):
        original = doc("alpha beta gamma delta epsilon zeta eta theta iota kappa")
        proposed = doc(
            heading("Greek letters"),
            para("alpha beta gamma delta epsilon zeta eta theta iota kappa"),
        )
        loose = {"min_kept": 0.9, "min_original": 0.8}
        self.assertTrue(check_format(original, proposed, **loose, new_words_floor=2).ok)
        self.assertEqual(
            check_format(original, proposed, **loose, new_words_floor=1).reason, WORDS_ADDED
        )


class NumbersFromTheDocumentTests(SimpleTestCase):
    """Rule 2 reads the document's own numbers (D523)."""

    def test_a_changed_typed_marker_number_is_refused(self):
        original = doc("250) deposit for the flat")
        proposed = doc("500) deposit for the flat")
        self.assertEqual(check(original, proposed).reason, NUMBER_CHANGED)

    def test_a_typed_amount_dropped_into_a_list_starting_at_one_is_refused(self):
        original = doc("250) deposit for the flat")
        proposed = doc(ordered("deposit for the flat"))
        self.assertEqual(check(original, proposed).reason, NUMBER_CHANGED)

    def test_rebasing_a_list_is_refused(self):
        original = doc(ordered("deposit", "rent", start=12))
        proposed = doc(ordered("deposit", "rent", start=1))
        self.assertEqual(check(original, proposed).reason, NUMBER_CHANGED)

    def test_rebasing_to_a_default_start_is_refused(self):
        original = doc(ordered("deposit", "rent", start=12))
        self.assertEqual(check(original, doc(ordered("deposit", "rent"))).reason, NUMBER_CHANGED)

    def test_a_list_keeping_its_start_passes(self):
        items = ("pay the deposit for the flat", "pay the rent to the owner")
        original = doc(ordered(*items, start=12))
        proposed = doc(heading("Flat"), ordered(*items, start=12))
        self.assertTrue(check(original, proposed).ok)

    def test_typed_numbering_from_twelve_becoming_a_real_list_passes(self):
        original = doc("12. deposit", "13. rent")
        self.assertTrue(check(original, doc(ordered("deposit", "rent", start=12))).ok)

    def test_a_real_list_becoming_typed_numbering_passes(self):
        original = doc(ordered("buy milk", "walk the dog"))
        self.assertTrue(check(original, doc("1. buy milk", "2. walk the dog")).ok)

    def test_a_number_split_by_a_mark_is_one_number(self):
        original = doc("rent 4500")
        proposed = doc(para(text("rent 45"), text("00", marks=[{"type": "bold"}])))
        self.assertTrue(check(original, proposed).ok)
