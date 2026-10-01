"""Creating an ask or a turn: idempotency, the quota, and the task. Nothing else makes an AskQuery.

In one transaction, holding the user's row lock -- the pattern of
notes/services.py (DECISIONS D5), for the same reason: a check followed by
a write is only safe if nobody else can check in between. Without the lock
two asks at the quota edge both count ``limit - 1`` and both pass; with it
the second waits, then counts the first.

The idempotency lookup comes first, under the same lock: a retried POST
gets back the ask it already made -- counted once, and returned even if the
month has filled up since -- and two requests racing with one key cannot
both miss it and then trip the unique constraint.

The quota is the ``chat_turns`` limit of limits/ (DECISIONS D84, D101): a
new ask consumes one, linked to the ask, and a failed ask is refunded
(assistant/tasks.py). The system-wide limit raises SystemLimitExceeded
through this function untouched.

A conversation turn is an ask with a place in its conversation
(create_turn, DECISIONS D140-D141). It goes through the same lock, the
same idempotency lookup and the same limit, and adds one rule under that
lock: turns are sequential, so a new one is refused while the previous
one is still pending or running. Every ask and turn of a user takes the
same row lock, so two turns racing on one conversation run one after the
other, and the second sees the first. A previous turn still *pending*
after TURN_PENDING_STALE_SECONDS -- its message lost, no worker ever
claimed it -- does not block: it is failed and refunded under the same
lock, and the new turn proceeds (DECISIONS D505).
"""

from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from limits import service as limits

from . import quota
from .models import DERIVED_TITLE_CHARS, AskQuery, Conversation

User = get_user_model()


class QuotaExceeded(Exception):
    """The month's asks are used up. The API answers 429 with these fields."""

    def __init__(self, used: int, limit: int, resets_at: datetime | None):
        when = resets_at.isoformat() if resets_at else "never"
        super().__init__(f"{used} of {limit} asks used; resets {when}.")
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


class ConversationNotFound(Exception):
    """No such conversation of this user's, or it was deleted. The API answers 404."""


class TurnInProgress(Exception):
    """The conversation's previous turn is still pending or running. The API answers 409.

    Turns are sequential: a follow-up is read in the light of the answer
    before it, which does not exist yet.
    """

    def __init__(self, turn: AskQuery):
        super().__init__(f"Turn {turn.pk} is still {turn.status}.")
        self.turn = turn


UNFINISHED = (AskQuery.Status.PENDING, AskQuery.Status.RUNNING)


def _lock_user(user):
    """Lock the user's row until the transaction ends. Returns a fresh copy.

    Fresh, so the plan the quota is read from is the committed one.
    """
    return User.objects.select_for_update().only("pk", "plan").get(pk=user.pk)


@transaction.atomic
def create_ask(user, question: str, idempotency_key: str) -> tuple[AskQuery, bool]:
    """(ask, created). Raises QuotaExceeded, IdempotencyKeyReused or SystemLimitExceeded.

    A new ask is enqueued on commit, so the worker never looks for a row
    that is not visible yet, and a rolled-back create is never answered.
    """
    question = question.strip()
    locked = _lock_user(user)

    existing = _replay(locked, idempotency_key, question, conversation=None)
    if existing is not None:
        return existing, False

    return _create(locked, question, idempotency_key), True


@transaction.atomic
def create_turn(
    user, conversation: Conversation | int, question: str, idempotency_key: str
) -> tuple[AskQuery, bool]:
    """(turn, created): the next turn of one of the user's conversations.

    Raises ConversationNotFound, TurnInProgress, QuotaExceeded,
    IdempotencyKeyReused or SystemLimitExceeded. The order is the point:
    the conversation is read under the lock (a delete that committed first
    wins); a replayed key returns its turn before anything else is asked of
    it, so a retry of the turn now running is that turn, not a 409; only a
    new turn meets the sequential rule and then the quota.
    """
    question = question.strip()
    locked = _lock_user(user)
    conversation = _live_conversation(locked, getattr(conversation, "pk", conversation))

    existing = _replay(locked, idempotency_key, question, conversation=conversation)
    if existing is not None:
        return existing, False

    previous = _unfinished_turn(conversation)
    if previous is not None and not _fail_if_lost(previous):
        raise TurnInProgress(previous)

    return _create(locked, question, idempotency_key, conversation=conversation), True


@transaction.atomic
def create_conversation(
    user, question: str | None = None, idempotency_key: str | None = None
) -> tuple[Conversation, bool]:
    """(conversation, created), optionally with its first turn asked.

    Without a question it only makes an empty conversation (no quota, no
    key). With one, the key replays as create_turn's does: a retry returns
    the conversation its first turn made, counted once. A key that made
    anything else -- a plain ask, a later turn, or a turn of a conversation
    since deleted -- is IdempotencyKeyReused (DECISIONS D143).
    """
    question = question.strip() if question is not None else None
    locked = _lock_user(user)

    if not question:
        return Conversation.objects.create(user=locked), True
    if not idempotency_key:
        raise ValueError("A first question needs an idempotency key.")

    existing = AskQuery.objects.filter(user=locked, idempotency_key=idempotency_key).first()
    if existing is not None:
        conversation = existing.conversation
        if (
            existing.question != question
            or conversation is None
            or existing.position != 1
            or conversation.deleted_at is not None
        ):
            raise IdempotencyKeyReused(existing)
        return conversation, False

    conversation = Conversation.objects.create(user=locked)
    _create(locked, question, idempotency_key, conversation=conversation)
    conversation.refresh_from_db()
    return conversation, True


