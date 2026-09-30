from django.apps import AppConfig


class ConfigConfig(AppConfig):
    """Registers the project-wide system checks (config/checks.py)."""

    name = "config"

    def ready(self):
        from . import checks  # noqa: F401  (registers the checks)
