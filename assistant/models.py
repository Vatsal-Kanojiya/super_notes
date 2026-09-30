from django.conf import settings
from django.db import models

# A question is a sentence or two, not a document; the serializer enforces
# this through the field's max_length.
QUESTION_MAX_CHARS = 1000


class AskQuery(models.Model):
    """One question and, once the task has run, its answer (plan §5, §6.5).

    The job shape the reference uses: the API creates the row as pending and
    returns at once, the task moves it to running and then done or failed,
    and the client polls. Every row that is not failed is one ask against
    the month's quota (assistant/quota.py) -- the rows *are* the counter, so
    there is no second number to drift from them.
    """

    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        DONE = "done"
        FAILED = "failed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="asks"
    )
    question = models.CharField(max_length=QUESTION_MAX_CHARS)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)

    answer = models.TextField(blank=True)
    # [{n, note_id, chunk_id, title, snippet}] (assistant/citations.py).
    citations = models.JSONField(default=list, blank=True)
    # What retrieval returned, scores included: for debugging an answer and
    # for tuning ASK_RELEVANCE_FLOOR. Never sent to the client.
    retrieved = models.JSONField(default=list, blank=True)
    # User-safe text only. The vendor's own message goes to the log.
    error = models.CharField(max_length=255, blank=True)

    # Empty when the relevance floor answered without calling a provider.
    provider = models.CharField(max_length=20, blank=True)
    model = models.CharField(max_length=100, blank=True)
    prompt_version = models.CharField(max_length=50, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)

    idempotency_key = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # A retried POST finds the ask it already made instead of making
            # (and counting) a second one. Per user: keys are client-chosen.
            models.UniqueConstraint(
                fields=["user", "idempotency_key"], name="ask_user_idempotency_key"
            ),
        ]
        indexes = [
            # The quota count: this user's asks since the start of the month.
            models.Index(fields=["user", "created_at"], name="ask_user_created"),
            # GET ask/: this user's asks, newest id first (the cursor).
            models.Index(fields=["user", "-id"], name="ask_user_id"),
        ]

    def __str__(self):
        return f"Ask {self.pk} ({self.status})"

    @property
    def finished(self) -> bool:
        return self.status in (self.Status.DONE, self.Status.FAILED)
