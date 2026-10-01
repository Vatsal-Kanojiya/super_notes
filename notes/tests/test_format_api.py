"""The format API: auth, ownership, the Idempotency-Key header, the flow, replay, 429, apply.

Authentication is forced; the JWT layer is tested in accounts/. The rules
(limit, idempotency, lock) are tested in test_format_service.py; this is the
HTTP around them, plus the whole path from POST to an applied PATCH.
"""

import uuid
from unittest import mock

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from limits.models import UsageEvent
from notes import services
from notes.models import FormatJob, Note

from .helpers import doc, format_limit, make_user

DELAY = "notes.tasks.format_note.delay"
COMPLETE = "assistant.chat.complete"


def format_url(note):
    return f"/api/v1/notes/{getattr(note, 'pk', note)}/format/"


def job_url(job):
    return f"/api/v1/format-jobs/{getattr(job, 'pk', job)}/"


@override_settings(LIMIT_DEFAULTS=format_limit(3, 10))
class FormatAPITestCase(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.note = services.create_note(
            self.alice, title="Goa", content=doc("Trip to Goa", "Book the hotel", "Pay 4,500")
        )
        self.client = APIClient()
        self.client.force_authenticate(self.alice)

    def post(self, note=None, key=None, client=None):
        key = str(uuid.uuid4()) if key is None else key
        headers = {"Idempotency-Key": key} if key != "" else {}
        return (client or self.client).post(format_url(note or self.note), headers=headers)


class AuthAndIsolationTests(FormatAPITestCase):
    def test_every_endpoint_needs_a_user(self):
        anonymous = APIClient()
        self.assertEqual(
            anonymous.post(format_url(self.note), headers={"Idempotency-Key": "k"}).status_code, 401
        )
        self.assertEqual(anonymous.get(job_url(1)).status_code, 401)

    def test_another_users_note_is_a_404_and_uses_nothing(self):
        bob = make_user("bob")
        theirs = services.create_note(bob, content=doc("bob's secret plans"))

        with mock.patch(DELAY) as delay:
            response = self.post(theirs)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")
        self.assertFalse(FormatJob.objects.exists())
        self.assertFalse(UsageEvent.objects.exists())
        delay.assert_not_called()

    def test_a_missing_or_deleted_note_is_a_404(self):
        services.delete_note(self.alice, self.note.pk)
        self.assertEqual(self.post().status_code, 404)
        self.assertEqual(self.post(999_999).status_code, 404)

    def test_another_users_job_is_a_404(self):
        bob = make_user("bob")
        theirs = services.create_note(bob, content=doc("bob's note"))
        with mock.patch(DELAY):
            job = FormatJob.objects.create(
                note=theirs, owner=bob, base_version=1, idempotency_key="k"
            )

        self.assertEqual(self.client.get(job_url(job)).status_code, 404)

    def test_a_replayed_key_of_another_user_never_returns_their_job(self):
        bob = make_user("bob")
        theirs = services.create_note(bob, content=doc("bob's note"))
        with mock.patch(DELAY):
            self.assertEqual(self.post(theirs, key="same", client=self._as(bob)).status_code, 202)
            # Alice with Bob's key and Bob's note: his note is hidden, so 404, not his job.
            self.assertEqual(self.post(theirs, key="same").status_code, 404)

    def _as(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client


class IdempotencyHeaderTests(FormatAPITestCase):
    def test_the_header_is_required(self):
        response = self.post(key="")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "idempotency_key_required")

    def test_the_header_must_be_well_formed(self):
        for key in ("has space", "x" * 101, "semi;colon", "tab\tkey"):
            with self.subTest(key=key):
                response = self.post(key=key)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], "idempotency_key_invalid")
        self.assertFalse(FormatJob.objects.exists())

    def test_a_key_for_another_note_is_a_422(self):
        other = services.create_note(self.alice, content=doc("another"))
        with mock.patch(DELAY):
            self.post(key="k")
            response = self.post(other, key="k")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "idempotency_key_reused")


