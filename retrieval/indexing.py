"""Keeping a note's chunks in step with the note (plan §6.3, docs/RAG.md).

Plain functions, no Celery: retrieval/tasks.py wraps ``index_note`` for the
queue, and the management commands call it directly.

``index_note(note_id, version)`` is idempotent, which ``acks_late`` needs
(a task may run twice). Two rules keep it safe:

* **The network call holds no lock.** Embedding can take seconds; the
  note row is locked only for the short write at the end.
* **The write re-checks the version.** The note may have been edited while
  we were embedding. Its newer task will index the newer text, so this run
  writes nothing rather than overwrite it with stale chunks.

A note's chunks come from three sources (``NoteChunk.source``, D341). This
module's ``index_note`` owns the ``note`` ones only: an attachment's chunks
are written when its text is extracted (``embed_attachment`` then
``write_attachment_chunks``, under the lock notes/extraction.py holds) and
re-embedded by ``index_attachment``. Deleting the note removes them all.
"""

import logging

from django.db import transaction

from notes.models import Attachment, Note

from .chunking import Chunk, chunk_note, chunk_text
from .embeddings import embed_texts, embedding_model_id
from .models import NoteChunk

logger = logging.getLogger(__name__)


NOTE = NoteChunk.Source.NOTE
ATTACHMENT = NoteChunk.Source.ATTACHMENT


def deindex_note(note_id: int) -> int:
    """Delete a note's chunks, every source's. Returns how many there were."""
    deleted, _ = NoteChunk.objects.filter(note_id=note_id).delete()
    return deleted


def deindex_attachments(attachment_ids) -> int:
    """Delete these attachments' chunks. Returns how many there were."""
    deleted, _ = NoteChunk.objects.filter(attachment_id__in=list(attachment_ids)).delete()
    return deleted


def index_note(note_id: int, version: int) -> None:
    """Bring the note's chunks up to ``version``, embedding only what changed."""
    note = Note.objects.filter(pk=note_id).first()
    if note is None:
        return
    if note.deleted_at is not None:
        deindex_note(note_id)
        return
    if note.version != version:
        # Newer: a later task handles it. Older cannot happen after
        # on_commit, but indexing a version we did not read would be wrong.
        return

    model_id = embedding_model_id()
    chunks = chunk_note(note.title, note.content)

    # What we can reuse: a vector per content hash, from chunks embedded
    # with the current model. Repeated paragraphs share a hash and so
    # share one vector, and are embedded once.
    vectors = {
        chunk.content_hash: chunk.embedding
        for chunk in NoteChunk.objects.filter(
            note_id=note_id, source=NOTE, embedding_model=model_id
        )
    }
    missing = list(dict.fromkeys(c.content_hash for c in chunks if c.content_hash not in vectors))
    if missing:
        by_hash = {c.content_hash: c.embed_text for c in chunks}
        # One call: the boundary splits it into provider-sized batches.
        vectors.update(zip(missing, embed_texts([by_hash[h] for h in missing]), strict=True))

    with transaction.atomic():
        current = Note.objects.select_for_update().only("version").filter(pk=note_id).first()
        if current is None or current.version != version:
            return
        _write_chunks(note, chunks, vectors, model_id)


