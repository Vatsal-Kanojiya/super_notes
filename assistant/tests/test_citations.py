"""Reading [n] markers out of an answer and mapping them back to notes."""

from django.test import SimpleTestCase

from assistant.citations import cited_numbers, parse_citations, snippet
from assistant.prompt import Excerpt


def excerpts(count):
    return [
        Excerpt(
            n=n,
            note_id=100 + n,
            chunk_id=200 + n,
            title=f"Note {n}",
            heading_path="",
            text=f"Excerpt {n} text.",
        )
        for n in range(1, count + 1)
    ]


class CitedNumbersTests(SimpleTestCase):
    def test_marker_forms(self):
        cases = {
            "Friday [1].": [1],
            "Both [1][3].": [1, 3],
            "Both [1, 3].": [1, 3],
            "Both [1,3] and [ 2 ; 4 ].": [1, 3, 2, 4],
            "A range [2-4].": [2, 3, 4],
            "An en dash [2–3].": [2, 3],
            "Backwards [3-2].": [2, 3],
            "Mixed [1, 3-4].": [1, 3, 4],
        }
        for answer, expected in cases.items():
            with self.subTest(answer=answer):
                self.assertEqual(cited_numbers(answer, set(range(1, 9))), expected)

    def test_first_appearance_order_without_duplicates(self):
        self.assertEqual(cited_numbers("a [3] b [1] c [3][1] d [2]", {1, 2, 3}), [3, 1, 2])

    def test_things_in_brackets_that_are_not_markers(self):
        answer = "[see above] [link](http://x) [^1] [1a] [a1] [] [1,] [-1]"
        self.assertEqual(cited_numbers(answer, set(range(1, 9))), [])

    def test_numbers_outside_the_excerpts_are_ignored(self):
        self.assertEqual(cited_numbers("In [2024] and [9] and [0] but [2].", {1, 2}), [2])

    def test_a_huge_range_expands_only_over_real_excerpts(self):
        self.assertEqual(cited_numbers("[1-999999999]", {1, 2, 3}), [1, 2, 3])

    def test_without_a_valid_set_a_huge_range_is_read_as_its_ends(self):
        self.assertEqual(cited_numbers("[2-4]"), [2, 3, 4])
        self.assertEqual(cited_numbers("[1-999999999]"), [1, 999999999])


class ParseCitationsTests(SimpleTestCase):
    def test_citations_map_to_the_right_notes_and_chunks(self):
        citations = parse_citations("Launch is Friday [2]. Milk [1][2].", excerpts(3))
        self.assertEqual(
            citations,
            [
                {
                    "n": 2,
                    "note_id": 102,
                    "chunk_id": 202,
                    "title": "Note 2",
                    "snippet": "Excerpt 2 text.",
                },
                {
                    "n": 1,
                    "note_id": 101,
                    "chunk_id": 201,
                    "title": "Note 1",
                    "snippet": "Excerpt 1 text.",
                },
            ],
        )

    def test_an_invented_marker_is_dropped(self):
        citations = parse_citations("True [1]. Invented [7].", excerpts(2))
        self.assertEqual([c["n"] for c in citations], [1])

    def test_numbering_follows_the_excerpts_not_1_to_m(self):
        # After fit_excerpts, or if the task skips numbers, n is still the
        # excerpt's own.
        only_five = [e for e in excerpts(5) if e.n == 5]
        self.assertEqual(parse_citations("[5]", only_five)[0]["note_id"], 105)

    def test_no_markers_means_no_citations(self):
        self.assertEqual(parse_citations("Your notes don't cover this.", excerpts(3)), [])
        self.assertEqual(parse_citations("[1]", []), [])


class SnippetTests(SimpleTestCase):
    def test_short_text_is_kept_on_one_line(self):
        self.assertEqual(snippet("  one\n two\tthree "), "one two three")

    def test_long_text_is_cut_at_a_word_boundary(self):
        text = " ".join(["alpha"] * 100)
        # 29 characters end exactly on a word: nothing is lost.
        self.assertEqual(snippet(text, limit=30), "alpha alpha alpha alpha alpha…")
        # 31 would end mid-word: back off to the last whole one.
        self.assertEqual(snippet(text, limit=32), "alpha alpha alpha alpha alpha…")

    def test_one_unbroken_word_is_cut_hard(self):
        self.assertEqual(snippet("x" * 50, limit=10), "x" * 9 + "…")
