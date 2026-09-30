from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    """Users are keyed by email; there is no username.

    People sign in with Google and never have a usable password
    (accounts/google.py). ``create_superuser`` is the one path that sets
    one -- for the admin, from ``createsuperuser``.
    """

    use_in_migrations = True

    def _create(self, email, password, **extra):
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra["is_staff"] = True
        extra["is_superuser"] = True
        return self._create(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    """The account. Custom from the first migration (the reference's hard rule 1).

    ``notes_revision`` is the per-user write counter behind sync: every note
    write locks this row, increments it and stamps the note with the new
    value, so ``GET notes/changes/?after=<n>`` is exact. See DECISIONS D5.
    """

    class Plan(models.TextChoices):
        FREE = "free", "Free"
        PREMIUM = "premium", "Premium"

    email = models.EmailField(unique=True)
    # Google's stable subject id. Email can change on Google's side; this
    # cannot, so a repeat sign-in is matched on it first (accounts/google.py).
    google_sub = models.CharField(max_length=255, unique=True, null=True, blank=True)
    name = models.CharField(max_length=150, blank=True)
    avatar_url = models.URLField(max_length=500, blank=True)
    # Edited in the admin in V1; there are no payments (docs/BACKLOG.md).
    plan = models.CharField(max_length=10, choices=Plan.choices, default=Plan.FREE)
    notes_revision = models.BigIntegerField(default=0)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = "user"
        verbose_name_plural = "users"

    def __str__(self):
        return self.email


class SecurityEvent(models.Model):
    """One row per security-relevant thing that happened to an account.

    Carried over from the reference: sign-ins and failures, sign-ups,
    sign-outs, devices ended by the limit, attempts refused by the rate
    limit. Written through ``accounts.audit.record``, never directly: that
    function is what guarantees a failure here never breaks the request it
    rides along with, and that nothing secret ends up in ``detail``.

    ``user`` is SET_NULL rather than CASCADE, so a deleted account's events
    stay in the trail. ``email`` is a snapshot, not a live lookup: a failed
    sign-in may name no account at all. Rows are pruned only by
    ``purge_security_events``, after ``SECURITY_EVENT_RETENTION_DAYS``.
    """

    class Event(models.TextChoices):
        GOOGLE_LOGIN_SUCCEEDED = "google_login_succeeded", "Google sign-in succeeded"
        GOOGLE_LOGIN_FAILED = "google_login_failed", "Google sign-in failed"
        LOGIN_BLOCKED = "login_blocked", "Sign-in blocked (rate limited)"
        SIGNED_UP = "signed_up", "Signed up"
        LOGGED_OUT = "logged_out", "Logged out"
        DEVICE_SIGNED_OUT = "device_signed_out", "Device signed out"

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    event = models.CharField(max_length=32, choices=Event.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="security_events",
    )
    email = models.CharField(max_length=254, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    # Small, structured context, e.g. {"reason": "inactive"}. Never a token,
    # a claim set or anything else secret -- see accounts/audit.py.
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["event", "created_at"])]

    def __str__(self):
        who = self.email or (self.user_id and f"user {self.user_id}") or "unknown"
        return f"{self.get_event_display()} — {who} @ {self.created_at:%Y-%m-%d %H:%M}"
