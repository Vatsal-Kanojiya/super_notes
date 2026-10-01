"""Asks racing at the quota edge, and turns racing each other, proved rather than asserted.

TransactionTestCase, as in notes/tests/test_concurrency.py: inside TestCase's
one rolled-back transaction a second thread could never see the first's
commit -- the very thing under test.
"""

import threading
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connections
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient

from assistant import services
from assistant.models import AskQuery, Conversation
from assistant.services import (
    ConversationNotFound,
    QuotaExceeded,
    TurnInProgress,
    create_ask,
    create_turn,
    delete_conversation,
)
from assistant.tasks import answer_ask
from limits import service as limits
from limits.models import UsageEvent
from notes.tests.helpers import make_user

from .helpers import chat_turns, record_usage


@override_settings(LIMIT_DEFAULTS=chat_turns(2, 10))
class ConcurrentAskTests(TransactionTestCase):
    def setUp(self):
        self.alice = make_user("alice")
        # Only the creates race here; answering them is another test's job.
        patcher = mock.patch.object(answer_ask, "delay")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_in_threads(self, target, count):
        """Start ``count`` threads at once, wait for them all; (results, errors)."""
        barrier = threading.Barrier(count)
        results, errors = [None] * count, []

        def run(index):
            try:
                barrier.wait(timeout=10)
                results[index] = target(index)
            except Exception as exc:
                results[index] = exc
                if not isinstance(exc, QuotaExceeded):
                    errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return results, errors

    def test_parallel_asks_at_the_edge_cannot_both_pass(self):
        record_usage(
            AskQuery.objects.create(user=self.alice, question="earlier", idempotency_key="earlier")
        )

        results, errors = self._run_in_threads(
            lambda i: create_ask(self.alice, f"Question {i}?", f"key-{i}"), 6
        )

        self.assertEqual(errors, [])
        refused = [r for r in results if isinstance(r, QuotaExceeded)]
        self.assertEqual(len(refused), 5)
        self.assertEqual(AskQuery.objects.filter(user=self.alice).count(), 2)

    def test_parallel_replays_of_one_key_make_one_ask(self):
        results, errors = self._run_in_threads(
            lambda i: create_ask(self.alice, "When is the launch?", "same-key"), 4
        )

        self.assertEqual(errors, [])
        self.assertEqual(len({ask.pk for ask, _ in results}), 1)
        self.assertEqual(sorted(created for _, created in results), [False, False, False, True])

    def test_without_the_lock_both_would_pass(self):
        """The failure the lock prevents, forced: this test's teeth.

        With the lock replaced by a plain read, and both threads held after
        counting until both have counted, each sees one ask left and both
        create one. (With the real lock this interleaving cannot happen --
        the second thread waits at the lock, not at the barrier.)
        """
        record_usage(
            AskQuery.objects.create(user=self.alice, question="earlier", idempotency_key="earlier")
        )
        counted = threading.Barrier(2)
        real_used = limits._used

        def used_then_wait(key, start, end, user=None):
            result = real_used(key, start, end, user=user)
            # Only the per-user count: the system count runs under its own
            # advisory lock (limits, D98), where a barrier would deadlock.
            if user is not None:
                counted.wait(timeout=10)
            return result

        User = get_user_model()
        with (
            mock.patch.object(services, "_lock_user", lambda user: User.objects.get(pk=user.pk)),
            mock.patch.object(limits, "_used", used_then_wait),
        ):
            results, errors = self._run_in_threads(
                lambda i: create_ask(self.alice, f"Question {i}?", f"key-{i}"), 2
            )

        self.assertEqual(errors, [])
        self.assertEqual(AskQuery.objects.filter(user=self.alice).count(), 3)  # over the limit of 2


