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