class FlowTests(FormatAPITestCase):
    def test_post_returns_202_pending_and_the_job_can_be_polled(self):
        with mock.patch(DELAY) as delay, self.captureOnCommitCallbacks(execute=True):
            response = self.post()

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(
            body,
            {
                "id": body["id"],
                "note_id": self.note.pk,
                "status": "pending",
                "base_version": 1,
                "proposed_content": None,
                "error_code": "",
                "error": "",
                "created_at": body["created_at"],
                "completed_at": None,
            },
        )
        delay.assert_called_once_with(body["id"])
        self.assertEqual(self.client.get(job_url(body["id"])).json(), body)

    def test_the_whole_path_with_the_fake_provider_and_apply(self):
        with self.captureOnCommitCallbacks(execute=True):
            created = self.post().json()

        job = self.client.get(job_url(created["id"])).json()
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["proposed_content"]["content"][0]["type"], "heading")
        self.assertEqual(job["base_version"], 1)
        # Nothing has touched the note yet.
        self.assertEqual(Note.objects.get(pk=self.note.pk).version, 1)

        applied = self.client.patch(
            f"/api/v1/notes/{self.note.pk}/",
            {"version": job["base_version"], "content": job["proposed_content"]},
            format="json",
        )
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(applied.json()["version"], 2)
        self.assertEqual(applied.json()["content"], job["proposed_content"])

    def test_applying_on_a_note_edited_meanwhile_is_a_409(self):
        with self.captureOnCommitCallbacks(execute=True):
            created = self.post().json()
        job = self.client.get(job_url(created["id"])).json()
        # Edited on another device after the job was made.
        services.update_note(self.alice, self.note.pk, expected_version=1, title="Goa 2")

        stale = self.client.patch(
            f"/api/v1/notes/{self.note.pk}/",
            {"version": job["base_version"], "content": job["proposed_content"]},
            format="json",
        )

        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["code"], "version_conflict")
        self.assertEqual(stale.json()["current"]["title"], "Goa 2")
        self.assertNotEqual(Note.objects.get(pk=self.note.pk).content, job["proposed_content"])

    def test_a_refused_proposal_polls_as_failed_with_the_code(self):
        canned = mock.Mock(text='{"type":"doc","content":[]}', provider="fake", model="m")
        canned.input_tokens, canned.output_tokens = 5, 5
        with (
            mock.patch(COMPLETE, return_value=canned),
            self.assertLogs("notes.tasks", "WARNING"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            created = self.post().json()

        job = self.client.get(job_url(created["id"])).json()
        self.assertEqual((job["status"], job["error_code"]), ("failed", "format_changed_content"))
        self.assertIsNone(job["proposed_content"])
        self.assertTrue(job["error"])

    def test_shape_leaves_out_provider_model_and_tokens(self):
        job = FormatJob.objects.create(
            note=self.note,
            owner=self.alice,
            base_version=1,
            idempotency_key="k",
            provider="claude",
            model="secret-model",
            input_tokens=9,
        )
        body = self.client.get(job_url(job)).json()
        for hidden in ("provider", "model", "input_tokens", "output_tokens", "prompt_version"):
            self.assertNotIn(hidden, body)


class ReplayTests(FormatAPITestCase):
    def test_a_replayed_key_is_a_200_with_the_same_job_and_uses_nothing(self):
        with mock.patch(DELAY) as delay, self.captureOnCommitCallbacks(execute=True):
            first = self.post(key="k1")
            second = self.post(key="k1")

        self.assertEqual((first.status_code, second.status_code), (202, 200))
        self.assertEqual(first.json(), second.json())
        self.assertEqual(FormatJob.objects.count(), 1)
        self.assertEqual(UsageEvent.objects.filter(key="format").count(), 1)
        delay.assert_called_once()

    def test_a_replay_still_answers_when_the_limit_is_used_up(self):
        with mock.patch(DELAY):
            first = self.post(key="a")
            self.post(key="b")
            self.post(key="c")
            self.assertEqual(self.post(key="d").status_code, 429)
            replay = self.post(key="a")

        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["id"], first.json()["id"])


class LimitTests(FormatAPITestCase):
    def test_over_the_limit_is_a_429_with_usage_and_creates_nothing(self):
        with mock.patch(DELAY) as delay, self.captureOnCommitCallbacks(execute=True):
            for key in "abc":
                self.assertEqual(self.post(key=key).status_code, 202)
            response = self.post(key="d")

        self.assertEqual(response.status_code, 429)
        body = response.json()
        self.assertEqual((body["code"], body["used"], body["limit"]), ("quota_exceeded", 3, 3))
        self.assertIsNotNone(body["resets_at"])
        self.assertEqual(FormatJob.objects.count(), 3)
        self.assertEqual(delay.call_count, 3)

    def test_a_failed_job_gives_its_use_back(self):
        canned = mock.Mock(text="not json", provider="fake", model="m")
        canned.input_tokens, canned.output_tokens = 5, 5
        with (
            mock.patch(COMPLETE, return_value=canned),
            self.assertLogs("notes.tasks", "WARNING"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            for key in "abc":
                self.assertEqual(self.post(key=key).status_code, 202)
        with mock.patch(DELAY):
            self.assertEqual(self.post(key="d").status_code, 202)

    def test_me_reports_the_format_limit(self):
        with mock.patch(DELAY):
            self.post()
        limit = self.client.get("/api/v1/me/").json()["limits"]["format"]
        self.assertEqual((limit["used"], limit["limit"]), (1, 3))

    @override_settings(LIMIT_DEFAULTS=format_limit(5, 5, system=1))
    def test_the_system_limit_is_a_503(self):
        with mock.patch(DELAY):
            self.assertEqual(self.post(key="a").status_code, 202)
            response = self.post(key="b")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "system_limit_reached")
        self.assertEqual(FormatJob.objects.count(), 1)


class RefusalTests(FormatAPITestCase):
    def test_an_empty_note_is_a_400_and_uses_nothing(self):
        blank = services.create_note(self.alice)

        response = self.post(blank)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "note_empty")
        self.assertFalse(UsageEvent.objects.exists())

    @override_settings(FORMAT_MAX_INPUT_CHARS=40)
    def test_a_note_too_long_is_a_400_and_uses_nothing(self):
        response = self.post()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "note_too_long")
        self.assertFalse(UsageEvent.objects.exists())

    def test_get_and_other_methods_on_the_create_url_are_refused(self):
        self.assertEqual(self.client.get(format_url(self.note)).status_code, 405)
        self.assertEqual(self.client.delete(job_url(1)).status_code, 405)
