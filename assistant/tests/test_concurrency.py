"""Asks racing at the quota edge, proved rather than asserted.

TransactionTestCase, as in notes/tests/test_concurrency.py: inside TestCase's
one rolled-back transaction a second thread could never see the first's
commit -- the very thing under test.
"""

import threading
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connections
from django.test import TransactionTestCase, override_settings

from assistant import services
from assistant.models import AskQuery
from assistant.services import QuotaExceeded, create_ask
from assistant.tasks import answer_ask
from limits import service as limits
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
