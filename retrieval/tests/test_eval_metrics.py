"""recall@k and MRR on hand-built rankings, including the awkward cases."""

from django.test import SimpleTestCase

from retrieval.eval.metrics import dedupe_to_notes, evaluate, recall_at_k, reciprocal_rank


class DedupeTests(SimpleTestCase):
    def test_keeps_first_seen_order(self):
        chunks = ["n03", "n03", "n16", "n03", "n01", "n16"]
        self.assertEqual(dedupe_to_notes(chunks), ["n03", "n16", "n01"])

    def test_empty_and_already_unique(self):
        self.assertEqual(dedupe_to_notes([]), [])
        self.assertEqual(dedupe_to_notes(iter(["a", "b"])), ["a", "b"])


class RecallTests(SimpleTestCase):
    def test_single_relevant(self):
        self.assertEqual(recall_at_k(["a", "b", "c"], ["b"], k=2), 1.0)
        self.assertEqual(recall_at_k(["a", "b", "c"], ["c"], k=2), 0.0)

    def test_multi_relevant_is_the_fraction_found(self):
        self.assertEqual(recall_at_k(["a", "x", "y", "b"], ["a", "b"], k=3), 0.5)
        self.assertEqual(recall_at_k(["a", "x", "y", "b"], ["a", "b"], k=4), 1.0)

    def test_k_larger_than_the_ranking(self):
        self.assertEqual(recall_at_k(["a"], ["a", "b"], k=50), 0.5)
        self.assertEqual(recall_at_k([], ["a"], k=5), 0.0)

    def test_duplicates_do_not_use_up_the_window_or_count_twice(self):
        # Three chunks of one note would push "b" out of a naive top 3.
        self.assertEqual(recall_at_k(["a", "a", "a", "b"], ["b"], k=2), 1.0)
        self.assertEqual(recall_at_k(["a", "a"], ["a", "b"], k=5), 0.5)

    def test_no_relevant_or_bad_k_is_refused(self):
        with self.assertRaises(ValueError):
            recall_at_k(["a"], [], k=5)
        with self.assertRaises(ValueError):
            recall_at_k(["a"], ["a"], k=0)


class ReciprocalRankTests(SimpleTestCase):
    def test_rank_of_the_first_hit(self):
        self.assertEqual(reciprocal_rank(["a", "b", "c"], ["a"]), 1.0)
        self.assertEqual(reciprocal_rank(["a", "b", "c"], ["c"]), 1 / 3)

    def test_only_the_first_of_several_relevant_counts(self):
        self.assertEqual(reciprocal_rank(["x", "b", "a"], ["a", "b"]), 0.5)

    def test_rank_is_counted_after_dedupe(self):
        self.assertEqual(reciprocal_rank(["x", "x", "x", "a"], ["a"]), 0.5)

    def test_miss_scores_zero(self):
        self.assertEqual(reciprocal_rank(["x", "y"], ["a"]), 0.0)
        self.assertEqual(reciprocal_rank([], ["a"]), 0.0)

    def test_no_relevant_is_refused(self):
        with self.assertRaises(ValueError):
            reciprocal_rank(["a"], [])


class EvaluateTests(SimpleTestCase):
    def test_means_over_answerable_questions_only(self):
        runs = [
            (["a", "b"], ["a"]),  # recall 1, rr 1
            (["x", "b", "a"], ["a", "b"]),  # recall@2 0.5, rr 0.5
            (["x", "y"], ["a"]),  # recall 0, rr 0
            (["a", "b"], []),  # no answer: skipped
            (["c"], ()),  # no answer: skipped
        ]
        summary = evaluate(runs, k=2)
        self.assertEqual(summary.k, 2)
        self.assertEqual(summary.answerable, 3)
        self.assertEqual(summary.no_answer, 2)
        self.assertAlmostEqual(summary.recall_at_k, 1.5 / 3)
        self.assertAlmostEqual(summary.mrr, 1.5 / 3)

    def test_rankings_are_deduplicated(self):
        summary = evaluate([(["n1", "n1", "n1", "n2"], ["n2"])], k=2)
        self.assertEqual(summary.recall_at_k, 1.0)
        self.assertEqual(summary.mrr, 0.5)

    def test_nothing_answerable_gives_none_not_zero(self):
        summary = evaluate([(["a"], [])], k=5)
        self.assertIsNone(summary.recall_at_k)
        self.assertIsNone(summary.mrr)
        self.assertEqual((summary.answerable, summary.no_answer), (0, 1))
        self.assertEqual(evaluate([], k=5).answerable, 0)

    def test_bad_k_is_refused_even_with_no_answerable_questions(self):
        with self.assertRaises(ValueError):
            evaluate([(["a"], [])], k=0)
