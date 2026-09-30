from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase

from notes import services
from notes.tests.helpers import doc, make_user
from retrieval import indexing
from retrieval.models import NoteChunk


class NoteChunkAdminTests(TestCase):
    def setUp(self):
        self.client.force_login(
            get_user_model().objects.create_superuser("root@example.com", "pw-12345!")
        )
        note = services.create_note(make_user(), title="support case", content=doc("body"))
        indexing.index_note(note.pk, note.version)
        self.chunk = NoteChunk.objects.get()
        self.base = f"/{settings.ADMIN_URL}retrieval/notechunk/"

    def test_list_and_detail_are_readable(self):
        self.assertContains(self.client.get(self.base), "fake@1536")
        self.assertEqual(self.client.get(f"{self.base}{self.chunk.pk}/change/").status_code, 200)

    def test_add_and_delete_are_refused(self):
        self.assertEqual(self.client.get(f"{self.base}add/").status_code, 403)
        self.assertEqual(self.client.get(f"{self.base}{self.chunk.pk}/delete/").status_code, 403)
