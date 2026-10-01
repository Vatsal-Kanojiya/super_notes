"""Running a summary job (DECISIONS D550-D559).

``run(job_id)`` is what the ``summarize`` task does after the job's creation
commits (notes/summary_service.py):

1. **Claim** it, pending -> running, and check its target: the note must be
   live and still at ``base_version`` (a note edited before the call is
   failed and refunded, nothing was spent), an attachment must still be
   live and ready.
2. **Call** the chat provider with the versioned prompt (prompts/summary.md).
3. **Embed** a note's summary, with no lock held. An embedding error never
   costs the summary: it is stored without its chunk and the error logged
   (D555) -- the model call has been paid for.
4. **Store** it under the owner's lock (``services.apply_summary``: the
   note's revision, never its version) and mark the job done, in one
   transaction.

Every way out ends done or failed. A failure refunds the ``summary`` use in
the same transaction -- unless the vendor generated and billed an answer
that cannot be used (a refusal, a cut-off, an empty reply: chat.BilledChatError,
or an answer that is nothing but "EMPTY"): then the use stays and the job's
usage event records what the call cost (D500, D551), as for the condenser.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from assistant import chat
from limits import service as limits
from retrieval.embeddings import EmbeddingError, EmbeddingTransientError
from retrieval.indexing import embed_summary, write_summary_chunks

from . import services
from .content import content_to_text
from .models import Attachment, Note, SummaryJob
from .summary_prompt import build_messages, clean_summary, prompt_version

logger = logging.getLogger(__name__)

UNFINISHED = (SummaryJob.Status.PENDING, SummaryJob.Status.RUNNING)

# What the person reads on a failed job, with the code a program branches on.
# Never the provider's own message.
FAILED = ("summary_failed", "The assistant couldn't summarise this. Try again in a little while.")
BUSY = ("summary_busy", "The assistant is busy right now. Try again in a few minutes.")
UNEXPECTED = ("summary_unexpected", "Something went wrong summarising this. Try again.")
STUCK = ("summary_stuck", "This took too long. Please try again.")
EMPTY = ("summary_empty", "The assistant had no summary to give for this. Try again.")
NOTE_CHANGED = (
    "summary_note_changed",
    "This note was edited before it could be summarised. Try again.",
)
GONE = ("summary_gone", "This note or file no longer exists.")
NOT_STORED = (
    "summary_not_stored",
    "A newer summary of this note was already saved, or it was deleted meanwhile.",
)


def run(job_id: int) -> None:
    """Summarise one target, and make sure the job ends done or failed."""
    claimed = SummaryJob.objects.filter(pk=job_id, status__in=UNFINISHED).update(
        status=SummaryJob.Status.RUNNING
    )
    if not claimed:
        return  # Finished already (a redelivery), or never there.
    job = SummaryJob.objects.get(pk=job_id)

    try:
        note = Note.objects.get(pk=job.note_id, owner_id=job.owner_id, deleted_at__isnull=True)
    except Note.DoesNotExist:
        fail(job_id, GONE)
        return

    if job.attachment_id is None:
        if note.version != job.base_version:
            fail(job_id, NOTE_CHANGED)
            return
        title, text = note.title, content_to_text(note.content)
    else:
        attachment = Attachment.objects.filter(
            pk=job.attachment_id,
            note_id=note.pk,
            status=Attachment.Status.READY,
            deleted_at__isnull=True,
        ).first()
        if attachment is None:
            fail(job_id, GONE)
            return
        title, text = attachment.original_name, attachment.extracted_text

    system, user = build_messages(title, text)
    try:
        result = chat.complete(system, user, max_output_tokens=settings.SUMMARY_MAX_OUTPUT_TOKENS)
    except chat.BilledChatError as exc:
        logger.warning("Summary job %s: an unusable reply was billed", job_id, exc_info=True)
        fail(job_id, FAILED, refund=False, **exc.cost())
        return
    except chat.ChatError:
        logger.warning("Summary job %s: the provider refused", job_id, exc_info=True)
        fail(job_id, FAILED)
        return
    # chat.TransientChatError is the task's to retry.

    cost = {
        "provider": result.provider,
        "model": result.model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }
    summary = clean_summary(result.text)
    if not summary:
        fail(job_id, EMPTY, refund=False, **cost)
        return

    write_chunks = None
    if job.attachment_id is None:
        try:
            chunks, vectors, model_id = embed_summary(note, summary)
        except (EmbeddingError, EmbeddingTransientError):
            logger.warning(
                "Summary job %s: the summary could not be embedded; stored without it",
                job_id,
                exc_info=True,
            )
        else:

            def write_chunks(locked_note):
                write_summary_chunks(locked_note, chunks, vectors, model_id, job.base_version)

    finish(job, summary, write_chunks, **cost)


@transaction.atomic
def finish(job: SummaryJob, summary: str, write_chunks, **cost) -> None:
    """Store the summary and mark the job done, only if the job is still unfinished.

    The conditional update comes first and the store only follows it, so a
    late duplicate run neither overwrites a result nor revives a failed job.
    """
    done = SummaryJob.objects.filter(pk=job.pk, status__in=UNFINISHED).update(
        status=SummaryJob.Status.DONE,
        completed_at=timezone.now(),
        error="",
        error_code="",
        summary=summary,
        prompt_version=prompt_version(),
        **cost,
    )
    if not done:
        return
    _describe(job.pk, **cost)
    stored = services.apply_summary(
        job.owner_id,
        job.note_id,
        job.attachment_id,
        summary,
        job.base_version,
        write_chunks=write_chunks,
    )
    if not stored:
        # The call was made and stays counted; nothing was written.
        SummaryJob.objects.filter(pk=job.pk).update(
            status=SummaryJob.Status.FAILED, error_code=NOT_STORED[0], error=NOT_STORED[1]
        )


def _describe(job_id, **cost) -> None:
    """Put provider, model and tokens on the job's usage event (D133)."""
    event_id = SummaryJob.objects.values_list("usage_event_id", flat=True).get(pk=job_id)
    if event_id and cost:
        limits.describe_where(
            {"pk": event_id},
            **{name: cost[name] for name in ("provider", "model", "input_tokens", "output_tokens")},
        )


@transaction.atomic
def fail(job_id, reason: tuple[str, str], *, refund: bool = True, **cost) -> None:
    """Fail the job if it is still unfinished; refund its use in the same commit.

    Only the call that actually fails it refunds, and a refund never refunds
    twice, so a duplicate run or a race with the sweeper hands back one use.
    ``refund=False`` keeps the use (a billed failure); ``cost`` is recorded
    on the usage event either way.
    """
    code, message = reason
    failed = SummaryJob.objects.filter(pk=job_id, status__in=UNFINISHED).update(
        status=SummaryJob.Status.FAILED,
        completed_at=timezone.now(),
        error_code=code,
        error=message,
        **cost,
    )
    if not failed:
        return
    _describe(job_id, **cost)
    if refund:
        limits.refund_where(
            pk=SummaryJob.objects.values_list("usage_event_id", flat=True).get(pk=job_id)
        )
