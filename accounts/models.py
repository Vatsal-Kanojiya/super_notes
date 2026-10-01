from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone as dj_timezone


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

    # Lifecycle (D88, D90, D94). ``timezone`` is an IANA name, validated where
    # it is written (accounts/api.py).
    timezone = models.CharField(max_length=64, default="Asia/Kolkata")
    memory_enabled = models.BooleanField(default=True)
    # True once the user has set ``memory_enabled`` themselves (D88): it picks
    # the prominent or the subtle memory notice.
    memory_choice_explicit = models.BooleanField(default=False)
    # App opens counted so far, and the count when the user last saw the
    # memory notice: the notice is due every MEMORY_NOTICE_EVERY_OPENS opens.
    app_open_count = models.PositiveIntegerField(default=0)
    memory_notice_seen_at_open = models.PositiveIntegerField(null=True, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=dj_timezone.now)

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


class SignedInDevice(models.Model):
    """One signed-in device: one refresh-token chain.

    At most ``MAX_SIGNED_IN_DEVICES`` of these exist per account; a further
    sign-in ends the oldest (DECISIONS D6). Written and pruned only through
    ``accounts.devices``, which keeps this table in step with the
    ``OutstandingToken`` rows it stands for.

    The reference also tracked web sessions (a ``kind`` column and a
    ``session_key``). Here the only session is the admin's, so a device is
    always an API refresh-token chain and neither column exists (D17).

    ``refresh_jti`` is the id of the chain's *current* refresh token: a
    refresh rotates the token and moves this row to the new one, so a device
    keeps one row for its whole life. It is how the row finds the token to
    revoke, and is never shown to a client.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="signed_in_devices"
    )
    refresh_jti = models.CharField(max_length=255, db_index=True)
    created_at = models.DateTimeField(default=dj_timezone.now)
    # Moved on every refresh, so "oldest" means least recently used, not
    # first signed in: a phone used every day outlives a laptop left idle.
    last_seen_at = models.DateTimeField(default=dj_timezone.now)
    # The User-Agent, truncated: enough for a person to tell their phone
    # from their laptop. Untrusted text -- a client must escape it.
    label = models.CharField(max_length=200, blank=True)
    # When this device's last *counted* app open was (D90, D93): the per-device
    # throttle on ``app_opened`` reads it. Null: never opened.
    last_app_open_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        # The foreign key's own index covers "this user's devices".
        ordering = ["last_seen_at", "id"]

    def __str__(self):
        return f"Device {self.pk} for user {self.user_id}"


class PushSubscription(models.Model):
    """One browser's web push subscription, for reminder notifications (D87).

    ``endpoint`` is unique across all users: a browser profile is one
    subscription, and if a different account signs in on it the row moves to
    that account (accounts/api.py). ``p256dh`` and ``auth`` are the keys the
    payload is encrypted to; treat them as secrets and never show them back.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_subscriptions"
    )
    endpoint = models.TextField(unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    # Untrusted text, truncated: lets a person tell their browsers apart.
    user_agent = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=dj_timezone.now)
    last_success_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"Push subscription {self.pk} for user {self.user_id}"
