"""The daily cap on new accounts: the ``signups`` system limit (DECISIONS D131)."""

import logging
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError
from django.test import TestCase

from accounts import google, ratelimit
from accounts.models import SecurityEvent
from limits.models import Limit, UsageEvent

from .test_api_auth import AuthApiTestCase, with_cache, with_google

User = get_user_model()


def verified(sub="111", email="alice@example.com"):
    return {"sub": sub, "email": email, "name": "", "picture": ""}


def close_after(n):
    """Allow ``n`` new accounts a day."""
    return Limit.objects.create(key="signups", system=n, period="day")


class QuietLimits:
    def setUp(self):
        super().setUp()
        logger = logging.getLogger("limits.service")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.ERROR)


class FindOrCreateSignupTests(QuietLimits, TestCase):
    def test_a_new_account_consumes_one_signup(self):
        user, created = google.find_or_create_user(verified())

        self.assertTrue(created)
        event = UsageEvent.objects.get()
        self.assertEqual((event.key, event.user, event.amount), ("signups", None, 1))

    def test_full_refuses_a_new_account_and_creates_nothing(self):
        close_after(1)
        google.find_or_create_user(verified())

        with self.assertRaises(google.SignupsClosed) as caught:
            google.find_or_create_user(verified(sub="222", email="bob@example.com"))

        self.assertEqual(caught.exception.reason, "signups_closed")
        self.assertIsInstance(caught.exception, google.GoogleSignInError)
        self.assertEqual(User.objects.count(), 1)

    def test_full_still_signs_existing_accounts_in(self):
        close_after(1)
        first, _ = google.find_or_create_user(verified())
        User.objects.create_user("carol@example.com")  # made in the admin, no sub yet

        again, created = google.find_or_create_user(verified())
        linked, linked_created = google.find_or_create_user(
            verified(sub="333", email="carol@example.com")
        )

        self.assertEqual((again.pk, created, linked_created), (first.pk, False, False))
        self.assertEqual(UsageEvent.objects.count(), 1)

    def test_the_last_place_taken_by_the_same_person_signs_them_in(self):
        """A double tap at the day's last place: the loser is the winner's own account."""
        close_after(1)
        winner, _ = google.find_or_create_user(verified())
        # This request looked before the winner inserted, so it saw no account.
        with mock.patch.object(User.objects, "filter", wraps=User.objects.filter) as filt:
            filt.side_effect = [User.objects.none(), User.objects.none(), User.objects.all()]
            user, created = google.find_or_create_user(verified())

        self.assertEqual((user.pk, created), (winner.pk, False))

    def test_a_failed_insert_hands_its_signup_back(self):
        with (
            mock.patch.object(User.objects, "create_user", side_effect=IntegrityError),
            self.assertRaises(google.GoogleSignInError),
        ):
            google.find_or_create_user(verified())

        self.assertFalse(UsageEvent.objects.exists())

    def test_the_default_is_thirty_a_day(self):
        for i in range(30):
            google.find_or_create_user(verified(sub=str(i), email=f"u{i}@example.com"))
        with self.assertRaises(google.SignupsClosed):
            google.find_or_create_user(verified(sub="x", email="x@example.com"))


@with_google
@with_cache
class SignupsClosedApiTests(QuietLimits, AuthApiTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def test_a_new_account_when_full_is_a_403_signups_closed(self):
        close_after(0)

        response = self.google()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {
                "detail": "New accounts are closed for today. Try again tomorrow.",
                "code": "signups_closed",
            },
        )
        self.assertFalse(User.objects.exists())
        event = SecurityEvent.objects.get()
        self.assertEqual(
            (event.event, event.detail), ("google_login_failed", {"reason": "signups_closed"})
        )

    def test_an_existing_account_signs_in_when_full(self):
        self.sign_in()
        close_after(1)

        self.assertEqual(self.google().status_code, 200)

    def test_a_closed_signup_is_not_a_failed_attempt(self):
        close_after(0)
        with mock.patch.object(ratelimit, "GOOGLE_LOGIN_IP_LIMIT", 1):
            for _ in range(3):
                self.assertEqual(self.google().status_code, 403)
        self.assertFalse(SecurityEvent.objects.filter(event="login_blocked").exists())
