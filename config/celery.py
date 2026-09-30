import os

from celery import Celery

# Must be set before the app is created: the worker is a separate process
# that never runs manage.py, so nothing else configures Django for it.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("super_notes")

# Every Celery option is read from a Django setting prefixed CELERY_, so
# CELERY_BROKER_URL configures broker_url. One settings file.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Imports tasks.py from every installed app. Without this the worker starts
# fine and then reports "unregistered task" at call time.
app.autodiscover_tasks()
