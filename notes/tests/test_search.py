"""Keyword search, and proof that it uses the GIN index."""

from django.db import connection, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from notes import services
from notes.models import Note
from notes.search import SEARCH_CONFIG, keyword_search

from .helpers import checklist_doc, doc, make_user

NOTES = "/api/v1/notes/"


class KeywordSearchTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.client = APIClient()
        self.client.force_authenticate(self.alice)
        self.trip = services.create_note(self.alice, title="Goa trip", content=doc("Book flights"))
        self.shop = services.create_note(
            self.alice,
            type="checklist",
            title="Groceries",
            content=checklist_doc(("mangoes", False)),
        )

    def search(self, q, **params):
        response = self.client.get(NOTES, {"q": q, **params})
        self.assertEqual(response.status_code, 200)
        return [n["id"] for n in response.json()["results"]]

    def test_finds_by_title(self):
        self.assertEqual(self.search("goa"), [self.trip.pk])

    def test_finds_by_body(self):
        self.assertEqual(self.search("flights"), [self.trip.pk])

    def test_finds_checklist_items(self):
        self.assertEqual(self.search("mangoes"), [self.shop.pk])

    def test_stems_match(self):
        # english config: "flight" and "flights" share a stem, "booking" and "book" too.
        self.assertEqual(self.search("flight booking"), [self.trip.pk])

    def test_all_words_must_match(self):
        self.assertEqual(self.search("flights mangoes"), [])

    def test_or_and_exclusion(self):
        self.assertEqual(self.search("flights or mangoes"), [self.shop.pk, self.trip.pk])
        self.assertEqual(self.search("goa -flights"), [])

    def test_punctuation_is_not_a_syntax_error(self):
        self.assertEqual(self.search("goa & ( ! :*"), [self.trip.pk])

    def test_combines_with_type(self):
        self.assertEqual(self.search("mangoes", type="text"), [])

    def test_blank_q_is_no_filter(self):
        self.assertEqual(self.search("  "), [self.shop.pk, self.trip.pk])

    def test_search_follows_edits(self):
        services.update_note(self.alice, self.trip.pk, expected_version=1, content=doc("Trains"))
        self.assertEqual(self.search("flights"), [])
        self.assertEqual(self.search("trains"), [self.trip.pk])

    def test_deleted_notes_are_not_found(self):
        services.delete_note(self.alice, self.trip.pk)
        self.assertEqual(self.search("goa"), [])

    def test_q_over_200_characters_is_400(self):
        self.assertEqual(self.client.get(NOTES, {"q": "a" * 201}).status_code, 400)

    def test_never_another_users_note(self):
        bob = make_user("bob")
        services.create_note(bob, title="Goa trip", content=doc("Book flights"))

        self.assertEqual(self.search("goa flights"), [self.trip.pk])


class SearchIndexTests(TestCase):
    """The GIN index is an expression index; a query must build the same one."""

    def test_config_is_english(self):
        # Retrieval's chunk search reuses this; changing it needs a migration.
        self.assertEqual(SEARCH_CONFIG, "english")

    def test_the_query_can_use_the_gin_index(self):
        owner = make_user()
        for index in range(20):
            services.create_note(owner, title=f"note {index}", content=doc("filler words"))

        queryset = keyword_search(Note.objects.all(), "filler")
        with transaction.atomic(), connection.cursor() as cursor:
            # Take away every other way in. GIN supports only bitmap scans;
            # if the expressions differed, Postgres would have to fall back
            # to a (disabled, so hugely costed) sequential scan.
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute("SET LOCAL enable_indexscan = off")
            plan = queryset.explain()

        self.assertIn("Bitmap Index Scan on note_fts", plan)
