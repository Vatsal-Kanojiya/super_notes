"""assistant/tasks.py: answering, the relevance floor, and every way to fail.

Celery runs eagerly and the fake chat and embedding providers are forced
(config/test_runner.py), so .delay() runs the whole pipeline inline --
retries included, without their backoff.
"""

from datetime import timedelta
from unittest import mock

from celery.exceptions import Retry
from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from assistant import quota, tasks
from assistant.chat import ChatError, ChatResult, TransientChatError
from assistant.models import AskQuery
from assistant.prompt import prompt_version
from assistant.tasks import answer_ask, is_relevant, sweep_stuck_asks
from notes import services
from notes.tests.helpers import doc, make_user
from retrieval.embeddings import EmbeddingError
from retrieval.indexing import index_note
from retrieval.search import SearchHit

COMPLETE = "assistant.chat.complete"


def write(owner, title, body):
    """Create and index a one-paragraph note."""
    note = services.create_note(owner, title=title, content=doc(body))
    # Straight to the indexer: TestCase never runs the on-commit task.
    index_note(note.pk, note.version)
    return note


def ask(user, question, status=AskQuery.Status.PENDING):
    return AskQuery.objects.create(
        user=user, question=question, idempotency_key=question[:100], status=status
    )


def hit(similarity=None, keyword_rank=None):
    return SearchHit(
        chunk_id=1,
        note_id=1,
        title="t",
        heading_path="",
        text="x",
        score=1 / 61,
        similarity=similarity,
        keyword_rank=keyword_rank,
    )


class IsRelevantTests(SimpleTestCase):
    def test_nothing_retrieved(self):
        self.assertFalse(is_relevant([], floor=0.0))

    def test_similarity_at_or_above_the_floor(self):
        self.assertTrue(is_relevant([hit(similarity=0.3)], floor=0.3))
        self.assertFalse(is_relevant([hit(similarity=0.29)], floor=0.3))

    def test_any_hit_suffices(self):
        self.assertTrue(is_relevant([hit(similarity=0.1), hit(similarity=0.5)], floor=0.3))

    def test_a_keyword_match_passes_whatever_its_similarity(self):
        self.assertTrue(is_relevant([hit(similarity=0.1, keyword_rank=0.06)], floor=0.3))
        self.assertTrue(is_relevant([hit(keyword_rank=0.06)], floor=0.3))

    def test_the_rrf_score_is_never_compared(self):
        # A top-ranked hit with no similarity and no keyword match: rank alone
        # says nothing about relevance (D69).
        self.assertFalse(is_relevant([hit()], floor=0.0))


class AnswerTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.passport = write(
            self.alice, "Passport", "My passport expires in March 2027. Renew it at the office."
        )
        self.launch = write(self.alice, "Launch plan", "The passport photo booth launch is Friday.")
        self.groceries = write(self.alice, "Groceries", "Buy milk, eggs and bread.")
        # Near-identical words, another owner: must never be cited.
        write(make_user("bob"), "Passport", "My passport expires in June 2030.")

    def test_answers_with_citations_to_the_right_notes(self):
        query = ask(self.alice, "When does my passport expire?")

        answer_ask.delay(query.pk)

        query.refresh_from_db()
        self.assertEqual(query.status, "done")
        self.assertEqual((query.provider, query.model), ("fake", "fake"))
        self.assertEqual(query.prompt_version, prompt_version())
        self.assertGreater(query.input_tokens, 0)
        self.assertGreater(query.output_tokens, 0)
        self.assertIsNotNone(query.completed_at)
        self.assertEqual(query.error, "")

        # The fake quotes excerpts [1] and [2]: retrieval's top two hits.
        self.assertIn("[1]", query.answer)
        top_two = [row["note_id"] for row in query.retrieved[:2]]
        self.assertEqual([c["n"] for c in query.citations], [1, 2])
        self.assertEqual([c["note_id"] for c in query.citations], top_two)
        self.assertEqual(query.citations[0]["note_id"], self.passport.pk)
        self.assertEqual(query.citations[0]["title"], "Passport")
        self.assertIn("March 2027", query.citations[0]["snippet"])
        mine = {self.passport.pk, self.launch.pk, self.groceries.pk}
        self.assertTrue({row["note_id"] for row in query.retrieved} <= mine)
        self.assertEqual(
            set(query.retrieved[0]), {"chunk_id", "note_id", "score", "similarity", "keyword_rank"}
        )

    def test_retrieves_ask_retrieval_k(self):
        query = ask(self.alice, "passport")
        with (
            override_settings(ASK_RETRIEVAL_K=1),
            mock.patch.object(tasks, "search", wraps=tasks.search) as search,
        ):
            answer_ask.delay(query.pk)
        self.assertEqual(search.call_args.kwargs["k"], 1)
        query.refresh_from_db()
        self.assertEqual(len(query.retrieved), 1)

    def test_nothing_retrieved_makes_no_provider_call(self):
        query = ask(make_user("carol"), "When does my passport expire?")

        with mock.patch(COMPLETE) as complete:
            answer_ask.delay(query.pk)

        complete.assert_not_called()
        query.refresh_from_db()
        self.assertEqual(query.status, "done")
        self.assertEqual(query.answer, settings.ASK_NO_ANSWER_TEXT)
        self.assertEqual((query.provider, query.input_tokens, query.output_tokens), ("", 0, 0))
        self.assertEqual((query.citations, query.retrieved), ([], []))

    @override_settings(ASK_RELEVANCE_FLOOR=0.5)
    def test_below_the_floor_makes_no_provider_call(self):
        # No word in common with any note, so no keyword match either.
        query = ask(self.alice, "Which violin concerto did Heifetz record?")

        with mock.patch(COMPLETE) as complete:
            answer_ask.delay(query.pk)

        complete.assert_not_called()
        query.refresh_from_db()
        self.assertEqual((query.status, query.answer), ("done", settings.ASK_NO_ANSWER_TEXT))
        # What was retrieved is kept, scores and all: it is how the floor is tuned.
        self.assertTrue(query.retrieved)
        self.assertTrue(all(row["similarity"] < 0.5 for row in query.retrieved))

    @override_settings(ASK_RELEVANCE_FLOOR=0.99)
    def test_a_keyword_match_below_the_floor_still_asks(self):
        query = ask(self.alice, "passport")

        answer_ask.delay(query.pk)

        query.refresh_from_db()
        self.assertEqual(query.provider, "fake")
        self.assertEqual(query.citations[0]["note_id"], self.passport.pk)

    def test_chat_error_fails_the_ask_with_a_safe_message(self):
        query = ask(self.alice, "When does my passport expire?")

        with (
            mock.patch(COMPLETE, side_effect=ChatError("invalid x-api-key sk-secret")),
            self.assertLogs("assistant.tasks", "WARNING"),
        ):
            answer_ask.delay(query.pk)

        query.refresh_from_db()
        self.assertEqual((query.status, query.error), ("failed", tasks.CHAT_FAILED))
        self.assertNotIn("sk-secret", query.error)
        self.assertIsNotNone(query.completed_at)
        # Kept for debugging even though the ask failed.
        self.assertTrue(query.retrieved)

    def test_embedding_error_fails_the_ask(self):
        query = ask(self.alice, "When does my passport expire?")

        with (
            mock.patch.object(tasks, "search", side_effect=EmbeddingError("bad key")),
            mock.patch(COMPLETE) as complete,
            self.assertLogs("assistant.tasks", "WARNING"),
        ):
            answer_ask.delay(query.pk)

        complete.assert_not_called()
        query.refresh_from_db()
        self.assertEqual((query.status, query.error), ("failed", tasks.SEARCH_FAILED))

    def test_transient_error_is_retried_then_answered(self):
        query = ask(self.alice, "When does my passport expire?")
        answer = ChatResult(text="In March 2027 [1].", provider="fake", model="fake")

        with mock.patch(COMPLETE, side_effect=[TransientChatError("429"), answer]) as complete:
            # throw=False: with eager propagation on, a retry would be raised
            # as Retry instead of run; without it apply() runs the next attempt.
            answer_ask.apply((query.pk,), throw=False)

        self.assertEqual(complete.call_count, 2)
        query.refresh_from_db()
        self.assertEqual((query.status, query.answer), ("done", "In March 2027 [1]."))
        self.assertEqual(query.citations[0]["note_id"], self.passport.pk)

    def test_a_transient_error_on_the_last_retry_fails_the_ask(self):
        query = ask(self.alice, "When does my passport expire?")

        with (
            mock.patch(COMPLETE, side_effect=TransientChatError("503 upstream sk-secret")),
            self.assertLogs("assistant.tasks", "ERROR"),
        ):
            answer_ask.apply((query.pk,), retries=answer_ask.max_retries)

        query.refresh_from_db()
        self.assertEqual((query.status, query.error), ("failed", tasks.BUSY))

    def test_a_transient_error_before_the_last_retry_is_retried(self):
        query = ask(self.alice, "When does my passport expire?")

        with (
            mock.patch(COMPLETE, side_effect=TransientChatError("503")),
            self.assertRaises(Retry),
        ):
            answer_ask.apply((query.pk,), retries=answer_ask.max_retries - 1)

        query.refresh_from_db()
        self.assertEqual(query.status, "running")

    def test_an_unexpected_error_fails_the_ask_too(self):
        query = ask(self.alice, "When does my passport expire?")

        with (
            mock.patch(COMPLETE, side_effect=RuntimeError("bug")),
            self.assertLogs("assistant.tasks", "ERROR"),
            self.assertRaises(RuntimeError),
        ):
            answer_ask.delay(query.pk)

        query.refresh_from_db()
        self.assertEqual((query.status, query.error), ("failed", tasks.UNEXPECTED))

    def test_a_finished_ask_is_left_alone(self):
        for status in (AskQuery.Status.DONE, AskQuery.Status.FAILED):
            query = ask(self.alice, f"Already {status}", status=status)

            with mock.patch(COMPLETE) as complete:
                answer_ask.delay(query.pk)

            complete.assert_not_called()
            query.refresh_from_db()
            self.assertEqual((query.status, query.answer, query.retrieved), (status, "", []))

    def test_a_running_ask_is_taken_up_again(self):
        # What a redelivery looks like after a worker died mid-ask.
        query = ask(self.alice, "When does my passport expire?", status=AskQuery.Status.RUNNING)

        answer_ask.delay(query.pk)

        query.refresh_from_db()
        self.assertEqual(query.status, "done")

    def test_a_missing_ask_is_a_no_op(self):
        answer_ask.delay(999_999)

    def test_retries_only_transient_errors(self):
        self.assertEqual(set(answer_ask.autoretry_for), set(tasks.TRANSIENT))
        self.assertFalse(issubclass(ChatError, tasks.TRANSIENT))


