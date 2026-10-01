"""The monthly ask quota: the ``chat_turns`` limit, read through limits/ (DECISIONS D101).

A thin wrapper kept so callers that think in "asks this month" (``me/``'s
``ask_usage``) need not know the key. The count is the non-refunded
``chat_turns`` events of limits/, and the limit is that key's value for the
user's plan (LIMIT_DEFAULTS, or its row in the admin). A failed ask is
refunded (assistant/tasks.py), so it does not count -- a vendor outage
should not cost the user their questions.

usage() is only a *count*. Deciding whether one more ask fits happens in
limits.consume(), under the user's row lock (assistant/services.py).
"""

from datetime import datetime

from django.utils import timezone

from limits import service as limits

KEY = "chat_turns"


def month_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """[start, end) of the calendar month containing ``now``, in the local zone."""
    return limits.period_bounds("month", now or timezone.now())


def limit_for(user) -> int | None:
    return limits.get_rule(KEY).for_user(user)


def used(user, now: datetime | None = None) -> int:
    return limits.usage(user, KEY, now)["used"]


def usage(user, now: datetime | None = None) -> dict:
    """``{used, limit, resets_at}``, as `me/` and a 429 report it."""
    return limits.usage(user, KEY, now)
