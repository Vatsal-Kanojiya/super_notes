"""Background work of the limits layer."""

from celery import shared_task
from django.conf import settings
from django.core.mail import mail_admins


@shared_task
def mail_system_limit_reached(key: str, used: int, limit: int, resets_at: str | None):
    """Tell the admins a feature is paused for everyone (limits.service, D84)."""
    until = f"until {resets_at}" if resets_at else "until the limit is raised"
    mail_admins(
        subject=f"System limit reached: {key}",
        message=(
            f"The system-wide limit for {key!r} is reached ({used} of {limit} used).\n"
            f"Every new request for it is refused with 503 system_limit_reached {until}.\n\n"
            f"To raise it, add or edit the {key!r} row under Limits in the admin "
            f"(/{settings.ADMIN_URL}limits/limit/).\n"
        ),
    )
