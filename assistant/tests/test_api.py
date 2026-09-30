"""The Ask API: auth, isolation, the Idempotency-Key header, the flow, replay, 429, me/.

Authentication is forced; the JWT layer is tested in accounts/. The quota
and idempotency rules themselves are tested in test_services.py; this is
the HTTP around them.
"""

import uuid
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from assistant import quota
from assistant.api import AskListView
from assistant.models import AskQuery
from notes import services
from notes.tests.helpers import doc, make_user
from retrieval.indexing import index_note

ASK = "/api/v1/ask/"
ME = "/api/v1/me/"


def detail(ask_or_id):
    return f"{ASK}{getattr(ask_or_id, 'pk', ask_or_id)}/"


class AskAPITestCase(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.client = APIClient()
        self.client.force_authenticate(self.alice)

    def post(self, question="When does my passport expire?", key=None, client=None):
        key = str(uuid.uuid4()) if key is None else key
        headers = {"Idempotency-Key": key} if key != "" else {}
        return (client or self.client).post(
            ASK, {"question": question}, format="json", headers=headers
        )


class AuthAndIsolationTests(AskAPITestCase):
    def test_every_endpoint_needs_a_user(self):
        anonymous = APIClient()
        for response in (
            anonymous.get(ASK),
            anonymous.post(ASK, {"question": "x"}, format="json", headers={"Idempotency-Key": "k"}),
            anonymous.get(detail(1)),
        ):
            self.assertEqual(response.status_code, 401)
        self.assertFalse(AskQuery.objects.exists())

    def test_another_users_ask_is_a_404_and_not_listed(self):
        bob = make_user("bob")
        theirs = AskQuery.objects.create(user=bob, question="Bob's?", idempotency_key="k")

        self.assertEqual(self.client.get(detail(theirs)).status_code, 404)
        self.assertEqual(self.client.get(ASK).json()["results"], [])

    def test_history_is_newest_first_and_paginated(self):
        asks = [
            AskQuery.objects.create(user=self.alice, question=f"q{i}", idempotency_key=f"k{i}")
            for i in range(3)
        ]

        first = self.client.get(ASK, {"page_size": 2}).json()
        self.assertEqual([a["id"] for a in first["results"]], [asks[2].pk, asks[1].pk])
        rest = self.client.get(first["next"]).json()
        self.assertEqual([a["id"] for a in rest["results"]], [asks[0].pk])

    def test_shape_leaves_out_debugging_fields(self):
        ask = AskQuery.objects.create(
            user=self.alice, question="q", idempotency_key="k", retrieved=[{"chunk_id": 1}]
        )
        body = self.client.get(detail(ask)).json()
        self.assertEqual(
            set(body),
            {
                "id",
                "question",
                "status",
                "answer",
                "citations",
                "error",
                "created_at",
                "completed_at",
            },
        )


class IdempotencyHeaderTests(AskAPITestCase):
    def test_missing_header(self):
        response = self.post(key="")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "idempotency_key_required")
        self.assertFalse(AskQuery.objects.exists())

    def test_malformed_header(self):
        for key in ("has space", "a" * 101, "semi;colon", "ünï"):
            with self.subTest(key=key):
                response = self.post(key=key)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], "idempotency_key_invalid")
        self.assertFalse(AskQuery.objects.exists())

    def test_uuid_and_token_keys_are_accepted(self):
        for key in (str(uuid.uuid4()), "a" * 100, "retry_1-A"):
            with self.subTest(key=key):
                self.assertEqual(self.post(key=key).status_code, 202)

    def test_question_is_validated(self):
        for question in ("", "   ", "x" * 1001):
            with self.subTest(length=len(question)):
                response = self.post(question=question)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], "invalid")


class FlowTests(AskAPITestCase):
    def setUp(self):
        super().setUp()
        self.passport = services.create_note(
            self.alice, title="Passport", content=doc("My passport expires in March 2027.")
        )
        index_note(self.passport.pk, self.passport.version)

    def test_ask_then_poll_to_a_cited_answer(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post()

        self.assertEqual(response.status_code, 202)
        created = response.json()
        self.assertEqual(created["status"], "pending")
        self.assertEqual(created["question"], "When does my passport expire?")

        polled = self.client.get(detail(created["id"])).json()
        self.assertEqual(polled["status"], "done")
        self.assertIn("March 2027", polled["answer"])
        self.assertEqual(polled["citations"][0]["note_id"], self.passport.pk)
        self.assertEqual(polled["citations"][0]["n"], 1)
        self.assertIsNotNone(polled["completed_at"])

    def test_replayed_key_returns_the_same_ask_with_200(self):
        key = str(uuid.uuid4())
        with self.captureOnCommitCallbacks(execute=True):
            first = self.post(key=key)
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            again = self.post(key=key)

        self.assertEqual((first.status_code, again.status_code), (202, 200))
        self.assertEqual(again.json()["id"], first.json()["id"])
        # The replay sees the answer the first request started.
        self.assertEqual(again.json()["status"], "done")
        self.assertEqual(callbacks, [])
        self.assertEqual(AskQuery.objects.count(), 1)

    def test_reused_key_with_another_question_is_a_422(self):
        key = str(uuid.uuid4())
        self.post(key=key)
        response = self.post(question="Something else?", key=key)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "idempotency_key_reused")


@override_settings(ASK_QUOTAS={"free": 1, "premium": 10})
class QuotaTests(AskAPITestCase):
    def test_over_quota_is_a_429_with_usage(self):
        self.assertEqual(self.post().status_code, 202)

        response = self.post(question="And another?")

        self.assertEqual(response.status_code, 429)
        body = response.json()
        self.assertEqual(body["code"], "quota_exceeded")
        self.assertEqual((body["used"], body["limit"]), (1, 1))
        resets_at = datetime.fromisoformat(body["resets_at"])
        self.assertEqual(resets_at, quota.month_bounds()[1])
        self.assertEqual(AskQuery.objects.count(), 1)

    def test_post_uses_the_ask_throttle_scope_and_reads_do_not(self):
        view = AskListView()
        view.request = mock.Mock(method="POST")
        self.assertEqual(view.throttle_scope, "ask")
        view.request = mock.Mock(method="GET")
        self.assertIsNone(view.throttle_scope)


@override_settings(ASK_QUOTAS={"free": 20, "premium": 500})
class MeUsageTests(AskAPITestCase):
    def test_me_reports_this_months_usage(self):
        AskQuery.objects.create(user=self.alice, question="a", idempotency_key="a")
        AskQuery.objects.create(
            user=self.alice, question="b", idempotency_key="b", status=AskQuery.Status.FAILED
        )

        usage = self.client.get(ME).json()["ask_usage"]

        self.assertEqual((usage["used"], usage["limit"]), (1, 20))
        resets_at = datetime.fromisoformat(usage["resets_at"])
        self.assertEqual(resets_at, quota.month_bounds()[1])
        self.assertEqual(resets_at.day, 1)
        self.assertEqual(resets_at.utcoffset(), datetime.now(ZoneInfo("Asia/Kolkata")).utcoffset())
