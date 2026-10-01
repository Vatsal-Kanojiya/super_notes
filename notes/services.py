"""Every note and reminder write goes through here. Nothing else saves a Note.

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

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import Note, Reminder

User = get_user_model()


class VersionConflict(Exception):
    """The client edited an old version. ``current`` is the server's copy."""

    def __init__(self, current: Note):
        super().__init__(f"Note {current.pk} is at version {current.version}.")
        self.current = current


def _next_revision(owner) -> int:
    """Lock the owner's row and take the next revision.

    Must run inside ``transaction.atomic``: the lock is held until the
    transaction ends, which is what serialises the owner's writes.
    """
    user = User.objects.select_for_update().only("pk", "notes_revision").get(pk=owner.pk)
    user.notes_revision += 1
    user.save(update_fields=["notes_revision"])
    # The caller's ``owner`` object is left alone: if the transaction rolls
    # back, a copy updated here would claim a revision that never happened.
    return user.notes_revision


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
