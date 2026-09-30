from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from notes.models import Note
from retrieval.embeddings import EmbeddingError
from retrieval.indexing import index_note
from retrieval.tasks import index_note_task


class Command(BaseCommand):
    help = (
        "Index every live note of one user or of everyone: a backfill, or the "
        "re-index after the embedding model changes. Unchanged chunks are reused."
    )

    def add_arguments(self, parser):
        parser.add_argument("--user", metavar="EMAIL", help="Only this user's notes.")
        parser.add_argument("--all", action="store_true", help="Every user's notes.")
        parser.add_argument(
            "--sync", action="store_true", help="Index here and now instead of queueing."
        )

    def handle(self, *args, user=None, all=False, sync=False, **options):
        if bool(user) == bool(all):
            raise CommandError("Give exactly one of --user EMAIL or --all.")
        notes = Note.objects.filter(deleted_at__isnull=True).order_by("pk")
        if user:
            owner = get_user_model().objects.filter(email__iexact=user).first()
            if owner is None:
                raise CommandError(f"No user with email {user}.")
            notes = notes.filter(owner=owner)

        targets = list(notes.values_list("pk", "version"))
        if not sync:
            for note_id, version in targets:
                index_note_task.delay(note_id, version)
            self.stdout.write(f"Queued {len(targets)} notes.")
            return

        failed = 0
        for note_id, version in targets:
            try:
                index_note(note_id, version)
            except EmbeddingError as exc:
                failed += 1
                self.stderr.write(f"Note {note_id}: {exc}")
        self.stdout.write(f"Indexed {len(targets) - failed} notes, {failed} failed.")
