"""The monthly ask quota, counted from AskQuery rows.

A month is a calendar month in settings.TIME_ZONE (Asia/Kolkata), the
users' own: counting in UTC would reset the quota at 05:30 on the 1st.
Failed asks are not counted -- a vendor outage should not cost the user
their questions.

usage() is only a *count*. Deciding whether one more ask fits must happen
under the user's row lock (assistant/services.py), or two asks at the edge
could both see room for one.
"""

from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from .models import AskQuery


def month_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """[start, end) of the calendar month containing ``now``, in the local zone."""
    local = timezone.localtime(now or timezone.now())
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Day 28 + 4 days is always in the next month, whatever this month's length.
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


def limit_for(user) -> int:
    return settings.ASK_QUOTAS[user.plan]


def used(user, now: datetime | None = None) -> int:
    start, end = month_bounds(now)
    return (
        AskQuery.objects.filter(user=user, created_at__gte=start, created_at__lt=end)
        .exclude(status=AskQuery.Status.FAILED)
        .count()
    )


def usage(user, now: datetime | None = None) -> dict:
    """``{used, limit, resets_at}``, as `me/` and a 429 report it."""
    now = now or timezone.now()
    return {"used": used(user, now), "limit": limit_for(user), "resets_at": month_bounds(now)[1]}
