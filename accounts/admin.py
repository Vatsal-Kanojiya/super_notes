from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import SecurityEvent, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """The plan is set here in V1 -- there are no payments."""

    ordering = ["-date_joined"]
    list_display = ["email", "name", "plan", "is_active", "is_staff", "date_joined"]
    list_filter = ["plan", "is_active", "is_staff"]
    search_fields = ["email", "name"]
    readonly_fields = ["google_sub", "notes_revision", "last_login", "date_joined"]
    fieldsets = [
        (None, {"fields": ["email", "password"]}),
        ("Profile", {"fields": ["name", "avatar_url", "google_sub"]}),
        ("Plan", {"fields": ["plan", "notes_revision"]}),
        ("Permissions", {"fields": ["is_active", "is_staff", "is_superuser", "groups"]}),
        ("Dates", {"fields": ["last_login", "date_joined"]}),
    ]
    add_fieldsets = [(None, {"classes": ["wide"], "fields": ["email", "password1", "password2"]})]
    filter_horizontal = ["groups"]


@admin.register(SecurityEvent)
class SecurityEventAdmin(admin.ModelAdmin):
    """Read-only: an audit trail anyone can edit from the admin proves nothing.

    Rows are written only through accounts.audit.record and removed only by
    the retention job (accounts/management/commands/purge_security_events.py).
    """

    list_display = ["created_at", "event", "email", "user", "ip"]
    list_filter = ["event", "created_at"]
    search_fields = ["email", "ip", "request_id"]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
