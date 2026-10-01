from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from pgvector.django import HnswIndex, VectorField

from .search_vector import chunk_search_vector


class NoteChunk(models.Model):
    """One embedded piece of a note. Written by retrieval/indexing.py only.

    ``source`` says what the text came from: the note's own content, the
    extracted text of one of its attachments (``attachment`` set, D341), or
    its summary. Each source is indexed on its own -- re-indexing the note
    never touches an attachment's chunks, nor the reverse -- and deleting
    the note deletes them all.

    ``owner`` repeats the note's owner so every search filters on it in SQL
    without a join. A note never changes hands, so it cannot go stale.

    ``embedding_model`` and ``note_version`` say what the row was built
    from: a chunk embedded with another model than the current one is
    stale even if its text is unchanged, and ``note_version`` is what
    ``index_status`` compares to find notes whose chunks lag behind.
    """

    class Source(models.TextChoices):
        NOTE = "note", "Note"
        ATTACHMENT = "attachment", "Attachment"
        SUMMARY = "summary", "Summary"

    note = models.ForeignKey("notes.Note", on_delete=models.CASCADE, related_name="chunks")
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.NOTE)
    # Set exactly when source is "attachment". A soft-deleted attachment's
    # chunks are deleted with it (notes/services.py); CASCADE covers a row
    # removed for good.
    attachment = models.ForeignKey(
        "notes.Attachment",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="chunks",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="note_chunks"
    )
    ordinal = models.PositiveIntegerField()
    text = models.TextField()
    heading_path = models.TextField(blank=True)
    content_hash = models.CharField(max_length=64)
    embedding = VectorField(dimensions=settings.EMBEDDING_DIMENSIONS)
    embedding_model = models.CharField(max_length=200)
    note_version = models.PositiveIntegerField()

    class Meta:
        ordering = ["note_id", "ordinal"]
        indexes = [
            # Vector search: nearest chunks by cosine distance.
            HnswIndex(
                name="chunk_embedding_hnsw",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
            models.Index(fields=["owner", "note"], name="chunk_owner_note"),
            # Keyword search: the expression phase 4 queries.
            GinIndex(chunk_search_vector(), name="chunk_fts"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(source="attachment", attachment__isnull=False)
                    | (~models.Q(source="attachment") & models.Q(attachment__isnull=True))
                ),
                name="chunk_attachment_iff_source",
            ),
        ]

    def __str__(self):
        return f"Chunk {self.ordinal} of note {self.note_id}"
