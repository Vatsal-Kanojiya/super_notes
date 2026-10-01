"""consume() racing at the user edge and the system edge, proved rather than asserted.

TransactionTestCase, as in assistant/tests/test_concurrency.py: inside
TestCase's one rolled-back transaction a second thread could never see the
first's commit -- the very thing under test.
"""

import threading
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.db import connections, transaction
from django.test import TransactionTestCase, override_settings

from limits import service
from limits.models import UsageEvent
from limits.service import SystemLimitExceeded, UserLimitExceeded
from notes.tests.helpers import make_user

from .helpers import TEST_LIMITS, QuietSystemLimitLog, consume_as

REFUSALS = (UserLimitExceeded, SystemLimitExceeded)


def run_in_threads(target, count):
    """Start ``count`` threads at once, wait for them all; (results, unexpected errors)."""
    barrier = threading.Barrier(count)
    results, errors = [None] * count, []

    def run(index):
        try:
            barrier.wait(timeout=10)
            results[index] = target(index)
        except Exception as exc:
            results[index] = exc
            if not isinstance(exc, REFUSALS):
                errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results, errors


@override_settings(
    LIMIT_DEFAULTS=TEST_LIMITS,
    ADMINS=[("Admin", "admin@example.com")],
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class ConcurrentConsumeTests(QuietSystemLimitLog, TransactionTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def test_parallel_consumes_at_the_user_edge(self):
        alice = make_user("alice")
        consume_as(alice, "chat_turns")  # one of two left

        results, errors = run_in_threads(lambda i: consume_as(alice, "chat_turns"), 6)

        self.assertEqual(errors, [])
        self.assertEqual(sum(isinstance(r, UsageEvent) for r in results), 1)
        self.assertEqual(UsageEvent.objects.filter(user=alice).count(), 2)

    def test_parallel_users_at_the_system_edge(self):
        """Eight users, each under their own limit and their own row lock; room for three."""
        users = [make_user(f"user{i}") for i in range(8)]

        results, errors = run_in_threads(lambda i: consume_as(users[i], "bytes", 50), 8)

        self.assertEqual(errors, [])
        self.assertEqual(sum(isinstance(r, UsageEvent) for r in results), 3)
        self.assertEqual(sum(isinstance(r, SystemLimitExceeded) for r in results), 5)
        self.assertEqual(service.system_usage("bytes")["used"], 150)
        self.assertEqual(len(mail.outbox), 1)  # five refusals, one mail

    def test_without_the_system_lock_both_would_pass(self):
        """The failure the advisory lock prevents, forced: this test's teeth.

        With the lock a no-op and both threads held after counting until both
        have counted, each sees one sign-up left and both record one. (With
        the real lock the second thread waits at the lock, not the barrier.)
        """
        consume_as(None, "signups")
        consume_as(None, "signups")  # one of three left
        counted = threading.Barrier(2)
        real_used = service._used

        def used_then_wait(*args, **kwargs):
            result = real_used(*args, **kwargs)
            counted.wait(timeout=10)
            return result

        with (
            mock.patch.object(service, "_lock_system", lambda key: None),
            mock.patch.object(service, "_used", used_then_wait),
        ):
            results, errors = run_in_threads(lambda i: consume_as(None, "signups"), 2)

        self.assertEqual(errors, [])
        self.assertEqual(service.system_usage("signups")["used"], 4)  # over the limit of 3

    def test_with_the_system_lock_the_same_interleaving_cannot_happen(self):
        """The control for the test above: only the lock differs."""
        consume_as(None, "signups")
        consume_as(None, "signups")

        results, errors = run_in_threads(lambda i: consume_as(None, "signups"), 2)

        self.assertEqual(errors, [])
        self.assertEqual(service.system_usage("signups")["used"], 3)

    def test_consume_outside_a_transaction_is_refused(self):
        alice = make_user("alice")
        self.assertFalse(transaction.get_connection().in_atomic_block)

        with self.assertRaises(RuntimeError):
            service.consume(alice, "chat_turns")
        self.assertFalse(UsageEvent.objects.exists())
