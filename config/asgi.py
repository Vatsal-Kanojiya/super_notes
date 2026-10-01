"""The ASGI entry point: ``uvicorn config.asgi:application`` (DECISIONS D92, D376).

Needed only for ``GET ask/<id>/stream/``, whose open streams then hold a
coroutine each instead of a worker thread; every other endpoint is
synchronous and works under ``runserver`` (WSGI) just the same.

With DEBUG on, static files (the admin's, the Swagger UI's) are served too,
as ``runserver`` does, so uvicorn can stand in for it in development.
"""

import os

from django.conf import settings
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_asgi_application()

if settings.DEBUG:
    from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler

    application = ASGIStaticFilesHandler(application)
