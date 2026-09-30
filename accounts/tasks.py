"""Periodic account housekeeping, run by Celery beat (CELERY_BEAT_SCHEDULE).

Each task only calls the management command of the same job, so the beat
path and ``manage.py <command>`` cannot drift apart. Both are harmless to
run twice.
"""

from celery import shared_task
from django.core.management import call_command


@shared_task
def purge_security_events():
    """Beat's entry point into ``manage.py purge_security_events``.

    No ``days`` argument, as in the reference: the command's own
    SECURITY_EVENT_RETENTION_DAYS default is what runs, not whatever value
    happened to be current when the schedule was written.
    """
    call_command("purge_security_events")


@shared_task
def flush_expired_tokens():
    """Beat's entry point into simplejwt's ``manage.py flushexpiredtokens``.

    Every refresh rotates the token, and every token leaves an
    ``OutstandingToken`` row (and its blacklist row) behind: about 50 a day
    for one device in steady use. Once expired they prove nothing -- an
    expired token is refused on its ``exp`` alone -- so they go. A device
    whose token is flushed is already dead, and ``accounts.devices.prune``
    treats a missing token exactly like an expired one.
    """
    call_command("flushexpiredtokens")
