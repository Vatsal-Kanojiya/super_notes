from django.core.management.base import BaseCommand
from django.db.models import Count, Exists, OuterRef

from notes.models import Note
from retrieval.embeddings import embedding_model_id
from retrieval.models import NoteChunk


class Command(BaseCommand):
    help = "Show how far the chunk index is behind the notes."

    def handle(self, *args, **options):
        model_id = embedding_model_id()
        live = Note.objects.filter(deleted_at__isnull=True)
        chunks = NoteChunk.objects.filter(note=OuterRef("pk"))

        # A note with no chunks is either not indexed yet or empty; both
        # are reported, and re-indexing an empty note is harmless.
        without = live.filter(~Exists(chunks)).count()
        lagging = live.filter(Exists(chunks.exclude(note_version=OuterRef("version")))).count()
        other_model = live.filter(Exists(chunks.exclude(embedding_model=model_id))).count()
        orphans = NoteChunk.objects.filter(note__deleted_at__isnull=False).aggregate(
            chunks=Count("pk"), notes=Count("note", distinct=True)
        )

        self.stdout.write(f"Embedding model: {model_id}")
        self.stdout.write(f"Live notes: {live.count()}")
        self.stdout.write(f"  without chunks: {without}")
        self.stdout.write(f"  chunks behind the note's version: {lagging}")
        self.stdout.write(f"  chunks from another embedding model: {other_model}")
        self.stdout.write(
            f"Orphan chunks of deleted notes: {orphans['chunks']} ({orphans['notes']} notes)"
        )
