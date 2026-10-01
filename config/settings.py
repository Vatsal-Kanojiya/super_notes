"""
Django settings for Super Notes.

One settings module, as in the reference (expense_management): every value
that differs between machines comes from the environment through
django-environ, with its type and a safe default declared here. See
docs/CONVENTIONS.md for what was carried over and why.
"""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

# Declaring types and defaults here means a missing or malformed value fails
# loudly at startup rather than silently becoming a string like "False"
# (which is truthy).
env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
)

# Read .env if present. It is gitignored; .env.example documents the keys.
environ.Env.read_env(BASE_DIR / ".env")

# No default on purpose: an unset SECRET_KEY should crash, not fall back to
# a shared value that silently ships to production.
SECRET_KEY = env("SECRET_KEY")

# Defaults to False so that forgetting to set it fails safe.
DEBUG = env("DEBUG")

ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# Where the admin site lives, with a trailing slash. Moving it off /admin/
# is not a defence on its own; it keeps scanner noise out of the logs.
ADMIN_URL = env("ADMIN_URL", default="admin/").strip("/") + "/"

# How many reverse proxies stand in front of the app. Each appends the
# address it received the request from to X-Forwarded-For, so the
# trustworthy client address is that many entries from the *right*. 0 means
# no proxy: REMOTE_ADDR is the client. DRF's throttles and
# accounts/ratelimit.py both read the address this way.
TRUSTED_PROXY_COUNT = env.int(
    "TRUSTED_PROXY_COUNT", default=1 if env.bool("USE_X_FORWARDED_PROTO", default=False) else 0
)


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "drf_spectacular",
    # Project-wide system checks (config/checks.py).
    "config.apps.ConfigConfig",
    "accounts",
    "notes",
    "retrieval",
    "assistant",
    "limits",
]

# Custom user model from the very first migration (the reference's hard
# rule 1). Swapping it afterwards is a painful data migration.
AUTH_USER_MODEL = "accounts.User"

# Swaps in fast, test-only settings for the suite. See its docstring.
TEST_RUNNER = "config.test_runner.FastTestRunner"


MIDDLEWARE = [
    # First, so nothing else gets a chance to read an oversized body before
    # this rejects it from Content-Length alone.
    "config.middleware.MaxUploadSizeMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Beside SecurityMiddleware, which sets the other security headers.
    "config.middleware.ContentSecurityPolicyMiddleware",
    # Before anything that can answer a request itself (CommonMiddleware's
    # redirects), so those answers carry CORS headers too.
    "corsheaders.middleware.CorsMiddleware",
    # Before everything that can log, so the id is set by the time any
    # other middleware, view or exception handler emits a line.
    "config.middleware.RequestIDMiddleware",
    # Tells a running client its build is too old (D106).
    "config.middleware.ClientMinVersionMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

# Only the admin and the Swagger UI render templates; the product UI is the
# Vue client in web/.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Database
#
# PostgreSQL with pgvector, and nothing else (DECISIONS D2). No default:
# the reference fell back to SQLite, but vector search, the GIN full-text
# indexes and select_for_update all need Postgres, so a missing
# DATABASE_URL should crash at startup rather than half-work.
# config/checks.py refuses any other engine.
DATABASES = {"default": env.db_url("DATABASE_URL")}


# Password validation
#
# People sign in with Google only and have no usable password. These still
# guard the one password that exists: a superuser's, for the admin.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# Internationalization

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True


# Static files (the admin's and the Swagger UI's only)

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Celery
# https://docs.celeryq.dev/en/stable/django/first-steps-with-django.html

# Separate Redis databases: a flushed result backend must not take the
# pending task queue with it.
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/1")

# JSON only. Celery's pickle serializer executes arbitrary code on
# deserialisation, so anyone who can write to the broker would get remote
# code execution on every worker.
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]

CELERY_TIMEZONE = TIME_ZONE

# Acknowledge a task only after it finishes. A worker killed mid-task has
# the task redelivered instead of lost -- so every task must be idempotent.
CELERY_TASK_ACKS_LATE = True

# With acks_late, a worker that dies redelivers everything it prefetched.
# One at a time keeps that blast radius small.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

# A hung task holds a worker forever without these. The soft limit raises
# an exception the task can clean up after; the hard limit kills it.
CELERY_TASK_SOFT_TIME_LIMIT = 60 * 5
CELERY_TASK_TIME_LIMIT = 60 * 10

# Run tasks inline. The test runner turns this on; never in production,
# where it would make every background job block the request.
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TASK_EAGER_PROPAGATES = True

