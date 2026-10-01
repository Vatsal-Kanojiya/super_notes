"""Delivering reminders: the minute sweep, the claim, and the send.

**The sweep** (``sweep()``, every minute from beat) looks at scheduled
reminders whose series could have something due, works out each one's
latest occurrence that is due now (notes/schedule.py), and claims it.

**The claim** is an ``INSERT ... ON CONFLICT DO NOTHING`` of the
occurrence's ``ReminderDelivery``. The unique ``(reminder, occurrence_at)``
constraint is what makes delivery at most once: two sweeps that both decide
an occurrence is due both try the insert, and Postgres lets exactly one
through. Only the sweep whose insert returned a row enqueues the send, and
only once its claim has committed (``on_commit``), so the send never looks
for a row that is not there yet.

**The send** (``send()``, a Celery task per claimed occurrence) marks the
delivery started with a conditional UPDATE before sending anything, so a
task Celery hands out twice (``acks_late`` redelivery) sends once. A worker
that dies mid-send loses that notification rather than repeating it: at
most once, by design (D137). It checks the reminder is still scheduled and
its note still live, then sends on each channel and records the outcome.

Which occurrence is due (D135, D136):

- the latest one at or before now; earlier missed ones are not replayed, so
  a worker that was down for three days sends one heads-up, not three;
- not one already delivered, or earlier than one already delivered;
- not one before the reminder's last change (``updated_at``): creating or
  moving a reminder never fires a heads-up whose time has already passed;
- not one older than ``REMINDER_MISSED_GRACE_HOURS``: a notification that
  late is noise.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone

from accounts import push

from .models import REMINDER_LEAD_DAYS_MAX, Reminder, ReminderDelivery
from .schedule import occurrences, user_timezone

logger = logging.getLogger(__name__)

# A reminder can have a heads-up due now only if it is due within its
# longest lead from now, plus a day for DST shifts.
_SERIES_SPAN = timedelta(days=REMINDER_LEAD_DAYS_MAX + 1)

TITLE_MAX_CHARS = 100


def _grace() -> timedelta:
    return timedelta(hours=settings.REMINDER_MISSED_GRACE_HOURS)


def _candidates(now: datetime) -> list[Reminder]:
    """Scheduled, live reminders whose series overlaps the grace window.

    One indexed range on ``(status, due_at)``. The last delivered occurrence
    comes along, so a reminder with nothing new due costs no further query.
    """
    return list(
        Reminder.objects.filter(
            status=Reminder.Status.SCHEDULED,
            deleted_at__isnull=True,
            note__deleted_at__isnull=True,
            due_at__gte=now - _grace(),
            due_at__lte=now + _SERIES_SPAN,
        )
        .select_related("owner")
        .annotate(last_delivered=Max("deliveries__occurrence_at"))
        .order_by("due_at", "id")
    )


def due_occurrence(reminder: Reminder, now: datetime) -> datetime | None:
    """The occurrence to deliver now, or None. See the module docstring.

    ``reminder.last_delivered`` must be set (``_candidates`` annotates it).
    """
    floor = max(reminder.updated_at, now - _grace())
    if reminder.last_delivered is not None:
        floor = max(floor, reminder.last_delivered + timedelta(microseconds=1))
    due = [
        at
        for at in occurrences(reminder.due_at, reminder.lead_days, user_timezone(reminder.owner))
        if floor <= at <= now
    ]
    return due[-1] if due else None


def _claim(reminder_id: int, occurrence_at: datetime) -> int | None:
    """Insert the occurrence's delivery row; its id, or None if already claimed.

    ``ON CONFLICT DO NOTHING`` rather than catching IntegrityError: no
    savepoint per claim, and no error logged by Postgres for the loser.
    """
    table = ReminderDelivery._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {table} (reminder_id, occurrence_at, created_at, channel_results) "
            "VALUES (%s, %s, %s, '{}'::jsonb) ON CONFLICT DO NOTHING RETURNING id",
            [reminder_id, occurrence_at, timezone.now()],
        )
        row = cursor.fetchone()
    return row[0] if row else None


def sweep(now: datetime | None = None) -> list[int]:
    """Claim every occurrence due now and enqueue its send. Returns the claimed ids."""
    from .tasks import send_reminder_task  # tasks imports this module

    now = now or timezone.now()
    claimed = []
    for reminder in _candidates(now):
        occurrence_at = due_occurrence(reminder, now)
        if occurrence_at is None:
            continue
        # One transaction per claim: a failure later in the sweep must not
        # roll back (and so re-open) claims whose sends are already queued.
        with transaction.atomic():
            delivery_id = _claim(reminder.pk, occurrence_at)
            if delivery_id is not None:
                transaction.on_commit(
                    lambda delivery_id=delivery_id: send_reminder_task.delay(delivery_id)
                )
        if delivery_id is not None:
            claimed.append(delivery_id)
    return claimed


def send(delivery_id: int) -> None:
    """Send one claimed occurrence on each of its reminder's channels."""
    started = ReminderDelivery.objects.filter(pk=delivery_id, sent_at__isnull=True).update(
        sent_at=timezone.now()
    )
    if not started:
        return  # already sent (or being sent) by another run of this task

    delivery = ReminderDelivery.objects.select_related("reminder__note", "reminder__owner").get(
        pk=delivery_id
    )
    reminder = delivery.reminder
    if (
        reminder.status != Reminder.Status.SCHEDULED
        or reminder.deleted_at is not None
        or reminder.note.deleted_at is not None
    ):
        # Done, deleted or cancelled between the claim and now.
        delivery.channel_results = {"skipped": "inactive"}
    else:
        delivery.channel_results = {
            channel: _send_on(channel, reminder, delivery.occurrence_at)
            for channel in reminder.channels
        }
    delivery.save(update_fields=["channel_results"])


