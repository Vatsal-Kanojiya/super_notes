"""The ask quota in tests, now that it is the chat_turns limit (DECISIONS D101)."""

from django.conf import settings

from assistant.models import AskQuery
from limits.models import UsageEvent


def chat_turns(free, premium):
    """LIMIT_DEFAULTS with chat_turns set per plan, for override_settings.

    Replaces V1's ``ASK_QUOTAS={"free": ..., "premium": ...}``.
    """
    return {
        **settings.LIMIT_DEFAULTS,
        "chat_turns": {
            **settings.LIMIT_DEFAULTS["chat_turns"],
            "user_free": free,
            "user_premium": premium,
        },
    }


def record_usage(ask):
    """The ledger entry create_ask would have made, for a row made behind its back.

    A failed ask gets none, as after the backfill (limits/migrations/0002).
    Reads created_at back, since a test may have back-dated the row.
    """
    ask.refresh_from_db(fields=["status", "created_at"])
    if ask.status != AskQuery.Status.FAILED:
        UsageEvent.objects.create(
            user_id=ask.user_id, key="chat_turns", ask=ask, created_at=ask.created_at
        )
    return ask