@override_settings(LIMIT_DEFAULTS=chat_turns(10, 10))
class ConcurrentTurnTests(TransactionTestCase):
    """Turns are sequential, even when two arrive at once (DECISIONS D141)."""

    def setUp(self):
        self.alice = make_user("alice")
        self.conversation = Conversation.objects.create(user=self.alice)
        patcher = mock.patch.object(answer_ask, "delay")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _race(self, count, call):
        """Run ``call(index)`` in ``count`` threads released together; their results."""
        barrier = threading.Barrier(count)
        results = [None] * count

        def run(index):
            try:
                barrier.wait(timeout=10)
                results[index] = call(index)
            except Exception as exc:
                results[index] = exc
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return results

    def _post_turn(self, index):
        client = APIClient()
        client.force_authenticate(self.alice)
        return client.post(
            f"/api/v1/conversations/{self.conversation.pk}/turns/",
            {"question": f"Question {index}?"},
            format="json",
            headers={"Idempotency-Key": f"key-{index}"},
        )

    def test_two_turns_at_once_are_one_202_and_one_409(self):
        responses = self._race(2, self._post_turn)

        codes = sorted(getattr(r, "status_code", 0) for r in responses)
        self.assertEqual(codes, [202, 409], responses)
        accepted = next(r for r in responses if r.status_code == 202)
        refused = next(r for r in responses if r.status_code == 409)
        self.assertEqual(refused.json()["code"], "turn_in_progress")
        self.assertEqual(refused.json()["turn"], accepted.json()["id"])
        self.assertEqual(AskQuery.objects.get().position, 1)
        self.assertEqual(UsageEvent.objects.count(), 1)

    def test_many_turns_at_once_make_exactly_one(self):
        results = self._race(
            6, lambda i: create_turn(self.alice, self.conversation, f"Question {i}?", f"key-{i}")
        )

        made = [r for r in results if isinstance(r, tuple)]
        refused = [r for r in results if isinstance(r, TurnInProgress)]
        self.assertEqual((len(made), len(refused)), (1, 5), results)
        self.assertEqual(AskQuery.objects.count(), 1)

    def test_without_the_lock_both_would_pass_the_check(self):
        """The interleaving the lock prevents, forced: this test's teeth.

        With the lock replaced by a plain read, and both threads held after
        looking for an unfinished turn, each finds none and both insert
        position 1. Only the unique constraint is left to stop the second,
        as an IntegrityError -- a 500, not a 409. (With the real lock this
        cannot happen: the second thread waits at the lock, not here.)
        """
        looked = threading.Barrier(2)
        real_unfinished = services._unfinished_turn

        def unfinished_then_wait(conversation):
            result = real_unfinished(conversation)
            looked.wait(timeout=10)
            return result

        User = get_user_model()
        with (
            mock.patch.object(services, "_lock_user", lambda user: User.objects.get(pk=user.pk)),
            mock.patch.object(services, "_unfinished_turn", unfinished_then_wait),
        ):
            results = self._race(
                2,
                lambda i: create_turn(self.alice, self.conversation, f"Question {i}?", f"key-{i}"),
            )

        self.assertFalse(any(isinstance(r, TurnInProgress) for r in results), results)
        self.assertEqual(sum(isinstance(r, IntegrityError) for r in results), 1, results)
        self.assertEqual(AskQuery.objects.count(), 1)

    def test_a_turn_racing_a_delete_never_lands_in_a_deleted_conversation(self):
        def turn_or_delete(index):
            if index == 0:
                return create_turn(self.alice, self.conversation, "Question?", "key")
            return delete_conversation(self.alice, self.conversation.pk)

        results = self._race(2, turn_or_delete)

        self.conversation.refresh_from_db()
        self.assertIsNotNone(self.conversation.deleted_at)
        if isinstance(results[0], ConversationNotFound):
            self.assertFalse(AskQuery.objects.exists())
        else:
            # The turn committed first; the delete then hid it with the rest.
            self.assertEqual(AskQuery.objects.get().conversation, self.conversation)
