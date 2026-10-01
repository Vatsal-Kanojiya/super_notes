from django.contrib import admin

from .models import Limit, UsageEvent


@admin.register(Limit)
class LimitAdmin(admin.ModelAdmin):
    """Overrides of LIMIT_DEFAULTS. A key with no row uses its default.

    Every field of a row applies, so a row copies the defaults it does not
    mean to change. Empty is unlimited; "enabled" off stops enforcing it.
    """

    list_display = ["key", "user_free", "user_premium", "system", "period", "enabled", "updated_at"]
    list_editable = ["user_free", "user_premium", "system", "enabled"]
    list_filter = ["period", "enabled"]
    search_fields = ["key"]


@admin.register(UsageEvent)
class UsageEventAdmin(admin.ModelAdmin):
    """Read-only, for support and debugging.

    The rows are the usage counter: an edited amount or a deleted row would
    silently hand use back or take it away. To give a user more, change
    their plan or the limit.
    """

    list_display = ["id", "key", "user", "amount", "refunded", "provider", "model", "created_at"]
    list_filter = ["key", "refunded", "provider"]
    search_fields = ["user__email", "key"]
    list_select_related = ["user"]
    raw_id_fields = ["user", "ask"]
    ordering = ["-id"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
