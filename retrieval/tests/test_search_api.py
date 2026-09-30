"""GET /api/v1/search/: auth, isolation, validation and the throttle scope."""

from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from notes import services
from notes.tests.helpers import doc, make_user
from retrieval.api import SearchView, snippet
from retrieval.indexing import index_note

SEARCH = "/api/v1/search/"


def write(owner, title, body):
    note = services.create_note(owner, title=title, content=doc(body))
    index_note(note.pk, note.version)
    return note


class SearchApiTests(TestCase):
    def setUp(self):
        self.alice, self.bob = make_user("alice"), make_user("bob")
        self.client = APIClient()
        self.client.force_authenticate(self.alice)

    def test_anonymous_is_401(self):
        self.assertEqual(APIClient().get(SEARCH, {"q": "passport"}).status_code, 401)

    def test_returns_the_callers_hits_only(self):
        mine = write(self.alice, "Passport", "renew the passport before the trip")
        write(self.bob, "Passport", "renew the passport before the trip")

        response = self.client.get(SEARCH, {"q": "renew passport"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([hit["note_id"] for hit in body], [mine.pk])
        self.assertEqual(
            set(body[0]),
            {
                "chunk_id",
                "note_id",
                "title",
                "heading_path",
                "text",
                "snippet",
                "score",
                "similarity",
                "keyword_rank",
            },
        )
        self.assertEqual(body[0]["title"], "Passport")
        self.assertEqual(body[0]["snippet"], "renew the passport before the trip")

    def test_k_limits_the_hits(self):
        for n in range(4):
            write(self.alice, f"Note {n}", f"budget {n}")
        self.assertEqual(len(self.client.get(SEARCH, {"q": "budget", "k": 2}).json()), 2)

    def test_validation(self):
        for params in (
            {},
            {"q": ""},
            {"q": "x", "k": 0},
            {"q": "x", "k": 21},
            {"q": "x", "k": "a"},
        ):
            with self.subTest(params=params):
                response = self.client.get(SEARCH, params)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], "invalid")
        self.assertEqual(self.client.get(SEARCH, {"q": "x" * 501}).status_code, 400)
        # The field trims whitespace and then refuses the blank.
        self.assertEqual(self.client.get(SEARCH, {"q": "   "}).status_code, 400)

    @override_settings(
        # The test runner turns the cache (and so throttling) off.
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "search-api-tests",
            }
        }
    )
    def test_throttle_scope(self):
        self.assertEqual(SearchView.throttle_scope, "search")
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"search": "1/hour"}):
            self.assertEqual(self.client.get(SEARCH, {"q": "x"}).status_code, 200)
            response = self.client.get(SEARCH, {"q": "x"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "throttled")


class SnippetTests(SimpleTestCase):
    def test_short_text_is_whole_with_whitespace_collapsed(self):
        self.assertEqual(snippet("a  b\n\nc"), "a b c")

    def test_long_text_is_cut_at_a_word(self):
        self.assertEqual(snippet("alpha beta gamma", limit=12), "alpha beta…")

    def test_one_long_word_is_cut_by_length(self):
        self.assertEqual(snippet("x" * 30, limit=10), "x" * 10 + "…")
