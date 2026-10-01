"""Creating a summary job: idempotency, the ``summary`` limit, and the task.

The pattern of notes/format_service.py (D104): one transaction under the
owner's row lock, the idempotency lookup first, the ``summary`` use consumed
before the job exists, the task enqueued on commit.

Two things differ from a format job. The target is a note or one of its
attachments (``create_summary_job`` takes an optional attachment id). And a
second request for a target that already has an unfinished job is answered
with that job (200) instead of paying for another (D552).
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import transaction

from limits import service as limits

from .content import content_to_text
from .models import Attachment, Note, SummaryJob

User = get_user_model()

KEY = "summary"
UNFINISHED = (SummaryJob.Status.PENDING, SummaryJob.Status.RUNNING)


class IdempotencyKeyReused(Exception):
    """The key already made a job for a different target (DECISIONS D75)."""

    def __init__(self, existing: SummaryJob):
        super().__init__(f"Idempotency key already used for summary job {existing.pk}.")
        self.existing = existing


class NothingToSummarize(Exception):
    """The note or file has no words. Nothing is consumed."""


class AttachmentNotReady(Exception):
    """The file's text is not read yet (or could not be). Nothing is consumed."""


@transaction.atomic
def create_summary_job(
    owner, note_id: int, idempotency_key: str, attachment_id: int | None = None
) -> tuple[SummaryJob, bool]:
    """``(job, created)``.

    Raises Note.DoesNotExist / Attachment.DoesNotExist (someone else's, or
    deleted), NothingToSummarize, AttachmentNotReady, IdempotencyKeyReused,
    limits.UserLimitExceeded and limits.SystemLimitExceeded.
    """
    locked = User.objects.select_for_update().only("pk", "plan").get(pk=owner.pk)

    existing = SummaryJob.objects.filter(owner=locked, idempotency_key=idempotency_key).first()
    if existing is not None:
        if (existing.note_id, existing.attachment_id) != (note_id, attachment_id):
            raise IdempotencyKeyReused(existing)
        return existing, False

    note = Note.objects.get(pk=note_id, owner=locked, deleted_at__isnull=True)
    if attachment_id is None:
        if not content_to_text(note.content).strip():
            raise NothingToSummarize
    else:
        attachment = Attachment.objects.get(
            pk=attachment_id, note=note, owner=locked, deleted_at__isnull=True
        )
        if attachment.status != Attachment.Status.READY:
            raise AttachmentNotReady
        if not attachment.extracted_text.strip():
            raise NothingToSummarize

    running = SummaryJob.objects.filter(
        owner=locked, note=note, attachment_id=attachment_id, status__in=UNFINISHED
    ).first()
    if running is not None:
        return running, False

    event = limits.consume(locked, KEY)
    job = SummaryJob.objects.create(
        note=note,
        attachment_id=attachment_id,
        owner=locked,
        base_version=note.version,
        usage_event=event,
        idempotency_key=idempotency_key,
    )

    from .tasks import summarize

    job_id = job.pk
    transaction.on_commit(lambda: summarize.delay(job_id))
    return job, True
