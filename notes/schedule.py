"""When a reminder notifies: the single source of its schedule (DECISIONS D95).

A reminder is due at ``due_at``. It notifies once a day at that instant's
local time of day, in the owner's timezone, from ``lead_days`` days before
the due date up to the due date itself: ``lead_days + 1`` notifications,
8 for the default of 7, one for 0.

"Local time of day" is the point. Across a DST change the heads-ups keep
their wall-clock time (09:00 stays 09:00) and so move by an hour in UTC;
adding 24-hour steps in UTC would drift to 08:00 or 10:00 on the other side.

Two wall-clock edge cases, where a local time does not name exactly one
instant (D125):

- **A skipped time** (clocks go forward, e.g. 01:30 in London on the last
  Sunday of March) is read with the offset in force before the change, so it
  lands just after it: 01:30 becomes 02:30 BST. Nobody gets a heads-up early.
- **A repeated time** (clocks go back, 01:30 happens twice) is the first of
  the two.

The due-day occurrence is always ``due_at`` itself, never rebuilt from its
wall-clock time, so the last notification is exactly the instant the user
chose even when that instant is the second 01:30.
"""

from __future__ import annotations

import zoneinfo
from datetime import datetime, timedelta, tzinfo
from datetime import timezone as dt_timezone

from django.conf import settings

# datetime.UTC is Python 3.11+; the project targets 3.10 (CI).
UTC = dt_timezone.utc


def user_timezone(user) -> tzinfo:
    """The user's timezone, or the site default if theirs no longer exists.

    ``User.timezone`` is validated when set, but tz data can drop a name
    between releases; a reminder should still fire rather than crash.
    """
    try:
        return zoneinfo.ZoneInfo(user.timezone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, TypeError):
        return zoneinfo.ZoneInfo(settings.TIME_ZONE)


def occurrences(due_at: datetime, lead_days: int, tz: tzinfo) -> list[datetime]:
    """Every notification instant of a reminder, oldest first, in UTC."""
    if due_at.tzinfo is None:
        raise ValueError("due_at must be timezone-aware.")
    if lead_days < 0:
        raise ValueError("lead_days must be 0 or more.")

    local = due_at.astimezone(tz)
    clock = local.timetz().replace(tzinfo=None)
    result = []
    for days_before in range(lead_days, 0, -1):
        day = local.date() - timedelta(days=days_before)
        # fold=0 everywhere: the first of a repeated time, and the pre-change
        # offset for a skipped one (see the module docstring).
        wall = datetime.combine(day, clock).replace(tzinfo=tz, fold=0)
        result.append(wall.astimezone(UTC))
    result.append(due_at.astimezone(UTC))
    return result
