from django.contrib import admin

from .models import AskQuery, Conversation


@admin.register(AskQuery)
class AskQueryAdmin(admin.ModelAdmin):
    """Read-only, for support and debugging.

    The quota is counted in the limits ledger (assistant/quota.py), and a
    failed status is what refunds an ask: an edited status would hand one
    back without a refund, or the reverse. To give a user more asks, change
    their plan or the chat_turns limit.
    """

    list_display = [
        "id",
        "user",
        "conversation",
        "position",
        "status",
        "provider",
        "model",
        "created_at",
        "completed_at",
    ]
    list_filter = ["status", "provider"]
    search_fields = ["user__email", "question"]
    list_select_related = ["user"]
    ordering = ["-id"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    """Read-only, for support. Its turns are AskQuery rows (filter by conversation)."""

    list_display = ["id", "user", "title", "created_at", "updated_at", "deleted_at"]
    search_fields = ["user__email", "title"]
    list_select_related = ["user"]
    ordering = ["-id"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