@transaction.atomic
def rename_conversation(user, conversation_id: int, title: str) -> Conversation:
    """Set the title. Raises ConversationNotFound."""
    locked = _lock_user(user)
    conversation = _live_conversation(locked, conversation_id)
    # update(), not save(): auto_now would move updated_at, and the list's
    # order is the last turn, not the last rename (DECISIONS D144).
    conversation.title = title.strip()
    Conversation.objects.filter(pk=conversation.pk).update(title=conversation.title)
    return conversation


@transaction.atomic
def delete_conversation(user, conversation_id: int) -> None:
    """Soft-delete: the turns are hidden, the usage they consumed stays counted.

    Under the user lock, so a turn being created at the same moment either
    commits first (and is hidden with the rest) or finds the conversation
    gone. A turn already running finishes as usual, unseen.
    """
    locked = _lock_user(user)
    conversation = _live_conversation(locked, conversation_id)
    Conversation.objects.filter(pk=conversation.pk).update(deleted_at=timezone.now())


def derive_title(question: str) -> str:
    """The first question, on one line, cut at a word near DERIVED_TITLE_CHARS."""
    text = " ".join(question.split())
    if len(text) <= DERIVED_TITLE_CHARS:
        return text
    cut = text[:DERIVED_TITLE_CHARS].rsplit(" ", 1)[0] or text[:DERIVED_TITLE_CHARS]
    return cut.rstrip(" ,.;:-") + "…"


# --- Under the user lock ------------------------------------------------------


def _live_conversation(locked_user, conversation_id) -> Conversation:
    conversation = Conversation.objects.filter(
        pk=conversation_id, user=locked_user, deleted_at__isnull=True
    ).first()
    if conversation is None:
        raise ConversationNotFound(conversation_id)
    return conversation


def _replay(locked_user, idempotency_key, question, conversation) -> AskQuery | None:
    """The ask this key already made, if it was this same request; else None.

    "Same request" is the same question to the same place: a key used for
    a plain ask does not replay as a turn, nor a turn of one conversation
    as a turn of another (DECISIONS D142).
    """
    existing = AskQuery.objects.filter(user=locked_user, idempotency_key=idempotency_key).first()
    if existing is None:
        return None
    conversation_id = conversation.pk if conversation is not None else None
    if existing.question != question or existing.conversation_id != conversation_id:
        raise IdempotencyKeyReused(existing)
    return existing


def _unfinished_turn(conversation) -> AskQuery | None:
    """The conversation's turn still pending or running, if any.

    Read under the user lock, which every turn creation takes: no other
    turn of this conversation can be made between this read and the
    insert that follows it.
    """
    return conversation.turns.filter(status__in=UNFINISHED).order_by("-position").first()


# What the user reads on a turn failed as lost (D505): the sweeper's words.
LOST = "This took too long. Please ask again."


def _fail_if_lost(turn: AskQuery) -> bool:
    """Fail and refund ``turn`` if it is a lost message; True if it did (DECISIONS D505).

    Lost: still pending -- no worker ever claimed it -- more than
    TURN_PENDING_STALE_SECONDS after it was made. The UPDATE checks both
    again, so a worker that claims the turn first wins (it is then
    running, and blocks as usual), and a worker that comes later finds it
    failed and leaves it alone (answer_ask claims only unfinished asks).
    The refund and the ``failed`` event go with this transaction, as the
    sweeper's do.
    """
    cutoff = timezone.now() - timedelta(seconds=settings.TURN_PENDING_STALE_SECONDS)
    if turn.status != AskQuery.Status.PENDING or turn.created_at >= cutoff:
        return False
    failed = AskQuery.objects.filter(
        pk=turn.pk, status=AskQuery.Status.PENDING, created_at__lt=cutoff
    ).update(
        status=AskQuery.Status.FAILED, completed_at=timezone.now(), error=LOST, partial_answer=""
    )
    if not failed:
        return False
    limits.refund_where(ask_id=turn.pk, key=quota.KEY)

    from .tasks import _announce

    _announce(turn.pk, None)
    return True


def _create(locked_user, question, idempotency_key, conversation=None) -> AskQuery:
    """Make the ask (or turn), consume its use, enqueue it on commit."""
    fields = {}
    if conversation is not None:
        last = conversation.turns.aggregate(last=Max("position"))["last"] or 0
        fields = {"conversation": conversation, "position": last + 1}

    # Made before the check so the event can point at it; a refusal raises
    # out of this transaction and takes the row with it.
    ask = AskQuery.objects.create(
        user=locked_user, question=question, idempotency_key=idempotency_key, **fields
    )
    try:
        limits.consume(locked_user, quota.KEY, ask=ask)
    except limits.UserLimitExceeded as exceeded:
        raise QuotaExceeded(exceeded.used, exceeded.limit, exceeded.resets_at) from exceeded

    if conversation is not None:
        # The list is most recently active first; the first question names
        # a conversation nobody has named yet. update(), so a concurrent
        # rename is not overwritten with a stale copy (renames take the
        # same lock anyway).
        changes = {"updated_at": timezone.now()}
        if not conversation.title:
            changes["title"] = derive_title(question)
        Conversation.objects.filter(pk=conversation.pk).update(**changes)

    # Imported here: the task module imports retrieval and the chat stack,
    # none of which creating a row needs.
    from .tasks import answer_ask

    ask_id = ask.pk
    transaction.on_commit(lambda: answer_ask.delay(ask_id))
    return ask