class SweepStuckAsksTests(TestCase):
    """The sweeper fails what D76's in-task handling cannot reach (D78)."""

    def setUp(self):
        self.user = make_user()

    def aged(self, question, status, seconds):
        row = ask(self.user, question, status=status)
        # auto_now_add ignores a value passed to create(); back-date after.
        AskQuery.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - timedelta(seconds=seconds)
        )
        return row

    def test_default_cutoff_outlasts_the_full_retry_span(self):
        attempts = answer_ask.max_retries + 1
        span = attempts * settings.CELERY_TASK_TIME_LIMIT + (1 + 2 + 4 + 8)
        self.assertGreater(settings.ASK_STUCK_AFTER_SECONDS, span)

    def test_is_scheduled_every_five_minutes(self):
        entry = settings.CELERY_BEAT_SCHEDULE["sweep-stuck-asks"]
        self.assertEqual(entry["task"], "assistant.tasks.sweep_stuck_asks")
        self.assertEqual(entry["schedule"], 300)

    @override_settings(ASK_STUCK_AFTER_SECONDS=100)
    def test_old_unfinished_asks_fail_with_a_user_safe_message(self):
        running = self.aged("a", AskQuery.Status.RUNNING, 200)
        pending = self.aged("b", AskQuery.Status.PENDING, 200)
        self.assertEqual(sweep_stuck_asks(), 2)
        for row in (running, pending):
            row.refresh_from_db()
            self.assertEqual(row.status, AskQuery.Status.FAILED)
            self.assertEqual(row.error, tasks.STUCK)
            self.assertIsNotNone(row.completed_at)

    @override_settings(ASK_STUCK_AFTER_SECONDS=100)
    def test_recent_and_finished_asks_are_left_alone(self):
        recent = self.aged("a", AskQuery.Status.RUNNING, 10)
        done = self.aged("b", AskQuery.Status.DONE, 500)
        failed = self.aged("c", AskQuery.Status.FAILED, 500)
        failed.error = "kept"
        failed.save()
        self.assertEqual(sweep_stuck_asks(), 0)
        for row, status in (
            (recent, "running"),
            (done, "done"),
            (failed, "failed"),
        ):
            row.refresh_from_db()
            self.assertEqual(row.status, status)
        failed.refresh_from_db()
        self.assertEqual(failed.error, "kept")

    @override_settings(ASK_STUCK_AFTER_SECONDS=100)
    def test_an_ask_that_finished_meanwhile_is_not_overwritten(self):
        row = self.aged("a", AskQuery.Status.RUNNING, 200)
        # The worker finishes between the sweeper's decision and its write:
        # the UPDATE's own WHERE is what protects it, so finish it first.
        tasks._finish(row.pk, answer="real answer")
        sweep_stuck_asks()
        row.refresh_from_db()
        self.assertEqual(row.status, AskQuery.Status.DONE)
        self.assertEqual(row.answer, "real answer")
        self.assertEqual(row.error, "")

    @override_settings(ASK_STUCK_AFTER_SECONDS=100)
    def test_a_swept_ask_stops_counting_against_the_quota(self):
        row = self.aged("a", AskQuery.Status.RUNNING, 200)
        self.assertEqual(quota.used(self.user), 1)
        sweep_stuck_asks()
        self.assertEqual(quota.used(self.user), 0)
        row.refresh_from_db()
        self.assertEqual(row.status, AskQuery.Status.FAILED)
