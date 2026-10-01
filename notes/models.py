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


class FormatJob(models.Model):
    """One "format my note" request and, once the task has run, its proposal.

    The job shape of AskQuery (assistant/models.py): the API creates the row
    pending and returns at once, notes/tasks.py moves it to running and then
    done or failed, and the client polls. It consumes one ``format`` use of
    the limits ledger when it is made (``usage_event``), refunded if the job
    fails (DECISIONS D102, D240).

    **The job never writes the note.** ``proposed_content`` is only a
    proposal; the client applies it with an ordinary PATCH carrying
    ``version = base_version``, so a note edited meanwhile is the usual 409.
    """

    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        DONE = "done"
        FAILED = "failed"

    note = models.ForeignKey(Note, on_delete=models.CASCADE, related_name="format_jobs")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="format_jobs",
        db_index=False,
    )
    # The note's version when the job was made, and the content the model was
    # shown (the task refuses to run on any other version).
    base_version = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)

    # A TipTap document that passed the guardrail; null until done.
    proposed_content = models.JSONField(null=True, blank=True)
    # For programs (``format_changed_content``...) and, user-safe, for people.
    # The vendor's own message goes to the log.
    error_code = models.CharField(max_length=50, blank=True)
    error = models.CharField(max_length=255, blank=True)

    provider = models.CharField(max_length=20, blank=True)
    model = models.CharField(max_length=100, blank=True)
    prompt_version = models.CharField(max_length=50, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)

    # The ``format`` use this job consumed: the task refunds it on failure
    # and records the cost on it. SET_NULL, so deleting an event never
    # deletes a job.
    usage_event = models.ForeignKey(
        "limits.UsageEvent", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    idempotency_key = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # A retried POST finds the job it already made instead of making
            # (and counting) a second one. Per owner: keys are client-chosen.
            models.UniqueConstraint(
                fields=["owner", "idempotency_key"], name="formatjob_owner_idempotency_key"
            ),
        ]
        indexes = [
            models.Index(fields=["owner", "-id"], name="formatjob_owner_id"),
            # The sweeper's range scan over unfinished jobs.
            models.Index(fields=["status", "created_at"], name="formatjob_status_created"),
        ]

    def __str__(self):
        return f"Format job {self.pk} ({self.status})"
