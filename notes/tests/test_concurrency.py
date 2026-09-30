"""Concurrent writes by one user, proved rather than asserted.

TransactionTestCase, not TestCase, as in the reference's
test_concurrency.py: TestCase wraps each test in one transaction that is
rolled back, so a second thread could never see the first thread's commit
-- the very thing under test. Each thread closes its own connection, or the
test database cannot be dropped afterwards.
"""

import threading
from unittest import mock

from django.db import connections
from django.test import TransactionTestCase

from notes import services
from notes.models import Note

from .helpers import doc, make_user


class ConcurrentWriteTests(TransactionTestCase):
    def setUp(self):
        self.alice = make_user("alice")

    def _run_in_threads(self, target, count):
        """Start ``count`` threads at once and wait for them all."""
        barrier = threading.Barrier(count)
        errors = []

        def run(index):
            try:
                barrier.wait(timeout=10)
                target(index)
            except Exception as exc:  # surfaced below; a thread's own traceback is lost
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return errors

    def test_concurrent_creates_get_distinct_consecutive_revisions(self):
        """Without the lock, two transactions read notes_revision = n together
        and both stamp n + 1: sync would then send one of them and never the
        other. With it, the second waits for the first to commit.
        """
        count = 8
        errors = self._run_in_threads(
            lambda i: services.create_note(self.alice, title=str(i), content=doc("x")), count
        )

        self.assertEqual(errors, [])
        revisions = sorted(Note.objects.filter(owner=self.alice).values_list("revision", flat=True))
        self.assertEqual(revisions, list(range(1, count + 1)))
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.notes_revision, count)

    def test_revision_order_is_commit_order(self):
        """Strictly ordered, not just distinct: a later commit never carries a
        lower revision.

        Each write records its revision from _after_write, which runs while
        its transaction still holds the owner's lock -- so the order of the
        records is the order the writes held the lock, which is the order
        they committed. (Recording after create_note returns would race:
        a thread can be descheduled between its commit and its append.)
        """
        order = []

        with mock.patch.object(services, "_after_write", lambda note: order.append(note.revision)):
            errors = self._run_in_threads(
                lambda i: services.create_note(self.alice, title=str(i)), 6
            )

        self.assertEqual(errors, [])
        self.assertEqual(order, list(range(1, 7)))

    def test_two_edits_from_the_same_version_one_wins_one_conflicts(self):
        note = services.create_note(self.alice, title="start")
        outcomes = [None, None]

        def edit(index):
            try:
                services.update_note(
                    self.alice, note.pk, expected_version=1, title=f"device {index}"
                )
                outcomes[index] = "saved"
            except services.VersionConflict:
                outcomes[index] = "conflict"

        errors = self._run_in_threads(edit, 2)

        self.assertEqual(errors, [])
        self.assertEqual(sorted(outcomes), ["conflict", "saved"])
        note.refresh_from_db()
        self.assertEqual((note.version, note.revision), (2, 2))
