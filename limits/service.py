"""Checking and recording use of a limited resource (DECISIONS D84).

``consume`` is the one way in: it checks the user's limit for their plan,
then the system-wide limit, and records a ``UsageEvent`` -- all inside the
caller's transaction, so a feature that fails to save rolls its use back
with it.

Two races, two locks:

* **Per user.** The caller locks the user's row (``select_for_update``)
  before calling, as assistant/services.py already does for asks. A check
  followed by a write is only safe if nobody can check in between; the
  caller usually needs that lock for its own reasons too, and the plan is
  read from the locked copy.
* **System-wide.** Many users means many row locks, none of which stops
  two users' requests at the system edge both seeing room for one. When a
  key has a system limit, consume takes a transaction-scoped Postgres
  advisory lock on the key (DECISIONS D98) before counting, so system
  checks of one key run one at a time. Keys without one never take it.

A month or a day is a calendar one in settings.TIME_ZONE (the users'),
as assistant/quota.py counts: in UTC the month would reset at 05:30.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone

from .models import Limit, Period, UsageEvent

logger = logging.getLogger(__name__)

# The first key of the two-key advisory lock, so this module's locks cannot
# collide with any other advisory lock the project takes later. "LIM".
SYSTEM_LOCK_NAMESPACE = 0x4C494D

# What a UsageEvent may carry besides user, key and amount.
META_FIELDS = frozenset({"ask", "provider", "model", "input_tokens", "output_tokens"})


class UserLimitExceeded(Exception):
    """This user's use of the key is up for the period. The API answers 429."""

    def __init__(self, used: int, limit: int, resets_at: datetime | None):
        when = resets_at.isoformat() if resets_at else "never"
        super().__init__(f"{used} of {limit} used; resets {when}.")
        self.used, self.limit, self.resets_at = used, limit, resets_at


class SystemLimitExceeded(Exception):
    """Everyone's use of the key is up for the period. The API answers 503."""

    def __init__(self, key: str):
        super().__init__(f"System limit reached for {key!r}.")
        self.key = key


@dataclass(frozen=True)
class Rule:
    """A key's effective values: its ``Limit`` row if it has one, else the default."""

    key: str
    period: str
    user_free: int | None = None
    user_premium: int | None = None
    system: int | None = None
    enabled: bool = True

    def for_user(self, user) -> int | None:
        """The user's limit by plan; None is unlimited (or not enforced)."""
        if not self.enabled:
            return None
        return {"free": self.user_free, "premium": self.user_premium}[user.plan]

    @property
    def system_limit(self) -> int | None:
        return self.system if self.enabled else None


def get_rule(key: str) -> Rule:
    """Raises KeyError for a key not in LIMIT_DEFAULTS: a typo must fail loudly."""
    default = settings.LIMIT_DEFAULTS[key]
    row = Limit.objects.filter(key=key).first()
    if row is None:
        return Rule(key=key, **default)
    return Rule(
        key=key,
        period=row.period,
        user_free=row.user_free,
        user_premium=row.user_premium,
        system=row.system,
        enabled=row.enabled,
    )


