"""Every note, reminder and attachment write goes through here. Nothing else saves a Note.

Each write, in one transaction:

1. locks the owner's User row (``SELECT ... FOR UPDATE``),
2. increments ``User.notes_revision``,
3. stamps the note with that value as its ``revision`` and bumps its
   ``version``; ``Note.save()`` derives ``content_text``,
4. calls ``_after_write(note)``, which enqueues indexing on commit.

The lock is the point (DECISIONS D5). A user's writes queue on their own
row, so revisions are handed out *in commit order*: once revision n is
visible, every revision below it already is. That is what lets
``GET notes/changes/?after=n`` promise "everything after n" -- the promise
a timestamp cannot make, because a timestamp is taken before a slow
transaction commits. The cost is that one user's writes run one at a time,
which at typing speed nobody notices. Other users are never blocked.

The version check happens *after* the lock, on a fresh read. Checked
before, two saves carrying the same version could both pass and the second
would overwrite the first without a conflict.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from limits import service as limits

from .attachments import attachment_storage
from .models import Attachment, Note, Reminder

logger = logging.getLogger(__name__)

User = get_user_model()


class VersionConflict(Exception):
    """The client edited an old version. ``current`` is the server's copy."""

    def __init__(self, current: Note):
        super().__init__(f"Note {current.pk} is at version {current.version}.")
        self.current = current


def _lock_owner(owner):
    """Lock the owner's row and return a fresh copy of it (with its plan).

    Must run inside ``transaction.atomic``: the lock is held until the
    transaction ends, which is what serialises the owner's writes.
    """
    return User.objects.select_for_update().only("pk", "plan", "notes_revision").get(pk=owner.pk)


def _bump_revision(locked) -> int:
    """Take the next revision on a row ``_lock_owner`` returned."""
    locked.notes_revision += 1
    locked.save(update_fields=["notes_revision"])
    return locked.notes_revision


def _next_revision(owner) -> int:
    """Lock the owner's row and take the next revision.

    The caller's ``owner`` object is left alone: if the transaction rolls
    back, a copy updated here would claim a revision that never happened.
    """
    return _bump_revision(_lock_owner(owner))


def _locked_live_note(owner, note_id) -> Note:
    """Re-read a note after the owner's lock is held.

    Every writer takes the same lock first, so this read sees the latest
    committed version. Raises Note.DoesNotExist for someone else's note or
    one deleted since the caller loaded it.
    """
    return Note.objects.get(pk=note_id, owner=owner, deleted_at__isnull=True)


def _after_write(note: Note) -> None:
    """Runs inside the write's transaction, after the note is saved.

    Enqueues indexing on commit, so a worker never looks for a row that is
    not visible yet and a rolled-back write is never indexed. One function,
    so no write path can forget. A tombstone is enqueued without the
    debounce: it means "remove its chunks", and that should be quick.
    """
    # Imported here: retrieval imports notes, so a module-level import
    # would be a cycle.
    from retrieval.tasks import index_note_task

    countdown = 0 if note.deleted_at else settings.INDEX_DEBOUNCE_SECONDS
    note_id, version = note.pk, note.version
    transaction.on_commit(
        lambda: index_note_task.apply_async((note_id, version), countdown=countdown)
    )


@transaction.atomic
def create_note(owner, *, type=Note.Type.TEXT, title="", content=None) -> Note:
    note = Note(owner=owner, type=type, title=title)
    if content is not None:
        note.content = content
    note.revision = _next_revision(owner)
    note.save()
    _after_write(note)
    return note


@transaction.atomic
def update_note(owner, note_id, *, expected_version: int, **changes) -> Note:
    """Apply ``changes`` (any of type, title, content) if nobody got there first.

    Raises VersionConflict carrying the current note when ``expected_version``
    is not the stored one, and Note.DoesNotExist when the note is gone.
    """
    unknown = set(changes) - {"type", "title", "content"}
    if unknown:
        raise TypeError(f"Not editable: {', '.join(sorted(unknown))}")

    revision = _next_revision(owner)
    note = _locked_live_note(owner, note_id)
    if note.version != expected_version:
        # Raising rolls the transaction back, revision increment included,
        # so a refused write leaves no gap for sync to trip over.
        raise VersionConflict(note)

    for field, value in changes.items():
        setattr(note, field, value)
    note.version += 1
    note.revision = revision
    note.save()
    _after_write(note)
    return note


