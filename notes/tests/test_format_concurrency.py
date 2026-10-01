"""Format jobs racing at the limit edge, proved rather than asserted.

TransactionTestCase, as in assistant/tests/test_concurrency.py: inside
TestCase's one rolled-back transaction a second thread could never see the
first's commit -- the very thing under test.
"""

import threading
from unittest import mock

from django.db import connections
from django.test import TransactionTestCase, override_settings

from limits import service as limits
from limits.models import UsageEvent
from notes import services
from notes.format_service import create_format_job
from notes.models import FormatJob
from notes.tasks import format_note

from .helpers import doc, format_limit, make_user


@override_settings(LIMIT_DEFAULTS=format_limit(2, 10))
class ConcurrentFormatTests(TransactionTestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.note = services.create_note(self.alice, content=doc("buy milk", "call mum"))
        # Only the creates race here; running them is another test's job.
        patcher = mock.patch.object(format_note, "delay")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_in_threads(self, target, count):
        barrier = threading.Barrier(count)
        results = [None] * count

        def run(index):
            try:
                barrier.wait(timeout=10)
                results[index] = target(index)
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

    def test_parallel_jobs_at_the_edge_cannot_both_pass(self):
        create_format_job(self.alice, self.note.pk, "earlier")

        results = self._run_in_threads(
            lambda i: create_format_job(self.alice, self.note.pk, f"race-{i}"), 4
        )

        passed = [r for r in results if isinstance(r, tuple)]
        refused = [r for r in results if isinstance(r, limits.UserLimitExceeded)]
        self.assertEqual((len(passed), len(refused)), (1, 3), results)
        self.assertEqual(FormatJob.objects.count(), 2)
        self.assertEqual(UsageEvent.objects.filter(key="format", refunded=False).count(), 2)

    def test_parallel_requests_with_one_key_make_one_job(self):
        results = self._run_in_threads(
            lambda i: create_format_job(self.alice, self.note.pk, "same-key"), 4
        )

        self.assertTrue(all(isinstance(r, tuple) for r in results), results)
        self.assertEqual(sorted(created for _, created in results), [False, False, False, True])
        self.assertEqual(FormatJob.objects.count(), 1)
        self.assertEqual(UsageEvent.objects.count(), 1)
