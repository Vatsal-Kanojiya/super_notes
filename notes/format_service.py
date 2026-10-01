"""Creating a format job: idempotency, the ``format`` limit, and the task.

In one transaction, holding the owner's row lock -- the pattern of
assistant/services.py (DECISIONS D104) and the very lock every note write
takes (notes/services.py, D5). Two things follow from it being the same
lock:

* the ``format`` check-and-record cannot race with itself (two requests at
  the edge of the limit both see room otherwise);
* the note is read *under* it, so ``base_version`` is the committed version
  and the content the task will send is exactly that version's.

The idempotency lookup comes first: a retried POST gets back the job it
already made -- counted once, and returned even if the month has filled up
since. The replay is for the same note only; a key reused for another note
is the client's bug (``IdempotencyKeyReused``).

A new job is enqueued on commit, so the worker never looks for a row that is
not visible yet, and a refused or rolled-back create is never run. The
system-wide limit raises SystemLimitExceeded through here untouched (D130).
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction

from limits import service as limits

from .content import content_to_text
from .format_prompt import document_json
from .models import FormatJob, Note

User = get_user_model()

KEY = "format"


class IdempotencyKeyReused(Exception):
    """The key already made a job for a different note (DECISIONS D75)."""

    def __init__(self, existing: FormatJob):
        super().__init__(f"Idempotency key already used for format job {existing.pk}.")
        self.existing = existing


class NothingToFormat(Exception):
    """The note has no words. Nothing is consumed."""


class NoteTooLong(Exception):
    """The note is longer than FORMAT_MAX_INPUT_CHARS. Nothing is consumed."""


@transaction.atomic
def create_format_job(owner, note_id: int, idempotency_key: str) -> tuple[FormatJob, bool]:
    """``(job, created)``.

    Raises Note.DoesNotExist (someone else's, or deleted), NothingToFormat,
    NoteTooLong, IdempotencyKeyReused, limits.UserLimitExceeded and
    limits.SystemLimitExceeded.
    """
    locked = User.objects.select_for_update().only("pk", "plan").get(pk=owner.pk)

    existing = FormatJob.objects.filter(owner=locked, idempotency_key=idempotency_key).first()
    if existing is not None:
        if existing.note_id != note_id:
            raise IdempotencyKeyReused(existing)
        return existing, False

    note = Note.objects.get(pk=note_id, owner=locked, deleted_at__isnull=True)
    if not content_to_text(note.content).strip():
        raise NothingToFormat
    if len(document_json(note.content)) > settings.FORMAT_MAX_INPUT_CHARS:
        raise NoteTooLong

    # The event first: a refusal raises out of this transaction before a job exists.
    event = limits.consume(locked, KEY)
    job = FormatJob.objects.create(
        note=note,
        owner=locked,
        base_version=note.version,
        usage_event=event,
        idempotency_key=idempotency_key,
    )

    # Imported here: the task module imports the chat stack, which creating a
    # row needs none of.
    from .tasks import format_note

    job_id = job.pk
    transaction.on_commit(lambda: format_note.delay(job_id))
    return job, True