@transaction.atomic
def delete_note(owner, note_id) -> Note:
    """Turn a note into a tombstone. No version check: see DECISIONS D28.

    The content stays on the row (the admin can still read it); clients are
    only ever sent the tombstone's id, version, revision and dates.
    """
    revision = _next_revision(owner)
    note = _locked_live_note(owner, note_id)
    note.deleted_at = timezone.now()
    note.version += 1
    note.revision = revision
    note.save(update_fields=["deleted_at", "version", "revision", "updated_at"])
    # A deleted note's reminders stop: none is ever delivered for a note
    # nobody can open. Done ones stay done; ones the user deleted stay deleted.
    Reminder.objects.filter(
        note=note, status=Reminder.Status.SCHEDULED, deleted_at__isnull=True
    ).update(status=Reminder.Status.CANCELLED, updated_at=note.deleted_at)
    # Its attachments go with it: storage released, files deleted on commit.
    _release_attachments(
        Attachment.objects.filter(note=note, deleted_at__isnull=True), note.deleted_at
    )
    _after_write(note)
    return note


# Reminders ---------------------------------------------------------------
#
# A reminder write is a write to its note as far as sync is concerned: it
# takes the same owner lock, takes the next revision and stamps it on the
# note, so ``notes/changes/`` sends the note again with its current
# reminders (D124). The note's ``version`` and ``updated_at`` are left alone:
# its content did not change, so an editor holding it must not conflict, and
# "recently edited" ordering must not move. Nothing is re-indexed either.

REMINDERS_PER_NOTE_MAX = 20

REMINDER_FIELDS = {"due_at", "lead_days", "channels"}


class TooManyReminders(Exception):
    """The note already has ``REMINDERS_PER_NOTE_MAX`` reminders."""


def _stamp_note(note: Note, revision: int) -> None:
    # A queryset update, so neither ``updated_at`` (auto_now) nor
    # ``content_text`` is touched.
    Note.objects.filter(pk=note.pk).update(revision=revision)
    note.revision = revision


def _locked_live_reminder(owner, reminder_id) -> Reminder:
    """Re-read a reminder under the owner's lock; its note must be live too.

    Raises Reminder.DoesNotExist for someone else's reminder, a deleted one,
    or one whose note has been deleted.
    """
    return Reminder.objects.select_related("note").get(
        pk=reminder_id,
        owner=owner,
        deleted_at__isnull=True,
        note__deleted_at__isnull=True,
    )


@transaction.atomic
def create_reminder(owner, note_id, *, due_at, lead_days=None, channels=None) -> Reminder:
    """Add a reminder to a live note of ``owner``'s.

    Raises Note.DoesNotExist for someone else's note or a deleted one, and
    TooManyReminders past the per-note cap (D128).
    """
    revision = _next_revision(owner)
    note = _locked_live_note(owner, note_id)
    # Counted under the lock, so two concurrent creates cannot both squeeze
    # under the cap.
    live = Reminder.objects.filter(note=note, deleted_at__isnull=True).count()
    if live >= REMINDERS_PER_NOTE_MAX:
        raise TooManyReminders
    reminder = Reminder(note=note, owner=owner, due_at=due_at)
    if lead_days is not None:
        reminder.lead_days = lead_days
    if channels is not None:
        reminder.channels = channels
    reminder.save()
    _stamp_note(note, revision)
    return reminder


@transaction.atomic
def update_reminder(owner, reminder_id, **changes) -> Reminder:
    """Change any of due_at, lead_days, channels. The status is untouched (D127)."""
    unknown = set(changes) - REMINDER_FIELDS
    if unknown:
        raise TypeError(f"Not editable: {', '.join(sorted(unknown))}")

    revision = _next_revision(owner)
    reminder = _locked_live_reminder(owner, reminder_id)
    for field, value in changes.items():
        setattr(reminder, field, value)
    reminder.save()
    _stamp_note(reminder.note, revision)
    return reminder


