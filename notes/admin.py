from django.contrib import admin

from .models import Attachment, FormatJob, Note, Reminder, ReminderDelivery


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Reminder)
class ReminderAdmin(ReadOnlyAdmin):
    """Read-only, like notes: a reminder write must stamp its note's revision."""

    list_display = ["id", "note", "owner", "due_at", "lead_days", "status", "deleted_at"]
    list_filter = ["status", ("deleted_at", admin.EmptyFieldListFilter)]
    search_fields = ["owner__email"]
    list_select_related = ["note", "owner"]
    ordering = ["-id"]


@admin.register(ReminderDelivery)
class ReminderDeliveryAdmin(ReadOnlyAdmin):
    """Read-only: a delivery row is the at-most-once claim; editing one could resend."""

    list_display = ["id", "reminder", "occurrence_at", "sent_at"]
    list_select_related = ["reminder"]
    ordering = ["-id"]


@admin.register(Note)
class NoteAdmin(ReadOnlyAdmin):
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


@admin.register(FormatJob)
class FormatJobAdmin(ReadOnlyAdmin):
    """Read-only, for support: a job is made and finished by its service and task.

    Editing one here could make a failed job look done without a refund, or the
    reverse.
    """

    list_display = [
        "id",
        "owner",
        "note",
        "status",
        "error_code",
        "provider",
        "input_tokens",
        "output_tokens",
        "created_at",
    ]
    list_filter = ["status", "error_code"]
    search_fields = ["owner__email"]
    list_select_related = ["owner", "note"]
    ordering = ["-id"]


@admin.register(Attachment)
class AttachmentAdmin(ReadOnlyAdmin):
    """Read-only, like notes: an attachment write must stamp its note's revision
    and record or release its storage.

    The file itself is left out: it is never served by URL (the storage
    refuses to make one), and the bytes are the user's.
    """

    list_display = [
        "id",
        "note",
        "owner",
        "mime_type",
        "size",
        "status",
        "created_at",
        "deleted_at",
    ]
    list_filter = ["status", "mime_type", ("deleted_at", admin.EmptyFieldListFilter)]
    search_fields = ["owner__email"]
    list_select_related = ["note", "owner"]
    exclude = ["file"]
    ordering = ["-id"]
