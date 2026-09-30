"""The admin can read notes and nothing else: writes must go through services."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase

from notes import services
from notes.models import Note

from .helpers import doc, make_user


class NoteAdminTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("root@example.com", "pw-12345!")
        self.client.force_login(self.admin)
        self.note = services.create_note(make_user(), title="support case", content=doc("body"))
        self.base = f"/{settings.ADMIN_URL}notes/note/"

    def test_list_and_detail_are_readable(self):
        self.assertContains(self.client.get(self.base), "support case")
        self.assertContains(self.client.get(f"{self.base}{self.note.pk}/change/"), "body")

    def test_add_change_and_delete_are_refused(self):
        self.assertEqual(self.client.get(f"{self.base}add/").status_code, 403)
        self.assertEqual(self.client.get(f"{self.base}{self.note.pk}/delete/").status_code, 403)
        self.client.post(f"{self.base}{self.note.pk}/change/", {"title": "edited"})

        self.assertEqual(Note.objects.get(pk=self.note.pk).title, "support case")
