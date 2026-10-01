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

``--conversations`` measures the follow-ups of the multi-turn fixtures
instead (docs/RAG.md "Conversations"): the last turn of each conversation
searched three ways -- as asked (raw), condensed by the chat provider the
way a turn is (the cheap "stands alone" check first, then the condenser,
falling back to raw if it fails), and as the human-written ``standalone``,
the upper bound. Hybrid search, the mode asks use. Condensing here records
no usage events: it calls the pure part of the condenser, so the eval needs
no AskQuery and consumes no ``condense`` limit -- but a real chat provider
is called, and costs.
"""

import statistics
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test.utils import override_settings

from assistant import chat, conversation
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

# The three ways a follow-up is searched (--conversations), and how a last turn
# can be handled: sent to the condenser, left as asked because it stands alone,
# or left as asked because the condenser failed or returned nothing.
VARIANTS = ("raw", "condensed", "standalone")
OUTCOMES = ("condensed", "stood alone", "fell back")


class Command(BaseCommand):
    help = (
        "Report recall@k and MRR for vector, keyword and hybrid retrieval over the eval "
        "fixtures (with --conversations: for multi-turn follow-ups, raw vs condensed vs "
        "standalone). Runs in a transaction that is rolled back; leaves no data."
    )

    def add_arguments(self, parser):
        parser.add_argument("--k", type=int, default=5, help="Notes scored per question.")
        parser.add_argument(
            "--provider", choices=sorted(PROVIDERS), help="Override EMBEDDING_PROVIDER."
        )
        parser.add_argument("--by-kind", action="store_true", help="Break down by question kind.")
        parser.add_argument(
            "--conversations",
            action="store_true",
            help="Evaluate multi-turn follow-ups: raw vs condensed vs the human standalone.",
        )

    def handle(self, *args, k=5, provider=None, by_kind=False, conversations=False, **options):
        if not 1 <= k <= settings.SEARCH_MAX_K:
            raise CommandError(f"--k must be between 1 and {settings.SEARCH_MAX_K}.")
        if conversations:
            return self.handle_conversations(k, provider, by_kind)
        eval_set = loader.load()
        with override_settings(EMBEDDING_PROVIDER=provider or settings.EMBEDDING_PROVIDER):
            try:
                with transaction.atomic():
                    results = self.run(eval_set)
                    transaction.set_rollback(True)
            except (EmbeddingError, EmbeddingTransientError) as exc:
                raise CommandError(f"Embedding failed: {exc}") from exc
            self.report(eval_set, results, k, by_kind)

    def load_notes(self, notes):
        """(user, {note id: fixture key}): a throwaway user with the fixture notes, indexed."""
        user = get_user_model().objects.create_user(email=f"eval-{uuid.uuid4().hex}@invalid")
        key_of = {}
        for fixture in notes:
            note = services.create_note(
                user, type=fixture.type, title=fixture.title, content=fixture.content
            )
            index_note(note.pk, note.version)
            key_of[note.pk] = fixture.key
        return user, key_of

    def run(self, eval_set):
        """{mode: {question id: ranked note keys}} and each question's top similarity."""
        user, key_of = self.load_notes(eval_set.notes)

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

    # --- Conversations ---------------------------------------------------

    def handle_conversations(self, k, provider, by_kind):
        eval_set = loader.load()
        conversations = loader.load_conversations()
        with override_settings(EMBEDDING_PROVIDER=provider or settings.EMBEDDING_PROVIDER):
            try:
                with transaction.atomic():
                    results = self.run_conversations(eval_set, conversations)
                    transaction.set_rollback(True)
            except (EmbeddingError, EmbeddingTransientError) as exc:
                raise CommandError(f"Embedding failed: {exc}") from exc
            self.report_conversations(eval_set, conversations, results, k, by_kind)

    def run_conversations(self, eval_set, conversations):
        """(rankings per variant, how each last turn was handled), keyed by conversation id."""
        user, key_of = self.load_notes(eval_set.notes)
        rankings = {variant: {} for variant in VARIANTS}
        outcome = {}

        def rank(question):
            hits = search(user, question, settings.SEARCH_MAX_K, mode="hybrid")
            return [key_of[hit.note_id] for hit in hits]

        for case in conversations:
            asked = case.last.question
            query, outcome[case.id] = self.condensed(case)
            rankings["raw"][case.id] = rank(asked)
            rankings["condensed"][case.id] = rank(query)
            rankings["standalone"][case.id] = rank(case.standalone)
        return rankings, outcome

    def condensed(self, case):
        """(the question a turn would search, how): as conversation.prepare decides it.

        The earlier turns are the fixture's questions, with no answers (there
        is no answer to repeat without running the whole ask on them).
        """
        asked = case.last.question
        if not conversation.needs_condensing(asked):
            return asked, "stood alone"
        history = [
            conversation.HistoryTurn(position, turn.question, "")
            for position, turn in enumerate(case.turns[:-1], start=1)
        ]
        try:
            rewritten, _ = conversation.run_condenser(asked, history)
        except (chat.ChatError, chat.TransientChatError) as exc:
            self.stderr.write(f"{case.id}: condensing failed, searched as asked ({exc})")
            return asked, "fell back"
        if not rewritten:
            return asked, "fell back"
        return rewritten, "condensed"

    def report_conversations(self, eval_set, conversations, results, k, by_kind):
        rankings, outcome = results
        answerable = sum(case.has_answer for case in conversations)
        counts = {name: list(outcome.values()).count(name) for name in OUTCOMES}
        self.stdout.write(
            f"Provider: {embedding_model_id()}. Chat: {settings.CHAT_PROVIDER}. "
            f"{len(eval_set.notes)} notes, {len(conversations)} conversations ({answerable} "
            f"answerable, {len(conversations) - answerable} no-answer). Hybrid search, k={k}."
        )
        if settings.EMBEDDING_PROVIDER == "fake" or settings.CHAT_PROVIDER == "fake":
            self.stdout.write(
                "Fake provider: a smoke test of the pipeline, not a measure of retrieval quality."
            )
        self.stdout.write(
            "Last turns: " + ", ".join(f"{counts[name]} {name}" for name in OUTCOMES) + "."
        )
        self.stdout.write("")

        relevant = {case.id: case.relevant for case in conversations}

        def summary(variant, cases):
            return evaluate(((rankings[variant][c.id], relevant[c.id]) for c in cases), k)

        self.stdout.write(f"{'follow-up':<12}{f'recall@{k}':>10}{'MRR':>8}")
        for variant in VARIANTS:
            result = summary(variant, conversations)
            self.stdout.write(f"{variant:<12}{fmt(result.recall_at_k):>10}{fmt(result.mrr):>8}")
        if not by_kind:
            return
        cells = "".join(f"{variant + f' r@{k} / MRR':>22}" for variant in VARIANTS)
        self.stdout.write("")
        self.stdout.write(f"{'kind':<16}{'n':>3}{cells}")
        for kind in sorted({case.kind for case in conversations if case.has_answer}):
            cases = [case for case in conversations if case.kind == kind]
            row = ""
            for variant in VARIANTS:
                result = summary(variant, cases)
                row += f"{fmt(result.recall_at_k) + ' / ' + fmt(result.mrr):>22}"
            self.stdout.write(f"{kind:<16}{len(cases):>3}{row}")


def fmt(value):
    return "-" if value is None else f"{value:.3f}"