# Periodic jobs. Each is harmless to run twice.
CELERY_BEAT_SCHEDULE = {
    # Daily: the trail changes slowly, and hourly would be wasted work.
    "purge-old-security-events": {
        "task": "accounts.tasks.purge_security_events",
        "schedule": 24 * 60 * 60,
    },
    # Every refresh leaves a token row behind; expired ones prove nothing.
    "flush-expired-tokens": {
        "task": "accounts.tasks.flush_expired_tokens",
        "schedule": 24 * 60 * 60,
    },
    # Often, because a stuck ask is a spinner someone is watching; the
    # query is one indexed-range UPDATE and does nothing when all is well.
    "sweep-stuck-asks": {
        "task": "assistant.tasks.sweep_stuck_asks",
        "schedule": 5 * 60,
    },
    # The same net for "format my note" jobs (notes/tasks.py).
    "sweep-stuck-format-jobs": {
        "task": "notes.tasks.sweep_stuck_format_jobs",
        "schedule": 5 * 60,
    },
    # And for attachments whose text extraction never finished.
    "sweep-stuck-attachments": {
        "task": "notes.tasks.sweep_stuck_attachments",
        "schedule": 5 * 60,
    },
    # Expired dynamic facts and old superseded ones (assistant/memory.py, D406).
    "purge-expired-facts": {
        "task": "assistant.tasks.purge_expired_facts",
        "schedule": 24 * 60 * 60,
    },
    # Reminders notify on the minute (D95). A sweep claims each occurrence
    # once (notes/delivery.py), so an overlapping or repeated run is
    # harmless; one that waited past the next run is dropped.
    "deliver-due-reminders": {
        "task": "notes.tasks.deliver_due_reminders",
        "schedule": 60,
        "options": {"expires": 55},
    },
}

# A reminder occurrence missed by more than this (the worker was down) is
# not sent late at all (DECISIONS D136).
REMINDER_MISSED_GRACE_HOURS = env.int("REMINDER_MISSED_GRACE_HOURS", default=24)


# Upload size
#
# Apart from attachment uploads (below), the largest body is a note, whose
# serialized TipTap content the serializer caps at NOTE_CONTENT_MAX_BYTES.
# The request limit leaves headroom above that for the title and the JSON
# around it, and MaxUploadSizeMiddleware refuses anything larger from the
# header alone.
NOTE_CONTENT_MAX_BYTES = 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = NOTE_CONTENT_MAX_BYTES + 512 * 1024

# Attachments (notes/attachments.py, DECISIONS D85, D91, D320-D330). The one
# endpoint that takes a file gets its own, larger body limit: one file of up
# to ATTACHMENT_MAX_BYTES plus the multipart envelope around it (boundaries,
# part headers, the file name). MaxUploadSizeMiddleware looks the request's
# view up only when a body is over DATA_UPLOAD_MAX_MEMORY_SIZE, and allows
# it only for a view named here, on POST.
ATTACHMENT_MAX_BYTES = env.int("ATTACHMENT_MAX_BYTES", default=10 * 1024 * 1024)
UPLOAD_SIZE_ALLOWANCES = {"api:v1:note-attachments": ATTACHMENT_MAX_BYTES + 64 * 1024}

# Text extraction (notes/extraction.py, DECISIONS D340-D349). After an upload
# a worker reads the file's text -- a PDF's with pypdf, an image's through the
# chat provider's vision call -- and indexes it for search and Ask. Every cap
# bounds what one hostile file can cost: a PDF's pages read, the text kept,
# the bytes any one compressed stream may inflate to (a "zip bomb" in a PDF),
# and the task's own time.
ATTACHMENT_PDF_MAX_PAGES = env.int("ATTACHMENT_PDF_MAX_PAGES", default=100)
# About 25,000 tokens, or some 60 chunks to embed; the rest of a longer file
# is not searched.
ATTACHMENT_TEXT_MAX_CHARS = env.int("ATTACHMENT_TEXT_MAX_CHARS", default=100_000)
# pypdf's own ceiling is 75 MB per stream; a page's text needs far less.
ATTACHMENT_PDF_MAX_STREAM_BYTES = env.int(
    "ATTACHMENT_PDF_MAX_STREAM_BYTES", default=20 * 1024 * 1024
)
# The largest image sent to the vision call. Claude takes at most 5 MB per
# image; a larger one fails with a clear message instead of a vendor 400.
ATTACHMENT_IMAGE_TEXT_MAX_BYTES = env.int(
    "ATTACHMENT_IMAGE_TEXT_MAX_BYTES", default=5 * 1024 * 1024
)
# The ceiling on an image's transcribed text (about 16,000 characters).
ATTACHMENT_IMAGE_TEXT_MAX_OUTPUT_TOKENS = env.int(
    "ATTACHMENT_IMAGE_TEXT_MAX_OUTPUT_TOKENS", default=4096
)
# The extraction task's own limits, tighter than the default task's: the soft
# one fails the attachment cleanly, the hard one kills a stuck parser.
ATTACHMENT_EXTRACT_SOFT_TIME_LIMIT = env.int("ATTACHMENT_EXTRACT_SOFT_TIME_LIMIT", default=120)
ATTACHMENT_EXTRACT_TIME_LIMIT = env.int("ATTACHMENT_EXTRACT_TIME_LIMIT", default=180)
# An attachment still pending or extracting this long after its upload is
# failed by notes.tasks.sweep_stuck_attachments: a lost task message, or a
# worker killed at the hard limit. It outlasts every retry (6 attempts of at
# most 180 s, with at most 10 minutes of backoff between them).
ATTACHMENT_STUCK_AFTER_SECONDS = env.int("ATTACHMENT_STUCK_AFTER_SECONDS", default=60 * 60)


