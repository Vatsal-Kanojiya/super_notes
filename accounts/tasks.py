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
