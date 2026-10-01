"""Notes tasks: formatting a note, and delivering reminders.

Formatting: prompt, complete, guard, store (DECISIONS D240-D249).

The task owns the FormatJob's life after creation: pending -> running -> done
or failed, and every way out of it ends in done or failed -- retries running
out, the soft time limit, bugs -- as for asks (D76). Failing a job refunds
its ``format`` use in the same transaction (D102), here and in the sweeper.
A job whose provider call was made records what it cost on its usage event
either way, a guardrail refusal included: the tokens were spent.

The task never touches the note. It reads it, and a proposal that passes
notes/format_guard.py is stored on the job for the client to apply with an
ordinary PATCH at ``base_version``.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from assistant import chat
from limits import service as limits
from retrieval.embeddings import EmbeddingTransientError

from . import delivery, extraction
from .format_guard import check_format
from .format_prompt import build_messages, prompt_version
from .models import Attachment, FormatJob, Note

logger = logging.getLogger(__name__)

TRANSIENT = (chat.TransientChatError,)
UNFINISHED = (FormatJob.Status.PENDING, FormatJob.Status.RUNNING)

# What the person reads on a failed job, with the code a program branches on.
# Never the provider's own message, which can name keys, models or quotas.
CHANGED = (
    "format_changed_content",
    "The formatted version changed what your note says, so it was not used. Nothing was changed.",
)
FAILED = ("format_failed", "The assistant couldn't format this note. Try again in a little while.")
BUSY = ("format_busy", "The assistant is busy right now. Try again in a few minutes.")
UNEXPECTED = ("format_unexpected", "Something went wrong formatting this note. Try again.")
STUCK = ("format_stuck", "This took too long. Please try again.")
NOTE_CHANGED = (
    "format_note_changed",
    "This note was edited before it could be formatted. Try again.",
)
NOTE_GONE = ("format_note_gone", "This note no longer exists.")

_FENCE = re.compile(r"^```(?:json)?\s*\n(.*)\n```$", re.DOTALL | re.IGNORECASE)


@shared_task(
    bind=True,
    # As answer_ask: a rate limit or an outage is worth waiting out briefly,
    # someone is watching a spinner. About a minute of backoff in all.
    autoretry_for=TRANSIENT,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=4,
)
def format_note(self, job_id: int) -> None:
    """Format one note, and make sure the job ends done or failed."""
    try:
        _format(job_id)
    except TRANSIENT as exc:
        if self.request.retries < self.max_retries:
            raise  # autoretry_for schedules the next attempt.
        logger.error("Format job %s: giving up after %s retries: %r", job_id, self.max_retries, exc)
        _fail(job_id, BUSY)
    except Exception:
        logger.exception("Format job %s failed unexpectedly", job_id)
        _fail(job_id, UNEXPECTED)
        raise


def _format(job_id: int) -> None:
    """Safe to run twice (acks_late redelivers): a finished job is left alone."""
    claimed = FormatJob.objects.filter(pk=job_id, status__in=UNFINISHED).update(
        status=FormatJob.Status.RUNNING
    )
    if not claimed:
        return
    job = FormatJob.objects.get(pk=job_id)

    try:
        note = Note.objects.get(pk=job.note_id, owner_id=job.owner_id, deleted_at__isnull=True)
    except Note.DoesNotExist:
        _fail(job_id, NOTE_GONE)
        return
    if note.version != job.base_version:
        # An apply at base_version would be a 409 anyway; don't pay for it.
        _fail(job_id, NOTE_CHANGED)
        return

    system, user = build_messages(note.content)
    try:
        result = chat.complete(system, user, max_output_tokens=settings.FORMAT_MAX_OUTPUT_TOKENS)
    except chat.ChatError:
        logger.warning("Format job %s: the provider refused", job_id, exc_info=True)
        _fail(job_id, FAILED)
        return

    cost = {
        "provider": result.provider,
        "model": result.model,
        "prompt_version": prompt_version(),
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }
    proposed = parse_document(result.text)
    verdict = check_format(
        note.content,
        proposed,
        min_kept=settings.FORMAT_MIN_WORDS_KEPT,
        min_original=settings.FORMAT_MIN_WORDS_ORIGINAL,
    )
    if not verdict.ok:
        logger.warning(
            "Format job %s refused: %s (kept %.2f, original %.2f)",
            job_id,
            verdict.reason,
            verdict.recall,
            verdict.precision,
        )
        _fail(job_id, CHANGED, **cost)
        return
    _finish(job_id, proposed_content=proposed, **cost)


def parse_document(text: str):
    """The model's JSON, or None if it is not JSON. Tolerates a markdown fence."""
    text = text.strip()
    if match := _FENCE.match(text):
        text = match.group(1)
    try:
        return json.loads(text)
    except ValueError:
        return None


def _describe(job_id, **cost) -> None:
    """Put provider, model and tokens on the job's usage event (D133)."""
    event_id = FormatJob.objects.values_list("usage_event_id", flat=True).get(pk=job_id)
    if event_id:
        limits.describe_where(
            {"pk": event_id},
            **{name: cost[name] for name in ("provider", "model", "input_tokens", "output_tokens")},
        )


