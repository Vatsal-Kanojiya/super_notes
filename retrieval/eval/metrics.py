"""Retrieval metrics: recall@k and MRR, scored per note.

Search returns *chunks*, but a question is labelled with the *notes* that
answer it, and a person reading the results sees notes. So a ranking is first
collapsed to notes, keeping each note at the rank of its best chunk
(``dedupe_to_notes``). Without that, a long note whose five chunks fill the
top five would crowd everything else out of recall@5 while still counting
once, and the numbers would punish long notes for being long.

Both per-question functions dedupe their input themselves, so passing a
chunk-level list of note keys is safe. Definitions, for one question with
relevant notes R and a deduplicated ranking L (1-based ranks):

* recall@k = |R ∩ L[:k]| / |R|. For a question with two relevant notes,
  finding one of them in the top k scores 0.5.
* reciprocal rank = 1 / (rank of the first note in L that is in R), or 0 if
  none of R is ranked at all.

A question with no relevant notes (a ``no_answer`` question) has no recall
or rank to speak of: dividing by |R| = 0 is undefined, and scoring it 0 or 1
would drag the mean either way for reasons unrelated to ranking. The
per-question functions refuse it; ``evaluate`` skips it and counts it.
"""

from collections.abc import Collection, Hashable, Iterable, Sequence
from dataclasses import dataclass


def dedupe_to_notes(ranked: Iterable[Hashable]) -> list:
    """Drop repeats, keeping each note at its first (best) position.

    ``["n03", "n03", "n16", "n03"]`` becomes ``["n03", "n16"]``.
    """
    return list(dict.fromkeys(ranked))


def recall_at_k(ranked_note_keys: Sequence, relevant: Collection, k: int) -> float:
    """Fraction of the relevant notes found in the top ``k`` notes.

    ``k`` larger than the ranking just means the whole ranking is looked at.
    Raises ``ValueError`` if ``relevant`` is empty or ``k`` is below 1.
    """
    _check(relevant)
    if k < 1:
        raise ValueError("k must be at least 1.")
    top = set(dedupe_to_notes(ranked_note_keys)[:k])
    wanted = set(relevant)
    return len(top & wanted) / len(wanted)


def reciprocal_rank(ranked_note_keys: Sequence, relevant: Collection) -> float:
    """``1 / rank`` of the first relevant note (ranks start at 1), else 0.

    Only the first hit counts, even with several relevant notes: MRR asks
    "how far down is the first useful result", which is what a reader of the
    search page or the Ask prompt feels. Raises ``ValueError`` if
    ``relevant`` is empty.
    """
    _check(relevant)
    wanted = set(relevant)
    for rank, key in enumerate(dedupe_to_notes(ranked_note_keys), start=1):
        if key in wanted:
            return 1 / rank
    return 0.0


@dataclass(frozen=True)
class Summary:
    """The scores for one retrieval mode over a question set.

    ``recall_at_k`` and ``mrr`` are means over the ``answerable`` questions
    only, and ``None`` when there are none. ``no_answer`` is how many
    questions were skipped for having no relevant notes; the relevance floor
    is judged on those separately.
    """

    k: int
    recall_at_k: float | None
    mrr: float | None
    answerable: int
    no_answer: int


def evaluate(runs: Iterable[tuple[Sequence, Collection]], k: int) -> Summary:
    """Aggregate recall@k and MRR over ``(ranking, relevant)`` pairs.

    Each ranking may be chunk-level; it is deduplicated to notes before
    scoring. Every answerable question weighs the same (a macro average), so
    a question with two relevant notes does not count double.
    """
    if k < 1:
        raise ValueError("k must be at least 1.")
    recalls, ranks, no_answer = [], [], 0
    for ranking, relevant in runs:
        if not relevant:
            no_answer += 1
            continue
        recalls.append(recall_at_k(ranking, relevant, k))
        ranks.append(reciprocal_rank(ranking, relevant))
    answerable = len(recalls)
    return Summary(
        k=k,
        recall_at_k=sum(recalls) / answerable if answerable else None,
        mrr=sum(ranks) / answerable if answerable else None,
        answerable=answerable,
        no_answer=no_answer,
    )


def _check(relevant: Collection) -> None:
    if not relevant:
        raise ValueError("No relevant notes: recall and rank are undefined; skip the question.")
