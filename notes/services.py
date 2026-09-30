"""Every note write goes through here. Nothing else saves a Note.

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

from .models import Note

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
    _after_write(note)
    return note
