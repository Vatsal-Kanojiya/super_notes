"""Limits and the usage ledger (DECISIONS D84, D91).

A *limit key* names one capped resource (``chat_turns``, ``storage_bytes``,
``signups``...). Its values live in settings.LIMIT_DEFAULTS; a ``Limit`` row
with the same key overrides all of them, so the admin can change a limit
without a deploy. Usage is never a counter: it is the sum of the
non-refunded ``UsageEvent`` rows in the period, so there is one source of
truth and nothing to drift. limits/service.py is the only writer.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class Period(models.TextChoices):
    # Calendar periods in settings.TIME_ZONE, the users' own.
    MONTH = "month", "Calendar month"
    DAY = "day", "Calendar day"
    TOTAL = "total", "All time"


class Limit(models.Model):
    """An admin override of one key's defaults. Every field is used as is.

    Null means unlimited at that level: a system-only key (``signups``) has
    no per-user values, and a key with no system value is never paused for
    everyone. ``enabled`` off stops enforcing the key altogether -- usage is
    still recorded, so turning it back on counts what happened meanwhile.
    """

    key = models.SlugField(max_length=50, unique=True)
    user_free = models.PositiveBigIntegerField(null=True, blank=True)
    user_premium = models.PositiveBigIntegerField(null=True, blank=True)
    system = models.PositiveBigIntegerField(null=True, blank=True)
    period = models.CharField(max_length=5, choices=Period.choices)
    enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return self.key

    def clean(self):
        # A row for a key no code consumes would silently limit nothing.
        if self.key and self.key not in settings.LIMIT_DEFAULTS:
            raise ValidationError({"key": f"Unknown limit key {self.key!r}."})


class UsageEvent(models.Model):
    """One use of a limited resource, of ``amount`` units (1 ask, N bytes).

    ``user`` is null for system-only keys (a sign-up has no account yet),
    and becomes null when an account is deleted: the event keeps counting
    against the system limit, so deleting accounts cannot reset it.
    A refunded event (a failed ask) no longer counts anywhere.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="usage_events",
    )
    key = models.SlugField(max_length=50)
    amount = models.PositiveBigIntegerField(default=1)
    refunded = models.BooleanField(default=False)
    # Deleting an ask must not hand its use back.
    ask = models.ForeignKey(
        "assistant.AskQuery",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="usage_events",
    )
    provider = models.CharField(max_length=20, blank=True)
    model = models.CharField(max_length=100, blank=True)
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    # Set by consume() to the instant it checked the limit against, so an
    # event made across a period boundary lands in the period it was
    # counted in.
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(fields=["user", "key", "created_at"], name="usage_user_key_created"),
            models.Index(fields=["key", "created_at"], name="usage_key_created"),
        ]

    def __str__(self):
        return f"{self.key} x{self.amount} ({self.user_id or 'system'})"
