"""Small limits for tests, and consume() the way a feature calls it."""

import logging

from django.contrib.auth import get_user_model
from django.db import transaction

from limits import service

TEST_LIMITS = {
    "chat_turns": {"user_free": 2, "user_premium": 5, "system": 100, "period": "month"},
    "daily": {"user_free": 1, "user_premium": 1, "period": "day"},
    "bytes": {"user_free": 100, "user_premium": 100, "system": 150, "period": "total"},
    "signups": {"system": 3, "period": "day"},
    "open": {"period": "month"},
}


def consume_as(user, key, amount=1, **meta):
    """Lock the user's row, then consume: the contract consume() documents."""
    with transaction.atomic():
        if user is not None:
            user = get_user_model().objects.select_for_update().get(pk=user.pk)
        return service.consume(user, key, amount, **meta)


class QuietSystemLimitLog:
    """Silence the warning each system refusal logs; the tests expect them."""

    def setUp(self):
        super().setUp()
        logger = logging.getLogger("limits.service")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.ERROR)
