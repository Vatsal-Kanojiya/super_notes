"""Answering an ask: retrieve, floor, prompt, complete, cite (plan §6.5, docs/RAG.md).

A conversation turn adds two steps (assistant/conversation.py): its
follow-up is condensed to stand alone before retrieval, and its prompt
(prompts/chat.md) carries the conversation so far. Finishing a turn may
queue a fold of the conversation's oldest turns into its summary
(fold_history, below), and queues the extraction of what the user said
about themselves (extract_memory, assistant/memory.py).

The task owns the AskQuery's life after creation: pending → running → done
or failed. Every way out of it ends in done or failed, including retries
running out, the soft time limit and bugs (DECISIONS D76): a row left
"running" would poll forever and, never failing, count against the quota.
Failing an ask refunds its ``chat_turns`` use in the same transaction
(DECISIONS D102), here and in the sweeper.

The answer call is streamed (DECISIONS D363-D366): each piece is published
on Redis as it arrives (assistant/events.py), the text so far is saved on
the row every ASK_PARTIAL_SAVE_SECONDS, and the end is published once it
is committed. None of that can fail an answer, and polling is unchanged.
"""

import logging
from datetime import timedelta
from time import monotonic

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from limits import service as limits
from retrieval.embeddings import EmbeddingError, EmbeddingTransientError
from retrieval.search import SearchHit, search

from . import chat, conversation, events, memory, quota
from .citations import parse_citations
from .models import AskQuery
from .prompt import Excerpt, build_messages, fit_excerpts, prompt_version

logger = logging.getLogger(__name__)

TRANSIENT = (chat.TransientChatError, EmbeddingTransientError)

# What the user reads on a failed ask. Never the provider's own message,
# which can name keys, models or quotas; that goes to the log.
CHAT_FAILED = "The assistant couldn't answer this question. Try again, or rephrase it."
SEARCH_FAILED = "Your notes couldn't be searched just now. Try again in a few minutes."
BUSY = "The assistant is busy right now. Try again in a few minutes."
UNEXPECTED = "Something went wrong answering this question. Try again."

UNFINISHED = (AskQuery.Status.PENDING, AskQuery.Status.RUNNING)


@shared_task(
    bind=True,
    # A rate limit or an outage is worth waiting out, briefly: someone is
    # watching a spinner. Four retries with jittered backoff span about a
    # minute before the ask is failed.
    autoretry_for=TRANSIENT,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=4,
)
def answer_ask(self, ask_id: int) -> None:
    """Answer one AskQuery, and make sure it ends done or failed (DECISIONS D76).

    Expected failures (the provider refuses, search is down) are handled
    where they happen. This catches the rest: a transient error on the last
    attempt, which autoretry would only re-raise, and anything unexpected --
    the soft time limit, a bug. Handled here rather than in on_failure so
    it runs, and is tested, the same with and without a worker.
    """
    publisher = events.Publisher(ask_id)
    try:
        _answer(ask_id, publisher)
    except TRANSIENT as exc:
        if self.request.retries < self.max_retries:
            raise  # autoretry_for schedules the next attempt.
        logger.error("Ask %s: giving up after %s retries: %r", ask_id, self.max_retries, exc)
        _fail(ask_id, BUSY, publisher)
    except Exception:
        logger.exception("Ask %s failed unexpectedly", ask_id)
        _fail(ask_id, UNEXPECTED, publisher)
        raise