@transaction.atomic
def mark_reminder_done(owner, reminder_id) -> Reminder:
    """Stop the rest of the series. Marking a done reminder done again changes nothing."""
    revision = _next_revision(owner)
    reminder = _locked_live_reminder(owner, reminder_id)
    if reminder.status == Reminder.Status.DONE:
        # Nothing changed, so give the revision back rather than make every
        # device fetch the note again.
        transaction.set_rollback(True)
        return reminder
    reminder.status = Reminder.Status.DONE
    reminder.save(update_fields=["status", "updated_at"])
    _stamp_note(reminder.note, revision)
    return reminder


@transaction.atomic
def delete_reminder(owner, reminder_id) -> Reminder:
    """Soft-delete a reminder. Its note's next ``changes`` entry leaves it out."""
    revision = _next_revision(owner)
    reminder = _locked_live_reminder(owner, reminder_id)
    reminder.deleted_at = timezone.now()
    reminder.save(update_fields=["deleted_at", "updated_at"])
    _stamp_note(reminder.note, revision)
    return reminder


# Attachments ---------------------------------------------------------------
#
# Like a reminder, an attachment write is a write to its note for sync: the
# owner lock, the next revision stamped on the note, version and updated_at
# untouched (D326). Under the same lock the ``storage_bytes`` limit is
# checked and recorded (limits/, D84, D91) -- one lock, so two uploads at the
# edge of the quota cannot both fit. The bytes themselves are written to
# storage *before* the lock (D323): a 10 MB write to S3 must not hold up the
# owner's other saves, and a refused or duplicate upload's file is deleted.

STORAGE_KEY = "storage_bytes"


def _delete_files(names) -> None:
    """Delete stored files, best effort: a failure is logged, never raised.

    Runs after the transaction that stopped referring to them has committed
    (or instead of one that never will), so raising would only turn a done
    request into a 500. A file left behind is unreachable: no live row
    names it.
    """
    storage = attachment_storage()
    for name in names:
        try:
            storage.delete(name)
        except Exception:
            logger.exception("Could not delete attachment file %s.", name)


def _release_attachments(attachments, when) -> None:
    """Soft-delete ``attachments``, refund their storage, delete their files on commit.

    Runs inside the caller's transaction, under the owner's lock.
    """
    rows = list(attachments.only("pk", "file", "usage_event_id"))
    if not rows:
        return
    Attachment.objects.filter(pk__in=[row.pk for row in rows]).update(deleted_at=when)
    # Storage is a running total ("total" period), so releasing a file is
    # refunding the use its upload recorded (D324).
    limits.refund_where(pk__in=[row.usage_event_id for row in rows if row.usage_event_id])
    names = [row.file.name for row in rows if row.file]
    # Out of search at once, in this transaction: the chunks' rows are what
    # search reads, and an extraction finishing now waits on the owner lock
    # and then finds the attachment deleted (D346).
    from retrieval.indexing import deindex_attachments

    deindex_attachments(row.pk for row in rows)
    transaction.on_commit(lambda: _delete_files(names))


def _after_attachment_added(attachment: Attachment) -> None:
    """Runs inside the upload's transaction, once the new row is saved.

    The one place a new attachment's follow-up work starts: text
    extraction, enqueued on commit as ``_after_write`` enqueues indexing,
    so no upload path can skip it. The row is ``pending`` until a worker
    takes it. A broker that cannot be reached is logged, not raised: the
    upload has committed, and the stuck-attachment sweeper fails the row
    later rather than leave it pending forever (D345).
    """
    # Imported here: notes.tasks imports the assistant and retrieval, which
    # import notes.
    from .tasks import extract_attachment

    attachment_id = attachment.pk

    def enqueue():
        try:
            extract_attachment.delay(attachment_id)
        except Exception:
            logger.exception("Could not queue text extraction for attachment %s.", attachment_id)

    transaction.on_commit(enqueue)