# File storage (DECISIONS D85, D320)
#
# Attachments live in their own storage alias. With AWS_STORAGE_BUCKET_NAME
# set it is S3 (django-storages): a private bucket, objects written without
# an ACL (the bucket's own policy decides, and it should block public
# access), never served by URL. boto3 reads its credentials from its usual
# chain (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, an instance role...),
# never a setting here. Without a bucket, local disk under MEDIA_ROOT, which
# no URL serves: there is no MEDIA_URL route, and the only way to a file's
# bytes is the owner-scoped download endpoint. Either way the stored name is
# random, never the client's. The test runner always swaps in a temp dir.
MEDIA_ROOT = env("MEDIA_ROOT", default=str(BASE_DIR / "media"))
AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME", default="")
if AWS_STORAGE_BUCKET_NAME:
    _attachment_storage = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": AWS_STORAGE_BUCKET_NAME,
            "region_name": env("AWS_S3_REGION_NAME", default="") or None,
            # For an S3-compatible service (MinIO, R2...); empty is AWS itself.
            "endpoint_url": env("AWS_S3_ENDPOINT_URL", default="") or None,
            "default_acl": None,
            "querystring_auth": True,
            "file_overwrite": False,
            "location": env("AWS_S3_LOCATION", default=""),
        },
    }
else:
    _attachment_storage = {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        # 0o600/0o700: readable by the app's own user only.
        "OPTIONS": {
            "location": MEDIA_ROOT,
            "file_permissions_mode": 0o600,
            "directory_permissions_mode": 0o700,
        },
    }
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    "attachments": _attachment_storage,
}


# Security
# https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/
#
# Gated on DEBUG because every one of these breaks local development: SSL
# redirect makes http://localhost unreachable, and secure cookies are not
# sent over plain HTTP.
if not DEBUG:
    # Start low: browsers cache this, and a wrong value with preload set
    # makes the domain unreachable over HTTP for up to a year. Raise it to
    # 31536000 (ASVS V3.4.1) once HTTPS is known stable -- DECISIONS D9.
    SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=3600)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True)
    # Opt-in: preload is a browser-baked list that is slow to leave.
    SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=False)

    SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)

    # The session and CSRF cookies exist only for the admin; the API uses
    # bearer tokens. Never over plain HTTP, and (ASVS V3.3.1) carrying the
    # __Host- prefix so a browser refuses them unless Secure is really set.
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    CSRF_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_NAME = "__Host-sessionid"
    CSRF_COOKIE_NAME = "__Host-csrftoken"

    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"

    # W021 warns that HSTS preload is off. Deliberate, as in the reference:
    # preloading is a decision for a specific deployment.
    SILENCED_SYSTEM_CHECKS = ["security.W021"]

    # Set only when the proxy is trusted to strip a client-supplied version
    # of this header, or anyone can forge "I am on HTTPS".
    if env.bool("USE_X_FORWARDED_PROTO", default=False):
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Who a production exception is mailed to: "Name:address" pairs,
# comma-separated. Empty by default, so nobody is mailed until it is set.
ADMINS = [
    tuple(entry.split(":", 1)) if ":" in entry else (entry, entry)
    for entry in env.list("ADMINS", default=[])
    if entry
]
EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")
SERVER_EMAIL = env("SERVER_EMAIL", default="no-reply@super-notes.local")
# The sender of mail to users (reminders); SERVER_EMAIL is for admin mail.
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default=SERVER_EMAIL)

# Where the web client is served, for links in mail ("open the note").
# No trailing slash.
WEB_APP_URL = env("WEB_APP_URL", default="http://localhost:5173").rstrip("/")