def _answer(ask_id: int, publisher: events.Publisher | None = None) -> None:
    """Answer one AskQuery. Safe to run twice (acks_late redelivers).

    A finished ask is left alone; a "running" one is taken up again, since
    that is what a redelivery after a worker crash looks like -- or a retry.
    Taking one up starts its streamed text over: the partial answer is
    cleared with the claim, and a ``reset`` tells readers (DECISIONS D365).
    """
    publisher = publisher or events.Publisher(ask_id)
    previous = AskQuery.objects.filter(pk=ask_id).values_list("status", flat=True).first()
    claimed = AskQuery.objects.filter(pk=ask_id, status__in=UNFINISHED).update(
        status=AskQuery.Status.RUNNING, partial_answer=""
    )
    if not claimed:
        return
    if previous == AskQuery.Status.RUNNING:
        publisher.reset()
    ask = AskQuery.objects.select_related("user", "conversation").get(pk=ask_id)

    # A conversation turn searches its follow-up condensed to stand alone,
    # and is answered with its history (assistant/conversation.py). A plain
    # ask, and turn 1, search the question as asked.
    turn = conversation.prepare(ask) if ask.conversation_id else None
    query = turn.search_question if turn else ask.question

    try:
        # Hybrid search already falls back to keyword-only when the query
        # can't be embedded (D70); this catches what it still lets through.
        hits = search(ask.user, query, k=settings.ASK_RETRIEVAL_K)
    except EmbeddingError:
        logger.warning("Ask %s: search failed", ask_id, exc_info=True)
        _fail(ask_id, SEARCH_FAILED, publisher)
        return
    AskQuery.objects.filter(pk=ask_id).update(retrieved=[_retrieved(hit) for hit in hits])

    if not is_relevant(hits):
        # Nothing to answer from: a fixed answer, and no provider call to pay
        # for. Still an answer (done), so still one ask of the quota.
        _finish(ask_id, publisher=publisher, answer=settings.ASK_NO_ANSWER_TEXT)
        return

    excerpts = [
        Excerpt(
            n=n,
            note_id=hit.note_id,
            chunk_id=hit.chunk_id,
            title=hit.title,
            heading_path=hit.heading_path,
            text=hit.text,
            attachment_id=hit.attachment_id,
            attachment_name=hit.attachment_name,
        )
        for n, hit in enumerate(hits, start=1)
    ]
    # build_messages fits the excerpts itself; fitting the same list here
    # gives the same result, which is what the model saw and so what its
    # markers can refer to.
    fitted = fit_excerpts(excerpts)
    if turn is None:
        system, user = build_messages(ask.question, excerpts)
        version = prompt_version()
    else:
        system, user = conversation.build_chat_messages(
            ask.question, excerpts, turn.history, turn.summary
        )
        version = conversation.chat_prompt_version()

    try:
        result = _stream_answer(ask_id, system, user, publisher)
    except chat.ChatError:
        logger.warning("Ask %s: the provider refused", ask_id, exc_info=True)
        _fail(ask_id, CHAT_FAILED, publisher)
        return

    _finish(
        ask_id,
        publisher=publisher,
        answer=result.text,
        citations=parse_citations(result.text, fitted),
        provider=result.provider,
        model=result.model,
        prompt_version=version,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )


def _stream_answer(ask_id: int, system: str, user: str, publisher) -> chat.ChatResult:
    """The answer call, streamed: publish each piece, save the text so far now and then.

    Returns the provider's result, which is what complete() would have
    returned. The partial text is saved at most every ASK_PARTIAL_SAVE_SECONDS
    (DECISIONS D364), and only while the ask is running, so a late save can
    never touch a finished row. A transient error after some text was
    streamed clears it and publishes ``reset`` before re-raising for the
    retry (D365): the next attempt starts from nothing. Any other error is
    the caller's to fail the ask with, which clears the text as well.
    """
    text = ""
    result = None
    saved_at = monotonic()
    pieces = chat.stream(system, user)
    try:
        for item in pieces:
            if isinstance(item, chat.ChatResult):
                result = item
                continue
            publisher.delta(item, offset=len(text))
            text += item
            now = monotonic()
            if now - saved_at >= settings.ASK_PARTIAL_SAVE_SECONDS:
                _save_partial(ask_id, text)
                saved_at = now
    except TRANSIENT:
        if text:
            _save_partial(ask_id, "")
            publisher.reset()
        raise
    finally:
        close = getattr(pieces, "close", None)
        if close is not None:
            close()  # a generator left mid-way still closes its connection
    if result is None:
        raise chat.ChatError("The chat stream ended without a result")
    return result


