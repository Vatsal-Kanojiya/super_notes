from django.contrib import admin

from .models import NoteChunk


@admin.register(NoteChunk)
class NoteChunkAdmin(admin.ModelAdmin):
    """Read-only: chunks are derived from notes by the indexer, and an
    edit here would be overwritten (or worse, left stale) by the next one."""

    list_display = [
        "id",
        "note",
        "source",
        "attachment",
        "ordinal",
        "heading_path",
        "embedding_model",
        "note_version",
    ]
    list_select_related = ["note", "attachment"]
    list_filter = ["source", "embedding_model"]
    ordering = ["-id"]
    # The vector is 1536 floats; nobody needs it on a page.
    exclude = ["embedding"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