# Web push (reminders, D87). Push is on only when both keys are set; make
# a pair with ``manage.py generate_vapid_keys``. The subject is a mailto: or
# https: contact the push service can reach.
VAPID_PUBLIC_KEY = env("VAPID_PUBLIC_KEY", default="")
VAPID_PRIVATE_KEY = env("VAPID_PRIVATE_KEY", default="")
# Hosts a push endpoint may point at: the server POSTs to it, so an open
# list would be an SSRF hole. Exact host, or ``*.suffix`` for subdomains.
PUSH_ENDPOINT_HOSTS = env.list(
    "PUSH_ENDPOINT_HOSTS",
    default=[
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
        "*.push.services.mozilla.com",
        "*.notify.windows.com",
        "web.push.apple.com",
        "*.push.apple.com",
    ],
)
VAPID_SUBJECT = env("VAPID_SUBJECT", default=f"mailto:{SERVER_EMAIL}")


# Logging
#
# Every line carries the request id (config/middleware.py), so
# `grep <id>` reads as a transcript of one request.
LOG_LEVEL = env("LOG_LEVEL", default="INFO")
LOGGING = {
    "version": 1,
    # Never True: it would kill Django's own error mail and security logging.
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {"()": "config.middleware.RequestIDFilter"},
        "require_debug_false": {"()": "django.utils.log.RequireDebugFalse"},
    },
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} [{request_id}] {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["request_id"],
        },
        # Mails a 5xx traceback to ADMINS through SafeExceptionReporterFilter,
        # so anything marked @sensitive_variables is starred out.
        "mail_admins": {
            "level": "ERROR",
            "filters": ["require_debug_false"],
            "class": "django.utils.log.AdminEmailHandler",
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.request": {
            "handlers": ["console", "mail_admins"],
            "level": "WARNING",
            "propagate": False,
        },
        "django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}


# Django REST Framework
REST_FRAMEWORK = {
    # DRF's three defaults, with the JSON one replaced by a parser that
    # answers 400 to a pathologically nested body (DECISIONS D79).
    "DEFAULT_PARSER_CLASSES": [
        "config.api.parsers.JSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    # Bearer tokens only. The reference kept SessionAuthentication for its
    # server-rendered pages; this project has none, and leaving it on would
    # bring CSRF into every API call made from an admin-logged-in browser
    # for no benefit (DECISIONS D4).
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    # Deny by default: a forgotten permission_classes stays closed.
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    # Cursor, not page number: stable while rows are being written.
    "DEFAULT_PAGINATION_CLASS": "config.api.pagination.IdCursorPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    # Generous enough for an editor that autosaves and polls, low enough to
    # make a runaway script visible. The scoped rates sit on the endpoints
    # that cost something: sign-in (Google's keys, the user table), search
    # (an embedding call) and ask (an LLM call).
    "DEFAULT_THROTTLE_RATES": {
        "user": env("API_USER_THROTTLE", default="3000/hour"),
        "anon": env("API_ANON_THROTTLE", default="60/hour"),
        "auth": env("API_AUTH_THROTTLE", default="30/hour"),
        "search": env("API_SEARCH_THROTTLE", default="120/hour"),
        "ask": env("API_ASK_THROTTLE", default="60/hour"),
        "format": env("API_FORMAT_THROTTLE", default="30/hour"),
        # Attachment uploads: each costs storage and, later, an extraction.
        "upload": env("API_UPLOAD_THROTTLE", default="120/hour"),
    },
    # Unset, DRF identifies an anonymous caller by the whole X-Forwarded-For
    # header -- which the caller writes.
    "NUM_PROXIES": TRUSTED_PROXY_COUNT,
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.NamespaceVersioning",
    "DEFAULT_VERSION": "v1",
    "ALLOWED_VERSIONS": ["v1"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # Every error as {"detail", "code"}.
    "EXCEPTION_HANDLER": "config.api.exceptions.exception_handler",
}


# Bearer tokens
#
# Short-lived access tokens, so a leaked one expires quickly. The refresh
# token rotates on every use and the old one is blacklisted, so a stolen
# refresh token stops working the moment the real client refreshes, and
# logout revokes for real.
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env.int("JWT_ACCESS_MINUTES", default=30)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env.int("JWT_REFRESH_DAYS", default=14)),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}


# Cross-origin access
#
# An explicit list, never a wildcard. Only /api/ is opened, and cookies are
# not allowed cross-origin -- tokens travel in the Authorization header,
# which is also why a cross-origin request needs no CSRF token.
# Example: CORS_ALLOWED_ORIGINS=http://localhost:5173 for the Vite server.
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
CORS_URLS_REGEX = r"^/api/.*$"
CORS_ALLOW_CREDENTIALS = False
# The Ask endpoint requires this header; a browser strips an unlisted one
# from a cross-origin request's preflight.
CORS_ALLOW_HEADERS = (
    "accept",
    "authorization",
    "content-type",
    "idempotency-key",
    "x-request-id",
)
CORS_EXPOSE_HEADERS = ["X-Request-ID", "X-Client-Min-Version"]