def _save_partial(ask_id: int, text: str) -> None:
    AskQuery.objects.filter(pk=ask_id, status=AskQuery.Status.RUNNING).update(partial_answer=text)


def is_relevant(hits: list[SearchHit], floor: float | None = None) -> bool:
    """Whether any hit is worth sending to the model (DECISIONS D74).

    A hit counts if its cosine similarity reaches the floor -- never the RRF
    score, which is rank, not relevance (D69) -- or if the keyword leg
    matched it at all. A keyword match is words of the question in the
    note: exactly what an embedding can miss (a code, a name, a rare term),
    and cheap to be wrong about, since the prompt tells the model to say
    when the excerpts don't answer. Skipping the call wrongly is worse: it
    tells the user their notes say nothing when they do.
    """
    floor = settings.ASK_RELEVANCE_FLOOR if floor is None else floor
    return any(
        hit.keyword_rank is not None or (hit.similarity is not None and hit.similarity >= floor)
        for hit in hits
    )


def _retrieved(hit: SearchHit) -> dict:
    return {
        "chunk_id": hit.chunk_id,
        "note_id": hit.note_id,
        "score": hit.score,
        "similarity": hit.similarity,
        "keyword_rank": hit.keyword_rank,
    }


COST_FIELDS = ("provider", "model", "input_tokens", "output_tokens")


@transaction.atomic
def _finish(ask_id, publisher: events.Publisher | None = None, **fields) -> bool:
    """Store a result and mark the ask done -- only if it is still unfinished.

    Conditional, so a late duplicate run can never overwrite an answer
    already given, or revive a failed ask. The ask's usage event gets the
    provider, model and token counts in the same commit, for cost reports
    (DECISIONS D133); a floor answer made no provider call and has none.
    The ``done`` event is published once this has committed. Returns
    whether this call finished it.
    """
    done = AskQuery.objects.filter(pk=ask_id, status__in=UNFINISHED).update(
        status=AskQuery.Status.DONE,
        completed_at=timezone.now(),
        error="",
        partial_answer="",
        **fields,
    )
    cost = {name: fields[name] for name in COST_FIELDS if name in fields}
    if done and cost:
        # The ask's own use only: a turn's condense event has its own cost.
        limits.describe_where({"ask_id": ask_id, "key": quota.KEY}, **cost)
    if done:
        _queue_fold(ask_id)
        _queue_memory(ask_id)
        _announce(ask_id, publisher)
    return bool(done)


def _announce(ask_id: int, publisher: events.Publisher | None) -> None:
    """Publish how the ask ended, after the commit that ended it (DECISIONS D363).

    After, so a reader that fetches the row on ``done`` finds it done.
    ``robust``: an on-commit callback that raised would fail the task's
    caller after the answer is safely stored; the publisher swallows its
    own errors anyway.
    """
    publisher = publisher or events.Publisher(ask_id)
    if publisher.client is not None:  # events off: nothing to queue
        transaction.on_commit(publisher.outcome, robust=True)


def _queue_fold(ask_id: int) -> None:
    """After a conversation turn is answered, fold its conversation if it needs it.

    The check is one cheap query inside the finishing transaction (it sees
    this answer); the fold itself is its own task, queued on commit so it
    reads what this commit wrote, and never delays or fails the answer: a
    broker that is down costs the fold only, which the next turn retries
    (DECISIONS D284).
    """
    conversation_id = (
        AskQuery.objects.filter(pk=ask_id).values_list("conversation_id", flat=True).first()
    )
    if conversation_id is None or not conversation.should_fold(conversation_id):
        return

    def enqueue():
        try:
            fold_history.delay(conversation_id)
        except Exception:
            logger.exception("Conversation %s: could not queue the fold", conversation_id)

    transaction.on_commit(enqueue)


