from django.contrib import admin

from .models import Note


@admin.register(Note)
class NoteAdmin(admin.ModelAdmin):
    """Read-only, for support and debugging.

    Every write must go through notes/services.py, which takes the owner's
    next revision (DECISIONS D5). An edit saved here would change a note
    without one, and every synced device would miss it; a delete here would
    be a hard delete, which no device ever hears about. So the admin can
    look, and nothing else.
    """

    list_display = [
        "id",
        "title",
        "type",
        "owner",
        "version",
        "revision",
        "updated_at",
        "deleted_at",
    ]
    list_filter = ["type", ("deleted_at", admin.EmptyFieldListFilter)]
    search_fields = ["title", "owner__email"]
    list_select_related = ["owner"]
    ordering = ["-id"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
