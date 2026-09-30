"""System checks for what this project cannot work without.

The reference ran on SQLite or Postgres. This one cannot (DECISIONS D2):
pgvector, the GIN full-text indexes and select_for_update are all
Postgres-only, and on another engine the failure would come later and less
clearly -- a migration error, or a lock that silently does nothing.
"""

from django.conf import settings
from django.core.checks import Error, Warning, register

POSTGRES_ENGINES = ("django.db.backends.postgresql",)

PER_PROCESS_CACHES = (
    "django.core.cache.backends.locmem.LocMemCache",
    "django.core.cache.backends.dummy.DummyCache",
)


@register()
def postgres_required(app_configs, **kwargs):
    engine = settings.DATABASES.get("default", {}).get("ENGINE", "")
    if engine not in POSTGRES_ENGINES:
        return [
            Error(
                f"Super Notes needs PostgreSQL with pgvector; DATABASE_URL uses {engine!r}.",
                hint="Set DATABASE_URL=postgres://USER@HOST:PORT/super_notes (see README.md).",
                id="config.E001",
            )
        ]
    return []


@register(deploy=True)
def shared_cache_for_rate_limits(app_configs, **kwargs):
    """Every rate limit counts in the default cache; a per-process one
    gives each worker its own count, and a restart forgets them all."""
    backend = settings.CACHES.get("default", {}).get("BACKEND", "")
    if backend in PER_PROCESS_CACHES:
        return [
            Warning(
                "The default cache is not shared between processes, so rate limits "
                "count per worker and reset on restart.",
                hint="Set CACHE_URL to a shared cache, e.g. rediscache://127.0.0.1:6379/2.",
                id="config.W001",
            )
        ]
    return []
