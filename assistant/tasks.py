"""Answering an ask: retrieve, floor, prompt, complete, cite (plan §6.5, docs/RAG.md).

The task owns the AskQuery's life after creation: pending → running → done
or failed. Every way out of it ends in done or failed, including retries
running out, the soft time limit and bugs (DECISIONS D76): a row left
"running" would poll forever and, never failing, count against the quota.
Failing an ask refunds its ``chat_turns`` use in the same transaction
(DECISIONS D102), here and in the sweeper.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from limits import service as limits
from retrieval.embeddings import EmbeddingError, EmbeddingTransientError
from retrieval.search import SearchHit, search

from . import chat
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
    try:
        _answer(ask_id)
    except TRANSIENT as exc:
        if self.request.retries < self.max_retries:
            raise  # autoretry_for schedules the next attempt.
        logger.error("Ask %s: giving up after %s retries: %r", ask_id, self.max_retries, exc)
        _fail(ask_id, BUSY)
    except Exception:
        logger.exception("Ask %s failed unexpectedly", ask_id)
        _fail(ask_id, UNEXPECTED)
        raise


def _answer(ask_id: int) -> None:
    """Answer one AskQuery. Safe to run twice (acks_late redelivers).

    A finished ask is left alone; a "running" one is taken up again, since
    that is what a redelivery after a worker crash looks like.
    """
    claimed = AskQuery.objects.filter(pk=ask_id, status__in=UNFINISHED).update(
        status=AskQuery.Status.RUNNING
    )
    if not claimed:
        return
    ask = AskQuery.objects.select_related("user").get(pk=ask_id)

    try:
        # Hybrid search already falls back to keyword-only when the query
        # can't be embedded (D70); this catches what it still lets through.
        hits = search(ask.user, ask.question, k=settings.ASK_RETRIEVAL_K)
    except EmbeddingError:
        logger.warning("Ask %s: search failed", ask_id, exc_info=True)
        _fail(ask_id, SEARCH_FAILED)
        return
    AskQuery.objects.filter(pk=ask_id).update(retrieved=[_retrieved(hit) for hit in hits])

    if not is_relevant(hits):
        # Nothing to answer from: a fixed answer, and no provider call to pay
        # for. Still an answer (done), so still one ask of the quota.
        _finish(ask_id, answer=settings.ASK_NO_ANSWER_TEXT)
        return

    excerpts = [
        Excerpt(
            n=n,
            note_id=hit.note_id,
            chunk_id=hit.chunk_id,
            title=hit.title,
            heading_path=hit.heading_path,
            text=hit.text,
        )
        for n, hit in enumerate(hits, start=1)
    ]
    # build_messages fits the excerpts itself; fitting the same list here
    # gives the same result, which is what the model saw and so what its
    # markers can refer to.
    fitted = fit_excerpts(excerpts)
    system, user = build_messages(ask.question, excerpts)

    try:
        result = chat.complete(system, user)
    except chat.ChatError:
        logger.warning("Ask %s: the provider refused", ask_id, exc_info=True)
        _fail(ask_id, CHAT_FAILED)
        return

    _finish(
        ask_id,
        answer=result.text,
        citations=parse_citations(result.text, fitted),
        provider=result.provider,
        model=result.model,
        prompt_version=prompt_version(),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )


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


def _finish(ask_id, **fields) -> None:
    """Store a result and mark the ask done -- only if it is still unfinished.

    Conditional, so a late duplicate run can never overwrite an answer
    already given, or revive a failed ask.
    """
    AskQuery.objects.filter(pk=ask_id, status__in=UNFINISHED).update(
        status=AskQuery.Status.DONE, completed_at=timezone.now(), error="", **fields
    )


@transaction.atomic
def _fail(ask_id, message: str) -> None:
    """Fail the ask if it is still unfinished, and refund it with the same commit.

    Only the call that actually fails it refunds, and a refund never
    refunds twice, so a duplicate run or a race with the sweeper hands
    back exactly one ask.
    """
    failed = AskQuery.objects.filter(pk=ask_id, status__in=UNFINISHED).update(
        status=AskQuery.Status.FAILED, completed_at=timezone.now(), error=message
    )
    if failed:
        limits.refund_where(ask_id=ask_id)


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
        status=AskQuery.Status.FAILED, completed_at=timezone.now(), error=STUCK
    )
    limits.refund_where(ask_id__in=stuck)
    if failed:
        logger.warning("Failed %s stuck ask(s) older than %s", failed, cutoff)
    return failed
