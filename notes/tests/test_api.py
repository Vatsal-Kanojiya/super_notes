"""The notes API: CRUD, conflicts, sync, limits and ownership.

Authentication is forced; the JWT layer is tested in accounts/.
"""

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from notes import services
from notes.models import Note

from .helpers import checklist_doc, doc, make_user

NOTES = "/api/v1/notes/"
CHANGES = "/api/v1/notes/changes/"


def detail(note_or_id):
    return f"{NOTES}{getattr(note_or_id, 'pk', note_or_id)}/"


class NotesAPITestCase(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        self.client = APIClient()
        self.client.force_authenticate(self.alice)

    def as_bob(self):
        client = APIClient()
        client.force_authenticate(self.bob)
        return client


class CreateAndReadTests(NotesAPITestCase):
    def test_create_returns_the_note_with_derived_fields(self):
        response = self.client.post(
            NOTES,
            {"type": "checklist", "title": "Shop", "content": checklist_doc(("milk", False))},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["content_text"], "- [ ] milk")
        self.assertEqual((body["version"], body["revision"]), (1, 1))
        self.assertIsNone(body["deleted_at"])
        self.assertEqual(Note.objects.get(pk=body["id"]).owner, self.alice)

    def test_create_with_nothing_is_an_empty_text_note(self):
        response = self.client.post(NOTES, {}, format="json")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["type"], "text")
        self.assertEqual(response.json()["content"], {"type": "doc", "content": []})

    def test_server_fields_in_the_payload_are_ignored(self):
        response = self.client.post(
            NOTES,
            {
                "title": "mine",
                "content": doc("real words"),
                "content_text": "injected keywords",
                "version": 40,
                "revision": 50,
                "owner": self.bob.pk,
                "deleted_at": "2020-01-01T00:00:00Z",
            },
            format="json",
        )

        body = response.json()
        self.assertEqual(body["content_text"], "real words")
        self.assertEqual((body["version"], body["revision"]), (1, 1))
        self.assertIsNone(body["deleted_at"])
        self.assertEqual(Note.objects.get(pk=body["id"]).owner, self.alice)

    def test_invalid_content_is_400(self):
        response = self.client.post(NOTES, {"content": {"type": "nope"}}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid")
        self.assertIn("content", response.json())

    def test_title_over_500_characters_is_400(self):
        response = self.client.post(NOTES, {"title": "x" * 501}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("title", response.json())

    def test_unknown_type_is_400(self):
        response = self.client.post(NOTES, {"type": "drawing"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_retrieve(self):
        note = services.create_note(self.alice, title="hello")
        response = self.client.get(detail(note))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "hello")

    def test_non_numeric_id_is_404(self):
        self.assertEqual(self.client.get(f"{NOTES}abc/").status_code, 404)

    def test_anonymous_is_401(self):
        self.assertEqual(APIClient().get(NOTES).status_code, 401)


@override_settings(NOTE_CONTENT_MAX_BYTES=200)
class ContentCapTests(NotesAPITestCase):
    def test_content_over_the_cap_is_refused_on_create(self):
        response = self.client.post(NOTES, {"content": doc("x" * 300)}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("limit is 200 bytes", response.json()["content"][0])
        self.assertFalse(Note.objects.exists())

    def test_content_over_the_cap_is_refused_on_update(self):
        note = services.create_note(self.alice)
        response = self.client.patch(
            detail(note), {"version": 1, "content": doc("x" * 300)}, format="json"
        )

        self.assertEqual(response.status_code, 400)

    def test_content_under_the_cap_is_fine(self):
        response = self.client.post(NOTES, {"content": doc("x" * 50)}, format="json")
        self.assertEqual(response.status_code, 201)

    def test_size_counts_utf8_bytes_not_characters(self):
        # 60 characters, 240 bytes.
        response = self.client.post(NOTES, {"content": doc("😀" * 60)}, format="json")
        self.assertEqual(response.status_code, 400)


class ListTests(NotesAPITestCase):
    def test_list_is_newest_first_and_leaves_out_deleted_notes(self):
        first = services.create_note(self.alice, title="first")
        second = services.create_note(self.alice, title="second")
        gone = services.create_note(self.alice, title="gone")
        services.delete_note(self.alice, gone.pk)

        results = self.client.get(NOTES).json()["results"]

        self.assertEqual([n["id"] for n in results], [second.pk, first.pk])

    def test_filter_by_type(self):
        services.create_note(self.alice, type="text", title="prose")
        services.create_note(self.alice, type="checklist", title="todo")

        results = self.client.get(NOTES, {"type": "checklist"}).json()["results"]

        self.assertEqual([n["title"] for n in results], ["todo"])

    def test_bad_type_filter_is_400(self):
        response = self.client.get(NOTES, {"type": "drawing"})
        self.assertEqual(response.status_code, 400)

    def test_list_pages_by_cursor(self):
        for index in range(3):
            services.create_note(self.alice, title=str(index))

        page = self.client.get(NOTES, {"page_size": 2}).json()
        rest = self.client.get(page["next"]).json()

        self.assertEqual([n["title"] for n in page["results"] + rest["results"]], ["2", "1", "0"])


class UpdateTests(NotesAPITestCase):
    def setUp(self):
        super().setUp()
        self.note = services.create_note(self.alice, title="draft", content=doc("v1"))

    def test_patch_with_current_version_saves(self):
        response = self.client.patch(
            detail(self.note), {"version": 1, "content": doc("v2")}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["version"], body["revision"], body["title"]), (2, 2, "draft"))
        self.assertEqual(body["content_text"], "v2")

    def test_patch_can_change_type(self):
        response = self.client.patch(
            detail(self.note), {"version": 1, "type": "checklist"}, format="json"
        )
        self.assertEqual(response.json()["type"], "checklist")

    def test_patch_without_version_is_400(self):
        response = self.client.patch(detail(self.note), {"title": "x"}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("version", response.json())

    def test_stale_version_is_409_with_the_server_copy(self):
        self.client.patch(detail(self.note), {"version": 1, "title": "phone"}, format="json")

        response = self.client.patch(
            detail(self.note), {"version": 1, "title": "laptop"}, format="json"
        )

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["code"], "version_conflict")
        self.assertIn("detail", body)
        self.assertEqual(body["current"]["title"], "phone")
        self.assertEqual(body["current"]["version"], 2)
        self.assertEqual(body["current"]["content"], doc("v1"))
        self.assertEqual(Note.objects.get(pk=self.note.pk).title, "phone")

    def test_client_cannot_set_server_fields_on_patch(self):
        response = self.client.patch(
            detail(self.note),
            {"version": 1, "content_text": "x", "revision": 99, "owner": self.bob.pk},
            format="json",
        )

        body = response.json()
        self.assertEqual((body["content_text"], body["revision"]), ("v1", 2))
        self.assertEqual(Note.objects.get(pk=self.note.pk).owner, self.alice)

    def test_put_is_not_allowed(self):
        response = self.client.put(detail(self.note), {"version": 1}, format="json")
        self.assertEqual(response.status_code, 405)

    def test_note_deleted_after_lookup_is_404(self):
        # The note vanishes between get_object() and the service's lock.
        from unittest import mock

        with mock.patch.object(services, "_locked_live_note", side_effect=Note.DoesNotExist):
            response = self.client.patch(detail(self.note), {"version": 1}, format="json")
            deleted = self.client.delete(detail(self.note))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(deleted.status_code, 404)


class DeleteTests(NotesAPITestCase):
    def test_delete_is_204_then_the_note_is_gone(self):
        note = services.create_note(self.alice)

        self.assertEqual(self.client.delete(detail(note)).status_code, 204)
        self.assertEqual(self.client.get(detail(note)).status_code, 404)
        self.assertEqual(
            self.client.patch(detail(note), {"version": 2}, format="json").status_code, 404
        )
        self.assertEqual(self.client.delete(detail(note)).status_code, 404)
        # Soft: the row is still there, as a tombstone.
        self.assertIsNotNone(Note.objects.get(pk=note.pk).deleted_at)


class ChangesTests(NotesAPITestCase):
    def changes(self, **params):
        response = self.client.get(CHANGES, params)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_first_sync_gets_everything(self):
        a = services.create_note(self.alice, title="a")
        b = services.create_note(self.alice, title="b")

        body = self.changes()

        self.assertEqual([n["id"] for n in body["results"]], [a.pk, b.pk])
        self.assertEqual(body["latest_revision"], 2)
        self.assertFalse(body["has_more"])

    def test_after_n_returns_exactly_the_writes_after_n(self):
        a = services.create_note(self.alice, title="a")  # 1
        b = services.create_note(self.alice, title="b")  # 2
        c = services.create_note(self.alice, title="c")  # 3
        services.update_note(self.alice, a.pk, expected_version=1, title="a2")  # 4

        body = self.changes(after=2)

        self.assertEqual(
            [(n["id"], n["revision"]) for n in body["results"]], [(c.pk, 3), (a.pk, 4)]
        )
        self.assertNotIn(b.pk, [n["id"] for n in body["results"]])
        self.assertEqual(body["latest_revision"], 4)

    def test_nothing_new_is_empty_with_the_same_revision(self):
        services.create_note(self.alice)
        body = self.changes(after=1)

        self.assertEqual(body, {"results": [], "latest_revision": 1, "has_more": False})

    def test_a_note_written_twice_appears_once_at_its_newest_revision(self):
        note = services.create_note(self.alice, title="v1")
        services.update_note(self.alice, note.pk, expected_version=1, title="v2")

        body = self.changes()

        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["results"][0]["title"], "v2")

    def test_delete_appears_as_a_tombstone(self):
        note = services.create_note(self.alice, title="secret", content=doc("body"))
        services.delete_note(self.alice, note.pk)

        body = self.changes(after=1)

        self.assertEqual(len(body["results"]), 1)
        tombstone = body["results"][0]
        self.assertEqual(tombstone["id"], note.pk)
        self.assertEqual(tombstone["revision"], 2)
        self.assertIsNotNone(tombstone["deleted_at"])
        # Enough to drop it; none of its content.
        self.assertNotIn("title", tombstone)
        self.assertNotIn("content", tombstone)
        self.assertNotIn("content_text", tombstone)

    def test_live_notes_in_changes_carry_null_deleted_at(self):
        services.create_note(self.alice)
        self.assertIsNone(self.changes()["results"][0]["deleted_at"])

    def test_limit_cuts_the_batch_and_the_client_loops(self):
        ids = [services.create_note(self.alice, title=str(i)).pk for i in range(5)]

        seen, after, calls = [], 0, 0
        while True:
            body = self.changes(after=after, limit=2)
            calls += 1
            seen += [n["id"] for n in body["results"]]
            after = body["latest_revision"]
            if not body["has_more"]:
                break

        self.assertEqual(seen, ids)
        self.assertEqual(calls, 3)
        self.assertEqual(after, 5)

    def test_exactly_limit_changes_is_not_has_more(self):
        services.create_note(self.alice)
        services.create_note(self.alice)
        body = self.changes(limit=2)

        self.assertFalse(body["has_more"])
        self.assertEqual(body["latest_revision"], 2)

    def test_bad_parameters_are_400(self):
        for params in ({"after": -1}, {"after": "x"}, {"limit": 0}, {"limit": 1001}):
            with self.subTest(params):
                self.assertEqual(self.client.get(CHANGES, params).status_code, 400)

    def test_after_beyond_the_server_reports_the_servers_revision(self):
        # A client ahead of the server (a restored database) sees a lower
        # latest_revision than it sent, its cue for a full resync.
        services.create_note(self.alice)
        body = self.changes(after=10)

        self.assertEqual((body["results"], body["latest_revision"]), ([], 1))


class OwnershipTests(NotesAPITestCase):
    """Every endpoint, from Bob's side, on Alice's note."""

    def setUp(self):
        super().setUp()
        self.note = services.create_note(self.alice, title="alice's", content=doc("private"))
        self.bob_client = self.as_bob()

    def test_list_does_not_show_it(self):
        self.assertEqual(self.bob_client.get(NOTES).json()["results"], [])

    def test_search_does_not_find_it(self):
        response = self.bob_client.get(NOTES, {"q": "private"})
        self.assertEqual(response.json()["results"], [])

    def test_retrieve_is_404(self):
        self.assertEqual(self.bob_client.get(detail(self.note)).status_code, 404)

    def test_patch_is_404_and_changes_nothing(self):
        response = self.bob_client.patch(
            detail(self.note), {"version": 1, "title": "mine now"}, format="json"
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(Note.objects.get(pk=self.note.pk).title, "alice's")

    def test_delete_is_404_and_deletes_nothing(self):
        self.assertEqual(self.bob_client.delete(detail(self.note)).status_code, 404)
        self.assertIsNone(Note.objects.get(pk=self.note.pk).deleted_at)

    def test_changes_does_not_include_it(self):
        services.create_note(self.bob, title="bob's")
        body = self.bob_client.get(CHANGES).json()

        self.assertEqual([n["title"] for n in body["results"]], ["bob's"])
        self.assertEqual(body["latest_revision"], 1)

    def test_changes_does_not_include_its_tombstone(self):
        services.delete_note(self.alice, self.note.pk)
        self.assertEqual(self.bob_client.get(CHANGES).json()["results"], [])
