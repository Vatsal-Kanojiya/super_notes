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
    "retrieval",
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
CELERY_BEAT_SCHEDULE = {}


# Upload size
#
# Nothing here accepts files; the largest body is a note, whose serialized
# TipTap content the serializer caps at NOTE_CONTENT_MAX_BYTES. The request
# limit leaves headroom above that for the title and the JSON around it,
# and MaxUploadSizeMiddleware refuses anything larger from the header alone.
NOTE_CONTENT_MAX_BYTES = 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = NOTE_CONTENT_MAX_BYTES + 512 * 1024


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
CORS_EXPOSE_HEADERS = ["X-Request-ID"]


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
