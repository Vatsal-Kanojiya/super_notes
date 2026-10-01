from django.conf import settings
from django.db import models

# A question is a sentence or two, not a document; the serializer enforces
# this through the field's max_length.
QUESTION_MAX_CHARS = 1000

TITLE_MAX_CHARS = 200
# A title made from the first question is cut at a word near this length.
DERIVED_TITLE_CHARS = 80


class Conversation(models.Model):
    """A thread of turns, each an AskQuery (plan §4, DECISIONS D140).

    Holds no answers of its own: a turn is an ask, with its quota,
    idempotency, task and refunds unchanged. Deleting is soft
    (``deleted_at``): the turns stop being shown, and the usage they
    consumed stays counted.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversations"
    )
    # Blank until the first question names it; editable after.
    title = models.CharField(max_length=TITLE_MAX_CHARS, blank=True)
    # A running summary of the turns that no longer fit the prompt, and the
    # last turn position it covers (0: none). Written by phase 1's folding.
    summary = models.TextField(blank=True)
    summary_through = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    # Moved by every new turn: the list is most recently active first.
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "-updated_at"], name="conversation_user_updated"),
        ]

    def __str__(self):
        return f"Conversation {self.pk}"


class AskQuery(models.Model):
    """One question and, once the task has run, its answer (plan §5, §6.5).

    The job shape the reference uses: the API creates the row as pending and
    returns at once, the task moves it to running and then done or failed,
    and the client polls. Each ask consumes one ``chat_turns`` use in the
    limits ledger when it is made, refunded if it fails (assistant/quota.py,
    DECISIONS D101-D102).
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
    # A turn of a conversation, numbered from 1; both null for a plain ask
    # (``POST ask/``). Set together or not at all (the check constraint).
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, null=True, blank=True, related_name="turns"
    )
    position = models.PositiveIntegerField(null=True, blank=True)
    # The follow-up rewritten to stand alone: what was actually searched.
    # Blank when the question was searched as asked.
    standalone_question = models.TextField(blank=True)
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
            # Turns are sequential: one row per place in the conversation.
            # The user lock makes this so (services.create_turn); the
            # constraint makes sure.
            models.UniqueConstraint(
                fields=["conversation", "position"], name="ask_conversation_position"
            ),
            models.CheckConstraint(
                condition=models.Q(conversation__isnull=True, position__isnull=True)
                | models.Q(conversation__isnull=False, position__isnull=False),
                name="ask_turn_has_position",
            ),
        ]
        indexes = [
            # This user's asks by date (V1's quota count; the ledger counts now).
            models.Index(fields=["user", "created_at"], name="ask_user_created"),
            # GET ask/: this user's asks, newest id first (the cursor).
            models.Index(fields=["user", "-id"], name="ask_user_id"),
        ]

    def __str__(self):
        return f"Ask {self.pk} ({self.status})"

    @property
    def finished(self) -> bool:
        return self.status in (self.Status.DONE, self.Status.FAILED)