def _queue_memory(ask_id: int) -> None:
    """After a conversation turn is answered, learn from it -- unless memory is off.

    Queued the way the fold is (D284): on commit, so the task reads the
    answer, and a broker that is down costs the extraction only, never the
    answer. A plain ask, and a user with memory off, queue nothing; the
    task checks memory again, since it can be switched off in between
    (DECISIONS D401).
    """
    wanted = AskQuery.objects.filter(
        pk=ask_id, conversation__isnull=False, user__memory_enabled=True
    ).exists()
    if not wanted:
        return

    def enqueue():
        try:
            extract_memory.delay(ask_id)
        except Exception:
            logger.exception("Ask %s: could not queue the memory extraction", ask_id)

    transaction.on_commit(enqueue)


@shared_task
def extract_memory(ask_id: int) -> None:
    """Learn facts about the user from one finished turn (assistant/memory.py, D400-D409).

    Not retried: a turn whose extraction fails teaches nothing, and the
    user saying it again is the retry. Safe to run twice: a turn is
    extracted at most once.
    """
    memory.extract(ask_id)


@shared_task
def purge_expired_facts() -> int:
    """Daily: expired dynamic facts and old superseded ones (assistant/memory.py, D406)."""
    deleted = memory.purge_expired()
    if deleted:
        logger.info("Purged %s expired or superseded fact(s)", deleted)
    return deleted


@shared_task
def fold_history(conversation_id: int) -> None:
    """Fold a conversation's oldest unsummarised turns into its summary (DECISIONS D280-D287).

    Safe to run twice, concurrently or redelivered: the write is conditional
    (conversation.fold). Not retried when the provider fails -- the
    conversation is simply unchanged and the next finished turn queues
    another. A fold that leaves more than the budget unsummarised (a long
    backlog, folded a batch at a time) queues itself again.
    """
    if conversation.fold(conversation_id) and conversation.should_fold(conversation_id):
        fold_history.delay(conversation_id)


@transaction.atomic
def _fail(ask_id, message: str, publisher: events.Publisher | None = None) -> bool:
    """Fail the ask if it is still unfinished, and refund it with the same commit.

    Only the call that actually fails it refunds, and a refund never
    refunds twice, so a duplicate run or a race with the sweeper hands
    back exactly one ask. Any streamed text is dropped with it, and the
    ``failed`` event published once this has committed. Returns whether
    this call failed it.
    """
    failed = AskQuery.objects.filter(pk=ask_id, status__in=UNFINISHED).update(
        status=AskQuery.Status.FAILED,
        completed_at=timezone.now(),
        error=message,
        partial_answer="",
    )
    if failed:
        # The ask's own use only: a condense call that was made stays
        # counted against its system limit (DECISIONS D222).
        limits.refund_where(ask_id=ask_id, key=quota.KEY)
        _announce(ask_id, publisher)
    return bool(failed)


STUCK = "This took too long. Please ask again."


@shared_task
@transaction.atomic
def sweep_stuck_asks() -> int:
    """Fail asks left pending or running past ASK_STUCK_AFTER_SECONDS (DECISIONS D78).

    The net under D76: a worker killed at the hard time limit, or a lost
    message, runs no code, so the ask would be polled forever and -- never
    failing -- count against the quota. The stuck rows are locked, failed
    by an UPDATE that still checks their status, and refunded, all in one
    transaction: an ask that finishes meanwhile is never overwritten, and
    every ask failed here is refunded exactly once. Returns how many it
    failed, for the log.
    """
    cutoff = timezone.now() - timedelta(seconds=settings.ASK_STUCK_AFTER_SECONDS)
    stuck = list(
        AskQuery.objects.select_for_update()
        .filter(status__in=UNFINISHED, created_at__lt=cutoff)
        .values_list("pk", flat=True)
    )
    if not stuck:
        return 0
    # Locked, so no worker can finish one of these before this commits; the
    # status condition stays as a second guard.
    failed = AskQuery.objects.filter(pk__in=stuck, status__in=UNFINISHED).update(
        status=AskQuery.Status.FAILED, completed_at=timezone.now(), error=STUCK, partial_answer=""
    )
    limits.refund_where(ask_id__in=stuck, key=quota.KEY)
    if failed:
        logger.warning("Failed %s stuck ask(s) older than %s", failed, cutoff)
    return failed
