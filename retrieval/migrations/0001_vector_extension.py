"""Turn on pgvector, from a migration rather than by hand.

``CREATE EXTENSION`` needs a role allowed to create it (a superuser, or the
database owner on PostgreSQL 13+ for a trusted extension -- pgvector is
not one, so README.md grants superuser to the local development role).
Doing it here means a fresh database, CI's and the test database included,
gets it with ``migrate`` and nothing else.
"""

from django.db import migrations
from pgvector.django import VectorExtension


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [VectorExtension()]