def _write_chunks(note, chunks, vectors, model_id):
    """Make the note's rows match ``chunks``: update, create, delete.

    Existing rows are read again here, under the lock, not taken from the
    earlier read: a duplicate delivery of this task may have changed them
    meanwhile. A row is matched to the first chunk with its hash, in
    ordinal order, so a repeated paragraph is handled the same way every
    time; rows left over are the stale ones.
    """
    spare = {}
    own = NoteChunk.objects.filter(note_id=note.pk, source=NOTE)
    for row in own.order_by("ordinal", "pk"):
        if row.embedding_model == model_id:
            spare.setdefault(row.content_hash, []).append(row)

    reused, created = [], []
    for chunk in chunks:
        rows = spare.get(chunk.content_hash)
        if rows:
            row = rows.pop(0)
            row.ordinal, row.text = chunk.ordinal, chunk.text
            row.heading_path, row.note_version = chunk.heading_path, note.version
            reused.append(row)
        else:
            created.append(
                NoteChunk(
                    note=note,
                    owner_id=note.owner_id,
                    ordinal=chunk.ordinal,
                    text=chunk.text,
                    heading_path=chunk.heading_path,
                    content_hash=chunk.content_hash,
                    embedding=vectors[chunk.content_hash],
                    embedding_model=model_id,
                    note_version=note.version,
                )
            )

    keep = [row.pk for row in reused]
    own.exclude(pk__in=keep).delete()
    NoteChunk.objects.bulk_update(reused, ["ordinal", "text", "heading_path", "note_version"])
    NoteChunk.objects.bulk_create(created)
    logger.debug(
        "Indexed note %s v%s: %s reused, %s new", note.pk, note.version, len(reused), len(created)
    )


# Attachments -----------------------------------------------------------------


def embed_attachment(attachment: Attachment, text: str):
    """Chunk an attachment's text and embed what is new: ``(chunks, vectors, model_id)``.

    No lock and no write: the network call happens here, and the caller
    writes with ``write_attachment_chunks`` once it holds its lock. Vectors
    already stored for this attachment (same hash, same model) are reused.
    """
    model_id = embedding_model_id()
    chunks = chunk_text(attachment.original_name, text)
    vectors = {
        row.content_hash: row.embedding
        for row in NoteChunk.objects.filter(attachment_id=attachment.pk, embedding_model=model_id)
    }
    missing = list(dict.fromkeys(c.content_hash for c in chunks if c.content_hash not in vectors))
    if missing:
        by_hash = {c.content_hash: c.embed_text for c in chunks}
        vectors.update(zip(missing, embed_texts([by_hash[h] for h in missing]), strict=True))
    return chunks, vectors, model_id


def write_attachment_chunks(
    attachment: Attachment, chunks: list[Chunk], vectors: dict, model_id: str, note_version: int
) -> None:
    """Replace the attachment's chunks with ``chunks``. The caller holds the lock.

    An attachment's text never changes once extracted, so there is nothing
    to reconcile row by row: the old rows (another model's, or a duplicate
    run's) go and the new ones are written, reusing vectors by hash.
    """
    NoteChunk.objects.filter(attachment_id=attachment.pk).delete()
    NoteChunk.objects.bulk_create(
        NoteChunk(
            note_id=attachment.note_id,
            owner_id=attachment.owner_id,
            source=ATTACHMENT,
            attachment_id=attachment.pk,
            ordinal=chunk.ordinal,
            text=chunk.text,
            heading_path=chunk.heading_path,
            content_hash=chunk.content_hash,
            embedding=vectors[chunk.content_hash],
            embedding_model=model_id,
            note_version=note_version,
        )
        for chunk in chunks
    )
    logger.debug("Indexed attachment %s: %s chunks", attachment.pk, len(chunks))


def index_attachment(attachment_id: int) -> bool:
    """Re-embed a ready attachment's stored text (after a model change). True if written.

    The write locks the attachment's row and re-checks that it is still
    live and ready: a delete meanwhile (which deletes the chunks under the
    same row's lock, notes/services.py) means nothing is written.
    """
    live = Attachment.objects.filter(
        pk=attachment_id,
        status=Attachment.Status.READY,
        deleted_at__isnull=True,
        note__deleted_at__isnull=True,
    )
    attachment = live.first()
    if attachment is None:
        return False
    chunks, vectors, model_id = embed_attachment(attachment, attachment.extracted_text)
    with transaction.atomic():
        locked = live.select_for_update(of=("self",)).select_related("note").first()
        if locked is None:
            return False
        write_attachment_chunks(locked, chunks, vectors, model_id, locked.note.version)
    return True