# Extraction's status changes (notes/extraction.py). Each takes the owner's
# lock and stamps the note with the next revision when, and only when, the
# status really changes, so ``notes/changes/`` sends the note again with the
# attachment's new status (D326). Every update is conditional on the status
# it expects, so a duplicate task run or a race with the sweeper changes it
# once.

EXTRACTION_UNFINISHED = (Attachment.Status.PENDING, Attachment.Status.EXTRACTING)


def _locked_extraction(attachment_id, statuses):
    """Lock the attachment's owner, then the attachment if it is live in ``statuses``.

    ``(locked owner, attachment)``; the attachment is None when it is gone,
    deleted, its note is deleted, or it has moved past ``statuses``.
    """
    owner_id = (
        Attachment.objects.filter(pk=attachment_id).values_list("owner_id", flat=True).first()
    )
    if owner_id is None:
        return None, None
    locked = _lock_owner(User(pk=owner_id))
    attachment = (
        Attachment.objects.select_for_update(of=("self",))
        .select_related("note")
        .filter(
            pk=attachment_id,
            status__in=statuses,
            deleted_at__isnull=True,
            note__deleted_at__isnull=True,
        )
        .first()
    )
    return locked, attachment


@transaction.atomic
def start_extraction(attachment_id) -> Attachment | None:
    """Claim an attachment for extraction: pending -> extracting.

    Returns the attachment (with its note) to work on, or None when there is
    nothing to do. An attachment already ``extracting`` is returned as it is:
    that is a redelivered task (acks_late) taking it up again, and no
    revision is taken for a status that did not change.
    """
    locked, attachment = _locked_extraction(attachment_id, EXTRACTION_UNFINISHED)
    if attachment is None:
        return None
    if attachment.status == Attachment.Status.PENDING:
        attachment.status = Attachment.Status.EXTRACTING
        attachment.save(update_fields=["status"])
        _stamp_note(attachment.note, _bump_revision(locked))
    return attachment


def save_extracted_text(attachment_id, text: str) -> bool:
    """Keep the text read from the file while the attachment is still extracting.

    So a retry after a failure further on (embedding) does not read the file
    -- or pay for a vision call -- again. No revision: nothing a client sees
    changes.
    """
    return bool(
        Attachment.objects.filter(
            pk=attachment_id, status=Attachment.Status.EXTRACTING, deleted_at__isnull=True
        ).update(extracted_text=text)
    )


def earlier_extracted_text(attachment: Attachment) -> str | None:
    """The text already read from the same bytes for the same owner, or None (D524).

    Any earlier attachment of the owner's with the same ``sha256`` that is
    ``ready`` -- on another note, or since deleted (its row keeps its text)
    -- read these very bytes already, so its text is reused: no file read,
    no model call, no ``image_text`` use. ``""`` is a real answer (a photo
    without words) and is reused too. Never another owner's: the hash says
    nothing about who may read what.
    """
    return (
        Attachment.objects.filter(
            owner_id=attachment.owner_id,
            sha256=attachment.sha256,
            status=Attachment.Status.READY,
        )
        .exclude(pk=attachment.pk)
        .order_by("-pk")
        .values_list("extracted_text", flat=True)
        .first()
    )


@transaction.atomic
def consume_for_owner(owner_id, key: str):
    """Record one use of ``key`` for an owner, under the owner's lock (D520).

    The per-user check counts and then writes; the lock is what keeps two of
    the owner's workers from both seeing room for one. Raises
    limits.UserLimitExceeded or SystemLimitExceeded, recording nothing.
    """
    return limits.consume(_lock_owner(User(pk=owner_id)), key)


