"""How a user's limits are shown (``me/``'s ``limits``)."""

from rest_framework import serializers

# The keys a user has their own limit on, in the order a client lists them.
# System-only keys (signups, condense, summarize_history, memory_extract) are
# not the user's business and are left out. image_text has a per-user limit
# but is not listed yet (D529): a client would need to show it.
USER_KEYS = ("chat_turns", "format", "summary", "storage_bytes")


class LimitUsageSerializer(serializers.Serializer):
    used = serializers.IntegerField(help_text="Used this period (bytes for `storage_bytes`).")
    limit = serializers.IntegerField(allow_null=True, help_text="Null: unlimited.")
    resets_at = serializers.DateTimeField(
        allow_null=True, help_text="When `used` goes back to 0. Null if it never resets."
    )


class UserLimitsSerializer(serializers.Serializer):
    """One entry per user-facing limit key (DECISIONS D84, D91)."""

    chat_turns = LimitUsageSerializer(help_text="Asks and chat turns, per month.")
    format = LimitUsageSerializer(help_text="Format my note, per month.")
    summary = LimitUsageSerializer(help_text="Attachment summaries, per month.")
    storage_bytes = LimitUsageSerializer(help_text="Attachment storage in bytes, all time.")