@transaction.atomic
def _finish(job_id, **fields) -> None:
    """Store the proposal and mark the job done -- only if it is still unfinished.

    Conditional, so a late duplicate run can never overwrite a result, or
    revive a failed job.
    """
    done = FormatJob.objects.filter(pk=job_id, status__in=UNFINISHED).update(
        status=FormatJob.Status.DONE,
        completed_at=timezone.now(),
        error="",
        error_code="",
        **fields,
    )
    if done:
        _describe(job_id, **fields)


@transaction.atomic
def _fail(job_id, reason: tuple[str, str], **cost) -> None:
    """Fail the job if it is still unfinished, and refund it with the same commit.

    Only the call that actually fails it refunds, and a refund never refunds
    twice, so a duplicate run or a race with the sweeper hands back exactly
    one use. ``cost`` (a call that was made and then refused) is recorded.
    """
    code, message = reason
    failed = FormatJob.objects.filter(pk=job_id, status__in=UNFINISHED).update(
        status=FormatJob.Status.FAILED,
        completed_at=timezone.now(),
        error_code=code,
        error=message,
        **cost,
    )
    if failed:
        if cost:
            _describe(job_id, **cost)
        limits.refund_where(
            pk=FormatJob.objects.values_list("usage_event_id", flat=True).get(pk=job_id)
        )


@shared_task
@transaction.atomic
def sweep_stuck_format_jobs() -> int:
    """Fail jobs left pending or running past FORMAT_STUCK_AFTER_SECONDS (DECISIONS D78).

    The net under the task's own handling: a worker killed at the hard time
    limit, or a lost message, runs no code. Locked, failed by an UPDATE that
    still checks the status, and refunded, all in one transaction, so every
    job failed here is refunded exactly once. Returns how many it failed.
    """
    cutoff = timezone.now() - timedelta(seconds=settings.FORMAT_STUCK_AFTER_SECONDS)
    stuck = list(
        FormatJob.objects.select_for_update()
        .filter(status__in=UNFINISHED, created_at__lt=cutoff)
        .values_list("pk", "usage_event_id")
    )
    if not stuck:
        return 0
    ids = [pk for pk, _ in stuck]
    failed = FormatJob.objects.filter(pk__in=ids, status__in=UNFINISHED).update(
        status=FormatJob.Status.FAILED,
        completed_at=timezone.now(),
        error_code=STUCK[0],
        error=STUCK[1],
    )
    limits.refund_where(pk__in=[event for _, event in stuck if event])
    if failed:
        logger.warning("Failed %s stuck format job(s) older than %s", failed, cutoff)
    return failed


# --- Attachment text extraction (notes/extraction.py has the how and why) ----

EXTRACTION_TRANSIENT = (chat.TransientChatError, EmbeddingTransientError)


@shared_task(
    bind=True,
    # An outage or a rate limit is worth waiting out; nobody is watching a
    # spinner, so the backoff is longer than an ask's.
    autoretry_for=EXTRACTION_TRANSIENT,
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=5,
    # Tighter than the default task's: a hostile PDF must not hold a worker
    # for ten minutes (D340).
    soft_time_limit=settings.ATTACHMENT_EXTRACT_SOFT_TIME_LIMIT,
    time_limit=settings.ATTACHMENT_EXTRACT_TIME_LIMIT,
)
def extract_attachment(self, attachment_id: int) -> None:
    """Read, index and finish one attachment; it always ends ready or failed."""
    try:
        extraction.run(attachment_id)
    except EXTRACTION_TRANSIENT as exc:
        if self.request.retries < self.max_retries:
            raise  # autoretry_for schedules the next attempt.
        logger.error(
            "Attachment %s: giving up after %s retries: %r", attachment_id, self.max_retries, exc
        )
        extraction.fail(attachment_id, extraction.BUSY)
    except SoftTimeLimitExceeded:
        logger.warning("Attachment %s: extraction hit the soft time limit", attachment_id)
        extraction.fail(attachment_id, extraction.TOO_SLOW)
    except Exception:
        logger.exception("Attachment %s: extraction failed unexpectedly", attachment_id)
        extraction.fail(attachment_id, extraction.UNEXPECTED)
        raise


@shared_task
def sweep_stuck_attachments() -> int:
    """Fail attachments pending or extracting past ATTACHMENT_STUCK_AFTER_SECONDS.

    The net under the task's own handling: a lost message (the broker was
    down at upload), or a worker killed at the hard time limit, runs no
    code. Each is failed through the service, which re-checks the status
    under the owner's lock and stamps the note, so one finishing right now
    is left alone. Returns how many it failed.
    """
    cutoff = timezone.now() - timedelta(seconds=settings.ATTACHMENT_STUCK_AFTER_SECONDS)
    stuck = Attachment.objects.filter(
        status__in=(Attachment.Status.PENDING, Attachment.Status.EXTRACTING),
        deleted_at__isnull=True,
        created_at__lt=cutoff,
    ).values_list("pk", flat=True)
    failed = sum(extraction.fail(pk, extraction.TOO_SLOW) for pk in list(stuck))
    if failed:
        logger.warning("Failed %s stuck attachment(s) older than %s", failed, cutoff)
    return failed


# --- Reminder delivery (notes/delivery.py has the how and why) ---------------
# Neither task retries: a claimed occurrence is sent at most once (D137).


@shared_task
def deliver_due_reminders() -> int:
    """Beat, every minute: claim what is due and enqueue the sends."""
    return len(delivery.sweep())


@shared_task
def send_reminder_task(delivery_id: int) -> None:
    delivery.send(delivery_id)