@transaction.atomic
def finish_extraction(attachment_id, *, write_chunks=None) -> bool:
    """extracting -> ready, writing the chunks under the same lock. True if it did.

    ``write_chunks(attachment)`` runs once the attachment is known to be
    live and still extracting, so chunks are never written for one deleted
    meanwhile: the delete takes the same owner lock.
    """
    locked, attachment = _locked_extraction(attachment_id, (Attachment.Status.EXTRACTING,))
    if attachment is None:
        return False
    if write_chunks is not None:
        write_chunks(attachment)
    attachment.status, attachment.error = Attachment.Status.READY, ""
    attachment.save(update_fields=["status", "error"])
    _stamp_note(attachment.note, _bump_revision(locked))
    return True


@transaction.atomic
def fail_extraction(attachment_id, error: str) -> bool:
    """pending or extracting -> failed with a user-safe ``error``. True if it did.

    Any chunk a partial run left is removed with it.
    """
    locked, attachment = _locked_extraction(attachment_id, EXTRACTION_UNFINISHED)
    if attachment is None:
        return False
    from retrieval.indexing import deindex_attachments

    deindex_attachments([attachment.pk])
    attachment.status, attachment.error = Attachment.Status.FAILED, error[:255]
    attachment.save(update_fields=["status", "error"])
    _stamp_note(attachment.note, _bump_revision(locked))
    return True


def _live_attachment(owner, note_id, sha256) -> Attachment | None:
    return Attachment.objects.filter(
        owner_id=owner.pk, note_id=note_id, sha256=sha256, deleted_at__isnull=True
    ).first()


def add_attachment(
    owner, note_id, upload, *, original_name: str, mime_type: str, sha256: str
) -> tuple[Attachment, bool]:
    """Store ``upload`` on a live note of ``owner``'s: ``(attachment, created)``.

    The caller has sniffed ``mime_type``, cleaned ``original_name``, checked
    the per-file size and hashed the bytes. The same bytes already on the
    note return that row with ``created`` False, consuming nothing.

    Raises Note.DoesNotExist (someone else's note, or deleted), and
    limits.UserLimitExceeded / SystemLimitExceeded when the file does not fit
    in ``storage_bytes``; nothing is kept in either case.
    """
    # A re-upload is answered without storing the bytes again. Checked
    # again under the lock below, where it counts.
    existing = _live_attachment(owner, note_id, sha256)
    if existing is not None:
        return existing, False

    attachment = Attachment(
        owner_id=owner.pk,
        note_id=note_id,
        original_name=original_name,
        mime_type=mime_type,
        size=upload.size,
        sha256=sha256,
    )
    # upload_to ignores the name given here: the stored one is random.
    attachment.file.save("upload", upload, save=False)
    stored = attachment.file.name
    try:
        attachment, created = _record_attachment(owner, note_id, attachment)
    except BaseException:
        _delete_files([stored])
        raise
    if not created:
        _delete_files([stored])
    return attachment, created


@transaction.atomic
def _record_attachment(owner, note_id, attachment: Attachment) -> tuple[Attachment, bool]:
    locked = _lock_owner(owner)
    note = _locked_live_note(locked, note_id)
    existing = _live_attachment(locked, note.pk, attachment.sha256)
    if existing is not None:
        # Nothing changed, so no revision is taken.
        return existing, False

    # The event first: a refusal raises out of this transaction before the row exists.
    attachment.usage_event = limits.consume(locked, STORAGE_KEY, attachment.size)
    attachment.note = note
    attachment.save()
    _stamp_note(note, _bump_revision(locked))
    _after_attachment_added(attachment)
    return attachment, True


@transaction.atomic
def delete_attachment(owner, attachment_id) -> Attachment:
    """Soft-delete an attachment: storage released, file deleted on commit.

    Raises Attachment.DoesNotExist for someone else's, a deleted one, or one
    whose note is deleted.
    """
    locked = _lock_owner(owner)
    attachment = Attachment.objects.select_related("note").get(
        pk=attachment_id,
        owner=locked,
        deleted_at__isnull=True,
        note__deleted_at__isnull=True,
    )
    when = timezone.now()
    _release_attachments(Attachment.objects.filter(pk=attachment.pk), when)
    attachment.deleted_at = when
    _stamp_note(attachment.note, _bump_revision(locked))
    return attachment