def _send_on(channel: str, reminder: Reminder, occurrence_at: datetime) -> str:
    if channel == Reminder.Channel.EMAIL:
        if not reminder.owner.email:
            return "no_address"
        try:
            send_reminder_email(reminder, occurrence_at)
        except Exception:
            # Not retried: at most once (D137). The log has the reason.
            logger.exception("Reminder %s: email failed", reminder.pk)
            return "failed"
        return "sent"
    if channel == Reminder.Channel.PUSH:
        return send_reminder_push(reminder)
    return "unavailable"


def send_reminder_push(reminder: Reminder) -> str:
    """Push to each of the owner's subscriptions; see accounts/push.py for the outcomes.

    The payload is ids and the title, never note content (D170).
    """
    payload = {
        "type": "reminder",
        "reminder_id": reminder.pk,
        "note_id": reminder.note_id,
        "title": _clean_title(reminder.note.title),
    }
    try:
        return push.send_to_user(reminder.owner, payload)
    except Exception:
        logger.exception("Reminder %s: push failed", reminder.pk)
        return "failed"


def _clean_title(title: str) -> str:
    # One line (a newline in a subject is refused as header injection),
    # bounded, and never empty.
    title = " ".join(title.split())
    if len(title) > TITLE_MAX_CHARS:
        title = title[: TITLE_MAX_CHARS - 1].rstrip() + "…"
    return title or "Untitled note"


def _when(due_local: datetime, occurrence_local: datetime) -> str:
    days = (due_local.date() - occurrence_local.date()).days
    if days <= 0:
        return "due now"
    if days == 1:
        return "due tomorrow"
    return f"due in {days} days"


def reminder_email(reminder: Reminder, occurrence_at: datetime) -> tuple[str, str]:
    """Subject and plain-text body: the title, the due date, a link. No note body (D138)."""
    tz = user_timezone(reminder.owner)
    due = reminder.due_at.astimezone(tz)
    title = _clean_title(reminder.note.title)
    when = _when(due, occurrence_at.astimezone(tz))
    due_text = f"{due:%a} {due.day} {due:%b %Y, %H:%M} {due.tzname()}"
    link = f"{settings.WEB_APP_URL}/notes/{reminder.note_id}"
    subject = f"Reminder: {title} ({when})"
    body = (
        f"{title}\n"
        f"Due {due_text}.\n\n"
        f"Open the note: {link}\n\n"
        "You set this reminder in Super Notes. Mark it done there to stop the rest of "
        "its notifications.\n"
    )
    return subject, body


def send_reminder_email(reminder: Reminder, occurrence_at: datetime) -> None:
    subject, body = reminder_email(reminder, occurrence_at)
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [reminder.owner.email])
