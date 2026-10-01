from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.db import models

from .content import content_to_text, empty_doc
from .search import note_search_vector


class Note(models.Model):
    """One note: a TipTap document plus what sync and search need around it.

    **Write through notes/services.py only.** A write there locks the owner,
    takes the next ``User.notes_revision`` and stamps it here, which is what
    makes ``GET notes/changes/`` exact (DECISIONS D5). A plain ``save()``
    from elsewhere would change the note without a new revision, and every
    other device would miss it. That is also why the admin is read-only.

    ``version`` and ``revision`` answer different questions. ``version`` is
    per note, for conflicts: "has this note changed since I loaded it?".
    ``revision`` is per user, for sync: "what changed since I last asked?".

    Deleting is soft. The row stays as a tombstone with ``deleted_at`` set,
    so a device that last synced before the delete learns about it, and so
    the indexer (phase 3) can remove the note's chunks.
    """

    class Type(models.TextChoices):
        TEXT = "text", "Text"
        CHECKLIST = "checklist", "Checklist"

    # No index of its own: the two composite indexes below both lead with
    # owner, so either serves a lookup by owner alone.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notes", db_index=False
    )
    type = models.CharField(max_length=10, choices=Type.choices, default=Type.TEXT)
    title = models.CharField(max_length=500, blank=True)
    # The editor's document, stored as it sends it (notes/content.py).
    content = models.JSONField(default=empty_doc)
    # Derived in save(), never accepted from a client: it is what search
    # reads, so a client able to set it could make a note match anything.
    content_text = models.TextField(blank=True, editable=False)
    version = models.PositiveIntegerField(default=1)
    revision = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            # GET notes/changes/?after=n: this user's notes past revision n.
            models.Index(fields=["owner", "revision"], name="note_owner_revision"),
            # GET notes/: this user's notes, newest id first (the cursor).
            models.Index(fields=["owner", "-id"], name="note_owner_id"),
            # GET notes/?q=: the same expression the query builds.
            GinIndex(note_search_vector(), name="note_fts"),
        ]

    def __str__(self):
        return self.title or f"Note {self.pk}"

    def save(self, *args, **kwargs):
        # Derived here rather than in the service so no write path, however
        # it got here, can leave the two out of step.
        update_fields = kwargs.get("update_fields")
        if update_fields is None or "content" in update_fields:
            self.content_text = content_to_text(self.content)
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "content_text"}
        super().save(*args, **kwargs)


REMINDER_LEAD_DAYS_DEFAULT = 7
REMINDER_LEAD_DAYS_MAX = 30


def default_reminder_channels():
    return ["email", "push"]


class Reminder(models.Model):
    """A due date-time on a note, with daily heads-ups before it (DECISIONS D95).

    The schedule is not stored: ``notes.schedule.occurrences()`` derives it
    from ``due_at``, ``lead_days`` and the owner's timezone, so there is one
    source and nothing to keep in step. What *is* stored, per notification
    actually sent, is a ``ReminderDelivery``.

    **Write through notes/services.py only**, like a note: a reminder write
    takes the owner's lock and stamps the note with the next revision, which
    is how ``notes/changes/`` carries reminders (D5).

    ``owner`` repeats ``note.owner`` so every reminder query is filtered by
    owner in SQL without a join, as notes are. ``status`` is the series:
    ``done`` is the user saying "stop", ``cancelled`` is the note being
    deleted. ``deleted_at`` is the user deleting the reminder itself.
    """

    class Channel(models.TextChoices):
        EMAIL = "email", "Email"
        PUSH = "push", "Push"

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        DONE = "done", "Done"
        CANCELLED = "cancelled", "Cancelled"

    note = models.ForeignKey(Note, on_delete=models.CASCADE, related_name="reminders")
    # No index of its own: (owner, due_at) leads with owner.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reminders",
        db_index=False,
    )
    due_at = models.DateTimeField()
    lead_days = models.PositiveSmallIntegerField(default=REMINDER_LEAD_DAYS_DEFAULT)
    channels = models.JSONField(default=default_reminder_channels)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            # The delivery sweep: scheduled reminders whose series has begun.
            models.Index(fields=["status", "due_at"], name="reminder_status_due"),
            # GET reminders/?from=&to=: this user's reminders by due date.
            models.Index(fields=["owner", "due_at"], name="reminder_owner_due"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(lead_days__lte=REMINDER_LEAD_DAYS_MAX),
                name="reminder_lead_days_max",
            ),
        ]

    def __str__(self):
        return f"Reminder {self.pk} on note {self.note_id}"


class ReminderDelivery(models.Model):
    """One notification of a reminder's series, claimed by the delivery sweep.

    The unique ``(reminder, occurrence_at)`` is the at-most-once guarantee: a
    second worker's insert for the same occurrence fails. ``sent_at`` is set
    when sending *starts*, by a conditional update, so a task run twice sends
    once; ``channel_results`` records each channel's outcome after (D137).
    notes/delivery.py does all of this.
    """

    reminder = models.ForeignKey(Reminder, on_delete=models.CASCADE, related_name="deliveries")
    occurrence_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    channel_results = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["reminder", "occurrence_at"], name="reminder_delivery_once"
            ),
        ]

    def __str__(self):
        return f"Delivery of reminder {self.reminder_id} at {self.occurrence_at:%Y-%m-%d %H:%M}"
