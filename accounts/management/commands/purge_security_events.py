"""Delete security event rows once they are older than the retention period.

Carried over from the reference. The trail (accounts.SecurityEvent, written
through accounts/audit.py) is meant to answer "what happened to this
account recently", not to grow for ever -- and every row holds an address
and an email, which are personal data that should not be kept longer than
they are useful. A ``--days`` override, a ``--dry-run``, and a daily beat
entry (CELERY_BEAT_SCHEDULE, config/settings.py) through accounts/tasks.py.

A plain ``queryset.delete()``: these rows own nothing and nothing points at
them, so there is no ordering to get right.
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import SecurityEvent


class Command(BaseCommand):
    help = "Delete security events older than the retention period."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=settings.SECURITY_EVENT_RETENTION_DAYS)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be removed without removing it.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        cutoff = timezone.now() - timedelta(days=days)
        stale = SecurityEvent.objects.filter(created_at__lt=cutoff)

        if options["dry_run"]:
            self.stdout.write(f"{stale.count()} security event(s) older than {days} days")
            return

        removed = stale.delete()[0]
        self.stdout.write(
            self.style.SUCCESS(f"Removed {removed} security event(s) older than {days} days.")
        )