def period_bounds(period: str, now: datetime) -> tuple[datetime | None, datetime | None]:
    """[start, end) of the period containing ``now``; (None, None) for ``total``."""
    local = timezone.localtime(now)
    if period == Period.TOTAL:
        return None, None
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == Period.DAY:
        return start, start + timedelta(days=1)
    if period == Period.MONTH:
        start = start.replace(day=1)
        # Day 28 + 4 days is always in the next month, whatever this month's length.
        return start, (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    raise ValueError(f"Unknown period {period!r}.")


def _used(key: str, start: datetime | None, end: datetime | None, user=None) -> int:
    """Sum of the non-refunded events of ``key`` in [start, end); everyone's if no user."""
    events = UsageEvent.objects.filter(key=key, refunded=False)
    if user is not None:
        events = events.filter(user=user)
    if start is not None:
        events = events.filter(created_at__gte=start, created_at__lt=end)
    return events.aggregate(total=Sum("amount"))["total"] or 0


def usage(user, key: str, now: datetime | None = None) -> dict:
    """``{used, limit, resets_at}`` for one user; limit None is unlimited."""
    rule = get_rule(key)
    start, end = period_bounds(rule.period, now or timezone.now())
    return {
        "used": _used(key, start, end, user=user),
        "limit": rule.for_user(user),
        "resets_at": end,
    }


def system_usage(key: str, now: datetime | None = None) -> dict:
    """``{used, limit, resets_at}`` across every user."""
    rule = get_rule(key)
    start, end = period_bounds(rule.period, now or timezone.now())
    return {"used": _used(key, start, end), "limit": rule.system_limit, "resets_at": end}


def _lock_system(key: str) -> None:
    """Serialise system checks of ``key`` until the transaction ends."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))", [SYSTEM_LOCK_NAMESPACE, key]
        )


def consume(user, key: str, amount: int = 1, **meta) -> UsageEvent:
    """Record ``amount`` of ``key`` for ``user`` (None for a system-only use).

    Call inside the caller's transaction, after locking the user's row; the
    event, and the advisory lock, last as long as that transaction. Raises
    UserLimitExceeded or SystemLimitExceeded and records nothing if either
    limit would be passed. ``meta`` fills the event's ``ask``, ``provider``,
    ``model``, ``input_tokens`` and ``output_tokens``.
    """
    if amount < 1:
        raise ValueError("amount must be at least 1.")
    unknown = set(meta) - META_FIELDS
    if unknown:
        raise TypeError(f"Unknown usage fields: {sorted(unknown)}.")
    # Outside a transaction the advisory lock would be released as soon as
    # it was taken, and the check would race (DECISIONS D98).
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("limits.consume() must run inside transaction.atomic().")

    rule = get_rule(key)
    now = timezone.now()
    start, end = period_bounds(rule.period, now)

    user_limit = rule.for_user(user) if user is not None else None
    if user_limit is not None:
        used = _used(key, start, end, user=user)
        if used + amount > user_limit:
            raise UserLimitExceeded(used, user_limit, end)

    system_limit = rule.system_limit
    if system_limit is not None:
        _lock_system(key)
        used = _used(key, start, end)
        if used + amount > system_limit:
            _alert_admins_once(key, used, system_limit, start, end, now)
            raise SystemLimitExceeded(key)

    return UsageEvent.objects.create(user=user, key=key, amount=amount, created_at=now, **meta)


def refund(event: UsageEvent) -> bool:
    """Stop ``event`` counting. True if this call refunded it; harmless twice."""
    changed = UsageEvent.objects.filter(pk=event.pk, refunded=False).update(refunded=True)
    event.refunded = True
    return bool(changed)


def _alert_admins_once(key, used, limit, start, end, now) -> None:
    """Mail the admins the first time ``key`` is refused for the system this period.

    Deduplicated in the cache (DECISIONS D99), not the database: the refused
    request's transaction rolls back, and a marker row would roll back with
    it. The limit is part of the marker, so raising the limit and filling it
    again alerts again. Sent by a task, so the SMTP round trip happens
    outside this transaction's locks.
    """
    period = start.isoformat() if start else "total"
    marker = f"limits:system-alerted:{key}:{period}:{limit}"
    timeout = (end - now).total_seconds() if end else None
    if not cache.add(marker, True, timeout):
        return
    logger.warning("System limit reached for %s: %s of %s used.", key, used, limit)

    from .tasks import mail_system_limit_reached

    try:
        mail_system_limit_reached.delay(key, used, limit, end.isoformat() if end else None)
    except Exception:
        # The broker is down: the user still gets their 503, and the next
        # refusal tries again.
        cache.delete(marker)
        logger.exception("Could not queue the system-limit mail for %s.", key)
