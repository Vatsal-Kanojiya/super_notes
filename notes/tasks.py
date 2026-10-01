"""Reminder delivery tasks (notes/delivery.py has the how and why).

Neither task retries: a claimed occurrence is sent at most once (D137).
"""

from celery import shared_task

from . import delivery


@shared_task
def deliver_due_reminders() -> int:
    """Beat, every minute: claim what is due and enqueue the sends."""
    return len(delivery.sweep())


@shared_task
def send_reminder_task(delivery_id: int) -> None:
    delivery.send(delivery_id)
