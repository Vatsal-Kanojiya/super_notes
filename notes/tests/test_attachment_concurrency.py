"""Uploads racing at the storage limit, proved rather than asserted.

TransactionTestCase, as in test_format_concurrency.py: inside TestCase's one
rolled-back transaction a second thread could never see the first's commit.

The guard: ``_used`` (the limits layer's count) is slowed down, so without
the owner's row lock every racing upload would count the same total, all
fit, and the first test would fail. With the lock they count one at a time.
"""

import os
import threading
import time
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections
from django.test import TransactionTestCase, override_settings

from limits import service as limits
from limits.models import UsageEvent
from notes import services
from notes.attachments import sha256_of
from notes.models import Attachment

from .helpers import isolate_attachment_storage, make_user
from .test_attachments import PDF, storage_limit

RACERS = 4


def pdf(index):
    """A distinct PDF of the same size per index."""
    return SimpleUploadedFile(f"r{index}.pdf", PDF + bytes([index]), "application/pdf")


def add(owner, note_id, index, file=None):
    file = file or pdf(index)
    return services.add_attachment(
        owner,
        note_id,
        file,
        original_name=f"r{index}.pdf",
        mime_type="application/pdf",
        sha256=sha256_of(file),
    )


@override_settings(LIMIT_DEFAULTS=storage_limit(len(PDF) + 1))
class ConcurrentUploadTests(TransactionTestCase):
    def setUp(self):
        self.location = isolate_attachment_storage(self)
        self.alice = make_user("alice")
        self.note = services.create_note(self.alice, title="Car service")

    def _run_in_threads(self, target):
        barrier = threading.Barrier(RACERS)
        results = [None] * RACERS

        def run(index):
            try:
                barrier.wait(timeout=10)
                results[index] = target(index)
            except Exception as exc:
                results[index] = exc
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run, args=(i,)) for i in range(RACERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return results

    def _files(self):
        return [name for _, _, names in os.walk(self.location) for name in names]

    def test_parallel_uploads_at_the_storage_edge_cannot_both_fit(self):
        real_used = limits._used

        def slow_used(*args, **kwargs):
            total = real_used(*args, **kwargs)
            # Wide open window between counting and recording.
            time.sleep(0.2)
            return total

        with mock.patch.object(limits, "_used", side_effect=slow_used):
            results = self._run_in_threads(lambda i: add(self.alice, self.note.pk, i))

        passed = [r for r in results if isinstance(r, tuple)]
        refused = [r for r in results if isinstance(r, limits.UserLimitExceeded)]
        self.assertEqual((len(passed), len(refused)), (1, RACERS - 1), results)
        self.assertEqual(Attachment.objects.count(), 1)
        self.assertEqual(UsageEvent.objects.filter(key="storage_bytes").count(), 1)
        # The refused uploads' bytes were stored before the lock, and deleted.
        self.assertEqual(len(self._files()), 1)

    def test_parallel_uploads_of_one_file_make_one_attachment(self):
        # The limit's size exactly: it fits once.
        content = PDF + b"s"

        results = self._run_in_threads(
            lambda i: add(self.alice, self.note.pk, i, SimpleUploadedFile("s.pdf", content))
        )

        self.assertTrue(all(isinstance(r, tuple) for r in results), results)
        self.assertEqual(sorted(created for _, created in results), [False, False, False, True])
        self.assertEqual(len({attachment.pk for attachment, _ in results}), 1)
        self.assertEqual(UsageEvent.objects.filter(key="storage_bytes").count(), 1)
        self.assertEqual(len(self._files()), 1)
