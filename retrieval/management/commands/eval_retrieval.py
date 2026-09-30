"""Measure retrieval: recall@k and MRR per mode over the eval fixtures (plan §6.6).

Everything happens inside one transaction that is always rolled back: a
throwaway user, the fixture notes, their chunks. So it can be run against
any database, production included, and leaves nothing behind -- only the
embedding calls cost anything. The notes are created through
notes/services.py and indexed with ``index_note`` directly, so the eval
runs the same chunker and indexer as real notes.

Each search asks for SEARCH_MAX_K chunks and is scored on notes (D42):
recall@k is over the top k *notes*, which with the per-note cap may take
up to 2k chunks.
"""

import statistics
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test.utils import override_settings

from notes import services
from retrieval.embeddings import EmbeddingError, EmbeddingTransientError, embedding_model_id
from retrieval.embeddings.registry import PROVIDERS
from retrieval.eval import loader
from retrieval.eval.metrics import evaluate
from retrieval.indexing import index_note
from retrieval.search import MODES, search

# Vector first: it is the mode that raises if the provider fails, before
# hybrid could quietly fall back to keyword-only and skew its numbers.
EVAL_MODES = ("vector", "keyword", "hybrid")
assert set(EVAL_MODES) == set(MODES)


class Command(BaseCommand):
    help = (
        "Report recall@k and MRR for vector, keyword and hybrid retrieval over the eval "
        "fixtures. Runs in a transaction that is rolled back; leaves no data."
    )

    def add_arguments(self, parser):
        parser.add_argument("--k", type=int, default=5, help="Notes scored per question.")
        parser.add_argument(
            "--provider", choices=sorted(PROVIDERS), help="Override EMBEDDING_PROVIDER."
        )
        parser.add_argument("--by-kind", action="store_true", help="Break down by question kind.")

    def handle(self, *args, k=5, provider=None, by_kind=False, **options):
        if not 1 <= k <= settings.SEARCH_MAX_K:
            raise CommandError(f"--k must be between 1 and {settings.SEARCH_MAX_K}.")
        eval_set = loader.load()
        with override_settings(EMBEDDING_PROVIDER=provider or settings.EMBEDDING_PROVIDER):
            try:
                with transaction.atomic():
                    results = self.run(eval_set)
                    transaction.set_rollback(True)
            except (EmbeddingError, EmbeddingTransientError) as exc:
                raise CommandError(f"Embedding failed: {exc}") from exc
            self.report(eval_set, results, k, by_kind)

    def run(self, eval_set):
        """{mode: {question id: ranked note keys}} and each question's top similarity."""
        user = get_user_model().objects.create_user(email=f"eval-{uuid.uuid4().hex}@invalid")
        key_of = {}
        for fixture in eval_set.notes:
            note = services.create_note(
                user, type=fixture.type, title=fixture.title, content=fixture.content
            )
            index_note(note.pk, note.version)
            key_of[note.pk] = fixture.key

        rankings, top_similarity = {mode: {} for mode in EVAL_MODES}, {}
        for question in eval_set.questions:
            for mode in EVAL_MODES:
                hits = search(user, question.question, settings.SEARCH_MAX_K, mode=mode)
                rankings[mode][question.id] = [key_of[hit.note_id] for hit in hits]
                if mode == "vector":
                    top_similarity[question.id] = hits[0].similarity if hits else None
        return rankings, top_similarity

    def report(self, eval_set, results, k, by_kind):
        rankings, top_similarity = results
        questions = eval_set.questions
        answerable = sum(q.has_answer for q in questions)
        self.stdout.write(
            f"Provider: {embedding_model_id()}. {len(eval_set.notes)} notes, {len(questions)} "
            f"questions ({answerable} answerable, {len(questions) - answerable} no-answer). "
            f"k={k}."
        )
        if settings.EMBEDDING_PROVIDER == "fake":
            self.stdout.write(
                "Fake provider: a smoke test of the pipeline, not a measure of retrieval quality."
            )

        self.stdout.write("")
        self.table(f"{'mode':<10}", [("all", questions)], rankings, k, by_mode_rows=True)
        if by_kind:
            kinds = sorted({q.kind for q in questions if q.has_answer})
            groups = [(kind, [q for q in questions if q.kind == kind]) for kind in kinds]
            self.stdout.write("")
            self.table(f"{'kind':<16}", groups, rankings, k, by_mode_rows=False)

        self.stdout.write("")
        self.stdout.write("Top similarity (vector leg), for setting ASK_RELEVANCE_FLOOR:")
        for question in questions:
            if not question.has_answer:
                self.stdout.write(
                    f"  {question.id}  {fmt(top_similarity[question.id])}  "
                    f"(no answer) {question.question}"
                )
        answered = [
            top_similarity[q.id]
            for q in questions
            if q.has_answer and top_similarity[q.id] is not None
        ]
        if answered:
            self.stdout.write(
                f"  answerable: min {fmt(min(answered))}, median {fmt(statistics.median(answered))}"
            )

    def table(self, header, groups, rankings, k, by_mode_rows):
        """Rows of recall@k and MRR: one per mode, or one per group with a column per mode."""
        relevant = {q.id: q.relevant for _, qs in groups for q in qs}

        def summary(mode, qs):
            return evaluate(((rankings[mode][q.id], relevant[q.id]) for q in qs), k)

        if by_mode_rows:
            ((_, qs),) = groups
            self.stdout.write(f"{header}{f'recall@{k}':>10}{'MRR':>8}")
            for mode in EVAL_MODES:
                result = summary(mode, qs)
                self.stdout.write(f"{mode:<10}{fmt(result.recall_at_k):>10}{fmt(result.mrr):>8}")
            return
        cells = "".join(f"{mode + f' r@{k} / MRR':>22}" for mode in EVAL_MODES)
        self.stdout.write(f"{header}{'n':>3}{cells}")
        for name, qs in groups:
            row = ""
            for mode in EVAL_MODES:
                result = summary(mode, qs)
                row += f"{fmt(result.recall_at_k) + ' / ' + fmt(result.mrr):>22}"
            self.stdout.write(f"{name:<16}{len(qs):>3}{row}")


def fmt(value):
    return "-" if value is None else f"{value:.3f}"
