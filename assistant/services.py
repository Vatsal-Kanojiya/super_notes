"""Creating an ask: idempotency, the quota, and the task. Nothing else makes an AskQuery.

In one transaction, holding the user's row lock -- the pattern of
notes/services.py (DECISIONS D5), for the same reason: a check followed by
a write is only safe if nobody else can check in between. Without the lock
two asks at the quota edge both count ``limit - 1`` and both pass; with it
the second waits, then counts the first.

The idempotency lookup comes first, under the same lock: a retried POST
gets back the ask it already made -- counted once, and returned even if the
month has filled up since -- and two requests racing with one key cannot
both miss it and then trip the unique constraint.
"""

from datetime import datetime

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from . import quota
from .models import AskQuery

User = get_user_model()


class QuotaExceeded(Exception):
    """The month's asks are used up. The API answers 429 with these fields."""

    def __init__(self, used: int, limit: int, resets_at: datetime):
        super().__init__(f"{used} of {limit} asks used; resets {resets_at.isoformat()}.")
        self.used, self.limit, self.resets_at = used, limit, resets_at


class IdempotencyKeyReused(Exception):
    """The key already made an ask with a different question (DECISIONS D75).

    Answering with the old ask would show the user an answer to a question
    they did not ask; making a new one would break the key's promise. A key
    names one request, so this is the client's bug, reported as such.
    """

    def __init__(self, existing: AskQuery):
        super().__init__(f"Idempotency key already used for ask {existing.pk}.")
        self.existing = existing


def _lock_user(user):
    """Lock the user's row until the transaction ends. Returns a fresh copy.

    Fresh, so the plan the quota is read from is the committed one.
    """
    return User.objects.select_for_update().only("pk", "plan").get(pk=user.pk)


@transaction.atomic
def create_ask(user, question: str, idempotency_key: str) -> tuple[AskQuery, bool]:
    """(ask, created). Raises QuotaExceeded or IdempotencyKeyReused.

    A new ask is enqueued on commit, so the worker never looks for a row
    that is not visible yet, and a rolled-back create is never answered.
    """
    question = question.strip()
    locked = _lock_user(user)

    existing = AskQuery.objects.filter(user=locked, idempotency_key=idempotency_key).first()
    if existing is not None:
        if existing.question != question:
            raise IdempotencyKeyReused(existing)
        return existing, False

    now = timezone.now()
    usage = quota.usage(locked, now)
    if usage["used"] >= usage["limit"]:
        raise QuotaExceeded(**usage)

    ask = AskQuery.objects.create(user=locked, question=question, idempotency_key=idempotency_key)

    # Imported here: the task module imports retrieval and the chat stack,
    # none of which creating a row needs.
    from .tasks import answer_ask

    ask_id = ask.pk
    transaction.on_commit(lambda: answer_ask.delay(ask_id))
    return ask, True