# The OpenAPI schema and Swagger UI at /api/v1/schema/ and /api/v1/docs/.
SPECTACULAR_SETTINGS = {
    "TITLE": "Super Notes API",
    "DESCRIPTION": (
        "Notes that sync across devices, and answers to questions about them with citations. "
        "Sign in with `POST /api/v1/auth/google/` and send `Authorization: Bearer <access>`."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    # Separate request and response components, so a generated client knows
    # that `id` is never sent.
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": r"/api/v1",
    "SERVE_PERMISSIONS": ["rest_framework.permissions.AllowAny"],
    # Four models have a `status`: asks and format jobs share one choice set
    # (StatusEnum); a reminder's and an attachment's are their own.
    "ENUM_NAME_OVERRIDES": {
        "StatusEnum": "assistant.models.AskQuery.Status",
        "ReminderStatusEnum": "notes.models.Reminder.Status",
        "AttachmentStatusEnum": "notes.models.Attachment.Status",
        # Two `kind`s: an app-open notice's keeps the name it had first.
        "KindEnum": ["update", "memory"],
        "FactKindEnum": "assistant.models.UserFact.Kind",
        "ChunkSourceEnum": "retrieval.models.NoteChunk.Source",
    },
}


# Cache
#
# The rate limits (accounts/ratelimit.py) count here. LocMemCache is per
# process, which is fine for runserver and wrong under several workers --
# check --deploy warns about it (config/checks.py). Set CACHE_URL to Redis
# in production, e.g. rediscache://127.0.0.1:6379/2.
CACHES = {"default": env.cache_url("CACHE_URL", default="locmemcache://super-notes")}


# Sign in with Google
#
# Every OAuth client id whose ID tokens are accepted: the web client's and
# the Android app's (they differ). Not secrets -- they identify the app to
# Google. Empty turns sign-in off.
GOOGLE_OAUTH_CLIENT_IDS = env.list("GOOGLE_OAUTH_CLIENT_IDS", default=[])


# Asking (assistant/)
#
# "fake" needs no API key and no network, so a fresh clone -- and CI --
# answers questions with nothing configured; the test runner forces it
# whatever .env says (DECISIONS D11). Each real provider reads its key from
# the vendor's usual env var (ANTHROPIC_API_KEY, GEMINI_API_KEY,
# OPENAI_API_KEY), never a CHAT_* setting: a key is a secret, not app
# configuration, and this keeps it out of this file entirely.
CHAT_PROVIDER = env("CHAT_PROVIDER", default="fake")
# The cheap tier of each vendor (DECISIONS D55): answering from a handful of
# excerpts is reading comprehension, not open-ended reasoning.
CHAT_MODELS = {
    "claude": env("CHAT_CLAUDE_MODEL", default="claude-haiku-4-5"),
    "gemini": env("CHAT_GEMINI_MODEL", default="gemini-2.5-flash-lite"),
    "openai": env("CHAT_OPENAI_MODEL", default="gpt-5-mini"),
}
# A ceiling on the answer, which is a few cited sentences. OpenAI counts its
# reasoning tokens against this too, so it is not set tighter.
CHAT_MAX_OUTPUT_TOKENS = env.int("CHAT_MAX_OUTPUT_TOKENS", default=2048)
# Read timeout of one provider call, in seconds -- for a streamed answer, the
# longest wait between two pieces of it. A timeout is transient (retried);
# CELERY_TASK_SOFT_TIME_LIMIT still bounds the whole task.
CHAT_TIMEOUT_SECONDS = env.int("CHAT_TIMEOUT_SECONDS", default=60)

# Streaming answers (assistant/events.py, DECISIONS D363-D366). The answer
# task publishes its progress on Redis pub/sub, channel ask:<id>; empty turns
# that off, and polling works either way. Pub/sub ignores the database
# number, so the broker's Redis is the natural default.
ASK_EVENTS_REDIS_URL = env("ASK_EVENTS_REDIS_URL", default=CELERY_BROKER_URL)
# The text streamed so far is saved on the ask (partial_answer) at most this
# often, so a reader that connects mid-answer can catch up from the row.
ASK_PARTIAL_SAVE_SECONDS = env.float("ASK_PARTIAL_SAVE_SECONDS", default=0.5)
# GET ask/<id>/stream/ (assistant/stream.py, DECISIONS D370-D377), served
# under uvicorn. A stream closes with a "timeout" event after this long, and
# the client goes on by polling: the answer task can take minutes, but an
# open stream should not outlive it by much.
ASK_STREAM_MAX_SECONDS = env.float("ASK_STREAM_MAX_SECONDS", default=300)
# A comment line after this long without output, so a proxy keeps it open.
ASK_STREAM_HEARTBEAT_SECONDS = env.float("ASK_STREAM_HEARTBEAT_SECONDS", default=15)
# How often an open stream reads its ask again, so one that ends without an
# event (swept as stuck, or the worker could not reach Redis) still ends it.
ASK_STREAM_RECHECK_SECONDS = env.float("ASK_STREAM_RECHECK_SECONDS", default=10)
# Streams one user may have open at once, counted in ASK_EVENTS_REDIS_URL's
# Redis (DECISIONS D511); one more is a 429 too_many_streams. 0 turns the cap off.
STREAM_MAX_PER_USER = env.int("STREAM_MAX_PER_USER", default=3)

# An ask still pending or running this long after it was made is failed by
# assistant.tasks.sweep_stuck_asks. It must outlast every way a live ask
# can still be working: answer_ask makes up to 5 attempts (max_retries=4),
# each bounded by CELERY_TASK_TIME_LIMIT (600 s), with at most 1+2+4+8 s of
# backoff between them -- 3,015 s. One hour leaves about 10 minutes for
# queueing behind a backlog, so a slow ask is never failed under a worker.
ASK_STUCK_AFTER_SECONDS = env.int("ASK_STUCK_AFTER_SECONDS", default=60 * 60)
# A conversation turn still *pending* -- never claimed by a worker -- this
# long after it was made is presumed lost (its Celery message never
# arrived), and the next turn of its conversation fails and refunds it
# instead of answering 409 for the hour above (DECISIONS D505). A worker
# claims a turn within seconds; this leaves room for a short backlog. A
# turn a worker has claimed (running) still blocks.
TURN_PENDING_STALE_SECONDS = env.int("TURN_PENDING_STALE_SECONDS", default=120)

# Format my note (notes/format_*.py, tasks.py; DECISIONS D240-D249). Uses per month
# are the `format` limit in LIMIT_DEFAULTS.
#
# The guardrail on a formatted note: the share of the original's words the
# result keeps (recall), and the share of the result's words that were in
# the original (precision, looser so a new heading or two is allowed).
FORMAT_MIN_WORDS_KEPT = env.float("FORMAT_MIN_WORDS_KEPT", default=0.9)
FORMAT_MIN_WORDS_ORIGINAL = env.float("FORMAT_MIN_WORDS_ORIGINAL", default=0.8)
# The note's TipTap JSON, compact, must fit in this many characters (about
# 6,000 tokens); a longer note is refused up front rather than cut off.
FORMAT_MAX_INPUT_CHARS = env.int("FORMAT_MAX_INPUT_CHARS", default=24000)
# The result is the whole document again, in JSON, so the ceiling is far
# above CHAT_MAX_OUTPUT_TOKENS (a few cited sentences).
FORMAT_MAX_OUTPUT_TOKENS = env.int("FORMAT_MAX_OUTPUT_TOKENS", default=16384)
# As ASK_STUCK_AFTER_SECONDS, for format jobs (same retry budget).
FORMAT_STUCK_AFTER_SECONDS = env.int("FORMAT_STUCK_AFTER_SECONDS", default=60 * 60)

# Asks per month are the chat_turns limit in LIMIT_DEFAULTS (DECISIONS D101).
# How many chunks retrieval hands the prompt.
ASK_RETRIEVAL_K = env.int("ASK_RETRIEVAL_K", default=8)
# Below this retrieval score nothing counts as relevant, and the ask is
# answered with ASK_NO_ANSWER_TEXT without calling the provider. Its
# meaning depends on the score retrieval returns (a fused RRF score and a
# cosine similarity live on very different scales), so 0.0 -- "only an
# empty result short-circuits" -- is a placeholder until the evaluation
# numbers of Phases 4/5 tune it (DECISIONS D58).
ASK_RELEVANCE_FLOOR = env.float("ASK_RELEVANCE_FLOOR", default=0.0)
# The fixed answer when the notes hold nothing relevant.
ASK_NO_ANSWER_TEXT = env(
    "ASK_NO_ANSWER_TEXT", default="I couldn't find anything in your notes about this."
)
# Total excerpt text sent in one prompt (assistant/prompt.py): about 3,000
# tokens at four characters a token -- eight chunks of the chunker's target
# size, with headroom. The cost of an ask is mostly this.
ASK_EXCERPT_MAX_CHARS = env.int("ASK_EXCERPT_MAX_CHARS", default=12000)

# Conversations (assistant/conversation.py, DECISIONS D220-D227)
#
# The earlier turns a conversation turn's prompt repeats verbatim, newest
# kept, in characters (about 1,500 tokens): on top of the excerpts, so a
# turn costs at most about half as much again as a plain ask.
CHAT_HISTORY_MAX_CHARS = env.int("CHAT_HISTORY_MAX_CHARS", default=6000)
# The same for the condense call, which only needs what a follow-up can
# point back to: the last turn or two.
CHAT_CONDENSE_HISTORY_MAX_CHARS = env.int("CHAT_CONDENSE_HISTORY_MAX_CHARS", default=2000)
# The ceiling on a condensed question. A question is a sentence; this is
# not tighter because OpenAI's reasoning tokens count against it too.
CHAT_CONDENSE_MAX_OUTPUT_TOKENS = env.int("CHAT_CONDENSE_MAX_OUTPUT_TOKENS", default=512)
# Folding (assistant/conversation.py, DECISIONS D280-D287): when the turns the
# summary does not cover grow past CHAT_HISTORY_MAX_CHARS, the oldest are
# folded into Conversation.summary. The summary is cut to this many
# characters (about 375 tokens) whatever the model wrote: it rides in every
# later prompt, so it must not grow without limit.
CHAT_SUMMARY_MAX_CHARS = env.int("CHAT_SUMMARY_MAX_CHARS", default=1500)
# The ceiling on a folding call's reply; like the condenser's, generous
# because reasoning tokens count against it.
CHAT_SUMMARY_MAX_OUTPUT_TOKENS = env.int("CHAT_SUMMARY_MAX_OUTPUT_TOKENS", default=1024)

# User memory (assistant/memory.py, DECISIONS D400-D409). Extraction after a
# finished conversation turn is capped by the system-only memory_extract
# limit in LIMIT_DEFAULTS; memory_enabled off means no extraction at all.
#
# A dynamic fact ("is moving house this month") expires this many days after
# it was learned; the daily purge deletes it.
MEMORY_DYNAMIC_FACT_DAYS = env.int("MEMORY_DYNAMIC_FACT_DAYS", default=30)
# A superseded fact is kept this many days after the fact that replaced it,
# then purged with the expired ones (D406).
MEMORY_SUPERSEDED_RETENTION_DAYS = env.int("MEMORY_SUPERSEDED_RETENTION_DAYS", default=30)
# The user's existing facts an extraction call is shown (the most similar
# to the question), so it can update or supersede instead of repeating.
MEMORY_SIMILAR_FACTS = env.int("MEMORY_SIMILAR_FACTS", default=10)
# Operations one extraction reply may hold; a reply with more is dropped whole.
MEMORY_MAX_OPERATIONS = env.int("MEMORY_MAX_OPERATIONS", default=5)
# The ceiling on an extraction reply: a few short JSON operations, with room
# for reasoning tokens, as for the condenser.
MEMORY_EXTRACT_MAX_OUTPUT_TOKENS = env.int("MEMORY_EXTRACT_MAX_OUTPUT_TOKENS", default=1024)
# The user's live facts a conversation turn's prompt carries at most: the
# nearest to the (standalone) question (DECISIONS D421).
MEMORY_PROMPT_FACTS = env.int("MEMORY_PROMPT_FACTS", default=5)

# Chunking (retrieval/chunking.py, DECISIONS D33)
#
# Measured in characters, not tokens: a tokenizer would be a new dependency
# and differs per provider. English runs about 4 characters to a token, so
# the target 1600 is ~400 tokens (the middle of the plan's 300-500), the
# max 2000 is ~500 and the overlap ~50. Blocks are packed up to the target;
# only a single block longer than the max is ever cut, and no chunk's text
# is longer than the max.
CHUNK_TARGET_CHARS = env.int("CHUNK_TARGET_CHARS", default=1600)
CHUNK_MAX_CHARS = env.int("CHUNK_MAX_CHARS", default=2000)
CHUNK_OVERLAP_CHARS = env.int("CHUNK_OVERLAP_CHARS", default=200)

# Indexing (retrieval/tasks.py, DECISIONS D61)
#
# Seconds a note write waits before its index task runs. Autosave writes
# every few seconds while someone types; each write enqueues a task for its
# own version and all but the last find the note has moved on and stop
# before embedding anything. A delete ignores this and de-indexes at once.
INDEX_DEBOUNCE_SECONDS = env.int("INDEX_DEBOUNCE_SECONDS", default=20)


# Embeddings (retrieval/embeddings/, DECISIONS D36-D39)
#
# "fake" needs no key and no network, so a fresh clone -- and CI -- indexes
# and searches with nothing configured; the test runner forces it whatever
# .env says. Each real provider reads its key from its own env var
# (OPENAI_API_KEY, GEMINI_API_KEY), never an EMBEDDING_* setting: a key is
# a secret, not app configuration.
EMBEDDING_PROVIDER = env("EMBEDDING_PROVIDER", default="fake")
EMBEDDING_MODELS = {
    "openai": env("EMBEDDING_OPENAI_MODEL", default="text-embedding-3-small"),
    "gemini": env("EMBEDDING_GEMINI_MODEL", default="gemini-embedding-001"),
}
# Fixed by the chunk table's vector column: changing it (or the model)
# means a migration and a full re-index (D36). 1536 because both default
# models can produce it and pgvector's HNSW index takes at most 2000.
EMBEDDING_DIMENSIONS = env.int("EMBEDDING_DIMENSIONS", default=1536)
# Texts per provider call. Under both vendors' per-request limits (Gemini's
# is the tighter, 100), and small enough that a retried call redoes little.
EMBEDDING_BATCH_SIZE = env.int("EMBEDDING_BATCH_SIZE", default=64)

# Retrieval (retrieval/search.py, DECISIONS D66-D71)
#
# Chunks each leg (vector, keyword) contributes before fusion: deep enough
# that a chunk ranked modestly by both legs still surfaces, cheap at 50.
SEARCH_CANDIDATES = env.int("SEARCH_CANDIDATES", default=50)
# The reciprocal-rank-fusion constant from the original RRF paper; larger
# flattens the gap between rank 1 and rank 10.
SEARCH_RRF_K = env.int("SEARCH_RRF_K", default=60)
# At most this many chunks of one note in a result, so one long note on the
# topic cannot crowd every other note out of the top k.
SEARCH_MAX_CHUNKS_PER_NOTE = env.int("SEARCH_MAX_CHUNKS_PER_NOTE", default=2)
SEARCH_DEFAULT_K = env.int("SEARCH_DEFAULT_K", default=8)
SEARCH_MAX_K = 20
# HNSW's candidate list for the vector leg (pgvector's default is 40, below
# SEARCH_CANDIDATES). The owner filter runs after the index scan, so the
# list needs headroom for other users' chunks (D68). Never below
# SEARCH_CANDIDATES.
SEARCH_HNSW_EF_SEARCH = env.int("SEARCH_HNSW_EF_SEARCH", default=200)

# Account security (accounts/, DECISIONS D6)
#
# How long a security event is kept before purge_security_events removes
# it. Each row holds an address and an email: long enough to answer "what
# happened to my account", not for ever. The command's --days overrides it
# for a one-off run.
SECURITY_EVENT_RETENTION_DAYS = env.int("SECURITY_EVENT_RETENTION_DAYS", default=365)

# How many devices (refresh-token chains) an account may be signed in on at
# once. A further sign-in signs the least recently used one out
# (accounts/devices.py). Two covers the web client and the Android app.
MAX_SIGNED_IN_DEVICES = env.int("MAX_SIGNED_IN_DEVICES", default=2)


# Limits (limits/, DECISIONS D84, D91)
#
# Every capped resource is a key with a per-user value by plan and a
# system-wide value, over a period: "month" or "day" (calendar, in
# TIME_ZONE) or "total". None (or absent) is unlimited. A Limit row in the
# admin with the same key overrides all of a key's values, so changing one
# needs no deploy. condense, summarize_history, memory_extract and image_text are model calls
# the user never pays for; only the system caps them.
LIMIT_DEFAULTS = {
    "chat_turns": {"user_free": 20, "user_premium": 100, "system": 2000, "period": "month"},
    "format": {"user_free": 5, "user_premium": 25, "system": 500, "period": "month"},
    "summary": {"user_free": 2, "user_premium": 10, "system": 200, "period": "month"},
    "storage_bytes": {
        "user_free": 1024**3,
        "user_premium": 1024**3,
        "system": 20 * 1024**3,
        "period": "total",
    },
    "signups": {"system": 30, "period": "day"},
    "condense": {"system": 20000, "period": "month"},
    "summarize_history": {"system": 5000, "period": "month"},
    "memory_extract": {"system": 20000, "period": "month"},
    # Reading an image's text with the chat provider's vision call, one per
    # image attachment (notes/extraction.py, D344). The upload already costs
    # the user storage_bytes; this caps what images cost the service.
    "image_text": {"system": 5000, "period": "month"},
}

# App lifecycle (D88-D94, D106-D111). A build id is YYYYMMDDHHMM-<shortsha>;
# ids compare by their timestamp prefix. Set by the deploy; empty means
# "no opinion", so nothing is announced.
CLIENT_LATEST_VERSION = env("CLIENT_LATEST_VERSION", default="")
# Builds older than this are told to update at once, and every API response
# carries it as X-Client-Min-Version (config/middleware.py).
CLIENT_MIN_VERSION = env("CLIENT_MIN_VERSION", default="")
# The memory notice (D88) is due on a user's first app open, then every this
# many opens since they last saw it (D94).
MEMORY_NOTICE_EVERY_OPENS = env.int("MEMORY_NOTICE_EVERY_OPENS", default=5)
# A second app open from one device inside this window is not counted (D93).
APP_OPEN_MIN_INTERVAL_SECONDS = env.int("APP_OPEN_MIN_INTERVAL_SECONDS", default=300)
