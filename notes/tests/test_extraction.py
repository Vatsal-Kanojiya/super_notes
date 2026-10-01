"""Attachment text extraction and indexing (notes/extraction.py, DECISIONS D340-D349).

Uploads go through the API inside captureOnCommitCallbacks(execute=True), so
the on-commit extraction task runs eagerly with the fake chat and embedding
providers, exactly as a worker would run it after the upload commits.
"""

from datetime import timedelta
from unittest import mock

from celery.exceptions import Retry, SoftTimeLimitExceeded
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from assistant import chat
from assistant.chat import (
    BilledChatError,
    ChatError,
    ChatResult,
    ImageTextNotSupported,
    TransientChatError,
)
from assistant.chat.providers.fake import FAKE_IMAGE_TEXT
from assistant.models import AskQuery
from assistant.tasks import answer_ask
from limits.models import UsageEvent
from notes import extraction, services
from notes.models import Attachment
from notes.tasks import extract_attachment, sweep_stuck_attachments
from retrieval.embeddings import EmbeddingError, EmbeddingTransientError
from retrieval.indexing import index_attachment, index_note
from retrieval.models import NoteChunk
from retrieval.search import search

from .helpers import doc, isolate_attachment_storage, make_pdf, make_user

NOTES = "/api/v1/notes/"
ATTACHMENTS = "/api/v1/attachments/"
CHANGES = "/api/v1/notes/changes/"

BOILER = "Annual service report.\nThe boiler was serviced by Ravi in March.\nNext visit in 2027."
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 24
GARBAGE_PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << >>\n%%EOF\n"
IMAGE_CALL = "assistant.chat.extract_image_text"


def image_text_limit(system, user_free=None):
    rule = {"system": system, "user_free": user_free, "period": "month"}
    return {**settings.LIMIT_DEFAULTS, "image_text": rule}


class ExtractionTestCase(TestCase):
    def setUp(self):
        isolate_attachment_storage(self)
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        self.client = APIClient()
        self.client.force_authenticate(self.alice)
        self.note = services.create_note(self.alice, title="House", content=doc("Paint the fence"))
        index_note(self.note.pk, self.note.version)

    def upload(self, content, name="boiler-report.pdf", owner=None, note=None):
        """Upload through the API and run the on-commit extraction; the fresh row."""
        client = self.client
        if owner is not None:
            client = APIClient()
            client.force_authenticate(owner)
        note = note or self.note
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                f"{NOTES}{note.pk}/attachments/",
                {"file": SimpleUploadedFile(name, content)},
                format="multipart",
            )
        self.assertIn(response.status_code, (200, 201), response.content)
        return Attachment.objects.get(pk=response.json()["id"])

    def chunks_of(self, attachment):
        return NoteChunk.objects.filter(attachment=attachment)

    def revision(self, user=None):
        user = user or self.alice
        user.refresh_from_db(fields=["notes_revision"])
        return user.notes_revision


class PdfTests(ExtractionTestCase):
    def test_a_pdfs_text_is_extracted_indexed_and_searchable(self):
        attachment = self.upload(make_pdf(BOILER, "Page two: the radiator valves."))

        self.assertEqual(attachment.status, Attachment.Status.READY)
        self.assertEqual(attachment.error, "")
        self.assertIn("serviced by Ravi", attachment.extracted_text)
        self.assertIn("\f", attachment.extracted_text)  # pages kept apart
        chunks = list(self.chunks_of(attachment))
        self.assertGreater(len(chunks), 0)
        for chunk in chunks:
            self.assertEqual(chunk.source, NoteChunk.Source.ATTACHMENT)
            self.assertEqual((chunk.note_id, chunk.owner_id), (self.note.pk, self.alice.pk))

        hits = search(self.alice, "When was the boiler serviced?")
        self.assertEqual(hits[0].attachment_id, attachment.pk)
        self.assertEqual(hits[0].attachment_name, "boiler-report.pdf")
        self.assertEqual(hits[0].source, "attachment")
        self.assertEqual(hits[0].note_id, self.note.pk)
        self.assertEqual(hits[0].title, "House")

    def test_the_search_api_names_the_file(self):
        attachment = self.upload(make_pdf(BOILER))

        body = self.client.get("/api/v1/search/", {"q": "boiler serviced"}).json()

        self.assertEqual(body[0]["source"], "attachment")
        self.assertEqual(body[0]["attachment_id"], attachment.pk)
        self.assertEqual(body[0]["attachment_name"], "boiler-report.pdf")
        self.assertIn("Ravi", body[0]["text"])

    def test_an_ask_cites_the_file_and_labels_its_excerpt(self):
        attachment = self.upload(make_pdf(BOILER))
        query = AskQuery.objects.create(
            user=self.alice, question="Who serviced the boiler?", idempotency_key="k1"
        )
        real = chat.complete
        with mock.patch("assistant.chat.complete", side_effect=real) as spy:
            answer_ask.delay(query.pk)

        query.refresh_from_db()
        self.assertEqual(query.status, "done", query.error)
        cited = [c for c in query.citations if c["attachment_id"] == attachment.pk]
        self.assertEqual(len(cited), 1, query.citations)
        self.assertEqual(cited[0]["attachment_name"], "boiler-report.pdf")
        self.assertEqual(cited[0]["note_id"], self.note.pk)
        self.assertIn("Ravi", cited[0]["snippet"])
        # The model was told which excerpt is the file's.
        _, user = spy.call_args.args[:2]
        self.assertIn('title="House" file="boiler-report.pdf"', user)

        shown = self.client.get(f"/api/v1/ask/{query.pk}/").json()["citations"]
        self.assertIn(
            {"attachment_id": attachment.pk, "attachment_name": "boiler-report.pdf"},
            [{k: c[k] for k in ("attachment_id", "attachment_name")} for c in shown],
        )

    def test_a_citation_stored_before_attachments_reads_as_the_notes_own(self):
        old = {"n": 1, "note_id": self.note.pk, "chunk_id": 1, "title": "House", "snippet": "x"}
        query = AskQuery.objects.create(
            user=self.alice, question="Old?", idempotency_key="old", status="done", citations=[old]
        )

        shown = self.client.get(f"/api/v1/ask/{query.pk}/").json()["citations"]

        self.assertEqual(shown, [{**old, "attachment_id": None, "attachment_name": None}])

    def test_a_note_citation_has_no_attachment(self):
        query = AskQuery.objects.create(
            user=self.alice, question="What about the fence?", idempotency_key="k2"
        )
        answer_ask.delay(query.pk)

        query.refresh_from_db()
        self.assertEqual(query.citations[0]["note_id"], self.note.pk)
        self.assertIsNone(query.citations[0]["attachment_id"])
        self.assertIsNone(query.citations[0]["attachment_name"])

    def test_a_password_protected_pdf_fails_with_a_safe_message(self):
        attachment = self.upload(make_pdf(BOILER, password="hunter2"))

        self.assertEqual(attachment.status, Attachment.Status.FAILED)
        self.assertEqual(attachment.error, extraction.ENCRYPTED)
        self.assertFalse(self.chunks_of(attachment).exists())

    def test_a_pdf_that_opens_without_a_password_is_read(self):
        # Encrypted only to restrict printing and copying, as many statements are.
        attachment = self.upload(make_pdf(BOILER, password=""))

        self.assertEqual(attachment.status, Attachment.Status.READY)
        self.assertIn("Ravi", attachment.extracted_text)

    def test_a_damaged_pdf_fails_and_never_crashes_the_task(self):
        for content in (GARBAGE_PDF, b"%PDF-1.7\n" + bytes(range(256)) * 40):
            with self.subTest(size=len(content)):
                with self.assertLogs("notes.extraction", "WARNING"):
                    attachment = self.upload(content, name=f"bad{len(content)}.pdf")
                self.assertEqual(attachment.status, Attachment.Status.FAILED)
                self.assertEqual(attachment.error, extraction.UNREADABLE_PDF)

    @override_settings(ATTACHMENT_PDF_MAX_PAGES=2)
    def test_pages_past_the_cap_are_not_read(self):
        attachment = self.upload(make_pdf("first page", "second page", "third page"))

        self.assertEqual(attachment.status, Attachment.Status.READY)
        self.assertIn("second page", attachment.extracted_text)
        self.assertNotIn("third", attachment.extracted_text)

    @override_settings(ATTACHMENT_TEXT_MAX_CHARS=60)
    def test_text_past_the_cap_is_not_kept(self):
        attachment = self.upload(make_pdf("word " * 40, "never reached"))

        self.assertEqual(attachment.status, Attachment.Status.READY)
        self.assertLessEqual(len(attachment.extracted_text), 60)
        self.assertNotIn("never", attachment.extracted_text)

    @override_settings(ATTACHMENT_PDF_MAX_STREAM_BYTES=1000)
    def test_a_stream_that_inflates_past_the_cap_fails(self):
        # 841 compressed bytes that inflate to 12,000: a small decompression bomb.
        bomb = make_pdf("\n".join(["the same line again and again"] * 400), compress=True)
        self.assertLess(len(bomb), 2000)

        with self.assertLogs("notes.extraction", "WARNING"):
            attachment = self.upload(bomb)

        self.assertEqual(attachment.status, Attachment.Status.FAILED)
        self.assertEqual(attachment.error, extraction.UNREADABLE_PDF)

    def test_the_stream_cap_is_what_stops_it(self):
        # The same file, with the default cap, reads fine.
        attachment = self.upload(
            make_pdf("\n".join(["the same line again and again"] * 400), compress=True)
        )
        self.assertEqual(attachment.status, Attachment.Status.READY)

    def test_a_pdf_without_text_is_ready_with_nothing_indexed(self):
        attachment = self.upload(make_pdf(""))

        self.assertEqual(attachment.status, Attachment.Status.READY)
        self.assertEqual(attachment.extracted_text, "")
        self.assertFalse(self.chunks_of(attachment).exists())

    def test_a_missing_file_fails(self):
        with mock.patch.object(
            extraction,
            "_read_file",
            side_effect=extraction.ExtractionFailed(extraction.MISSING_FILE),
        ):
            attachment = self.upload(make_pdf(BOILER))
        self.assertEqual(attachment.error, extraction.MISSING_FILE)

    def test_nul_and_control_characters_are_removed(self):
        self.assertEqual(extraction.clean_text("a\x00b\x07c\r\nd\fe \n\n\n\nf"), "abc\nd\fe\n\nf")
        self.assertEqual(extraction.clean_text("x\ud800y"), "x?y")


class ImageTests(ExtractionTestCase):
    def test_an_images_text_is_read_by_the_provider_and_searchable(self):
        attachment = self.upload(PNG, name="whiteboard.png")

        self.assertEqual(attachment.status, Attachment.Status.READY)
        self.assertEqual(attachment.extracted_text, FAKE_IMAGE_TEXT)
        hit = search(self.alice, "scooter insurance")[0]
        self.assertEqual(
            (hit.attachment_id, hit.attachment_name), (attachment.pk, "whiteboard.png")
        )

    def test_it_counts_one_image_text_use_for_the_owner_with_its_cost(self):
        self.upload(PNG, name="whiteboard.png")

        event = UsageEvent.objects.get(key="image_text")
        self.assertEqual(event.user_id, self.alice.pk)
        self.assertFalse(event.refunded)
        self.assertEqual((event.provider, event.model), ("fake", "fake"))
        self.assertGreater(event.input_tokens, 0)

    def test_the_provider_gets_the_bytes_and_the_sniffed_type(self):
        result = ChatResult(text="Hello", provider="fake", model="fake")
        with mock.patch(IMAGE_CALL, return_value=result) as call:
            self.upload(PNG, name="photo.jpg")  # the name lies; the bytes are a PNG

        call.assert_called_once_with(PNG, "image/png")

    def test_an_image_with_no_text_is_ready_with_nothing_indexed(self):
        with mock.patch(IMAGE_CALL, return_value=ChatResult(text="", provider="f", model="f")):
            attachment = self.upload(PNG, name="cat.png")

        self.assertEqual(attachment.status, Attachment.Status.READY)
        self.assertFalse(self.chunks_of(attachment).exists())

    def test_a_refusal_fails_it_and_refunds_the_use(self):
        with (
            mock.patch(IMAGE_CALL, side_effect=ChatError("blocked sk-secret")),
            self.assertLogs("notes.extraction", "WARNING"),
        ):
            attachment = self.upload(PNG)

        self.assertEqual(attachment.status, Attachment.Status.FAILED)
        self.assertEqual(attachment.error, extraction.IMAGE_UNREADABLE)
        self.assertTrue(UsageEvent.objects.get(key="image_text").refunded)

    def test_a_billed_failure_keeps_the_use_and_records_its_cost(self):
        error = BilledChatError("cut off", provider="p", model="m", input_tokens=9, output_tokens=2)
        with (
            mock.patch(IMAGE_CALL, side_effect=error),
            self.assertLogs("notes.extraction", "WARNING"),
        ):
            attachment = self.upload(PNG)

        self.assertEqual(attachment.status, Attachment.Status.FAILED)
        self.assertEqual(attachment.error, extraction.IMAGE_UNREADABLE)
        event = UsageEvent.objects.get(key="image_text")
        self.assertFalse(event.refunded)
        self.assertEqual(
            (event.provider, event.model, event.input_tokens, event.output_tokens),
            ("p", "m", 9, 2),
        )

    def test_a_provider_that_cannot_read_images_fails_it_clearly(self):
        with (
            mock.patch(IMAGE_CALL, side_effect=ImageTextNotSupported("no vision")),
            self.assertLogs("notes.extraction", "WARNING"),
        ):
            attachment = self.upload(PNG)

        self.assertEqual(attachment.error, extraction.IMAGE_NOT_SUPPORTED)
        self.assertTrue(UsageEvent.objects.get(key="image_text").refunded)

    def test_the_system_limit_reached_fails_it_without_a_call(self):
        UsageEvent.objects.create(user=None, key="image_text")
        with (
            override_settings(LIMIT_DEFAULTS=image_text_limit(1)),
            mock.patch(IMAGE_CALL) as call,
            self.assertLogs("limits.service", "WARNING"),
        ):
            attachment = self.upload(PNG)

        call.assert_not_called()
        self.assertEqual(attachment.error, extraction.IMAGE_PAUSED)

    def test_the_users_limit_reached_fails_it_without_a_call(self):
        UsageEvent.objects.create(user=self.alice, key="image_text")
        with (
            override_settings(LIMIT_DEFAULTS=image_text_limit(100, user_free=1)),
            mock.patch(IMAGE_CALL) as call,
        ):
            attachment = self.upload(PNG)

        call.assert_not_called()
        self.assertEqual(attachment.status, Attachment.Status.FAILED)
        self.assertEqual(attachment.error, extraction.IMAGE_LIMIT_REACHED)
        self.assertEqual(UsageEvent.objects.filter(key="image_text").count(), 1)

    def test_another_users_use_does_not_count_against_the_owner(self):
        UsageEvent.objects.create(user=self.bob, key="image_text")
        with override_settings(LIMIT_DEFAULTS=image_text_limit(100, user_free=1)):
            attachment = self.upload(PNG)

        self.assertEqual(attachment.status, Attachment.Status.READY)

    def test_the_default_per_user_limits(self):
        rule = settings.LIMIT_DEFAULTS["image_text"]
        self.assertEqual((rule["user_free"], rule["user_premium"]), (50, 250))

    def test_the_use_is_consumed_under_the_owners_lock(self):
        real_lock, real_consume = services._lock_owner, extraction.limits.consume
        locked, seen = [], []

        def lock(owner):
            row = real_lock(owner)
            locked.append(row)
            return row

        def consume(user, key, *args, **kwargs):
            if key == extraction.IMAGE_TEXT_KEY:
                seen.append((user, locked[-1] if locked else None))
            return real_consume(user, key, *args, **kwargs)

        with (
            mock.patch.object(services, "_lock_owner", side_effect=lock),
            mock.patch.object(services.limits, "consume", side_effect=consume),
        ):
            self.upload(PNG)

        # Consumed once, with the very row the owner lock had just returned:
        # the per-user check runs under that lock, reading the plan from it.
        self.assertEqual(len(seen), 1)
        user, row = seen[0]
        self.assertIs(user, row)
        self.assertEqual(user.pk, self.alice.pk)

    def test_the_same_image_again_reuses_its_text_without_a_call_or_a_charge(self):
        first = self.upload(PNG, name="whiteboard.png")
        other = services.create_note(self.alice, title="Elsewhere", content=doc("x"))
        with mock.patch(IMAGE_CALL) as call:
            again = self.upload(PNG, name="copy.png", note=other)

        call.assert_not_called()
        self.assertEqual(again.status, Attachment.Status.READY)
        self.assertEqual(again.extracted_text, first.extracted_text)
        self.assertGreater(self.chunks_of(again).count(), 0)
        self.assertEqual(UsageEvent.objects.filter(key="image_text").count(), 1)

    def test_a_deleted_earlier_copy_is_reused_too(self):
        first = self.upload(PNG)
        services.delete_attachment(self.alice, first.pk)
        with mock.patch(IMAGE_CALL) as call:
            again = self.upload(PNG)

        call.assert_not_called()
        self.assertEqual(again.extracted_text, FAKE_IMAGE_TEXT)

    def test_an_earlier_empty_reading_is_reused(self):
        empty = ChatResult(text="", provider="f", model="f")
        with mock.patch(IMAGE_CALL, return_value=empty):
            self.upload(PNG, name="cat.png")
        other = services.create_note(self.alice, title="Elsewhere", content=doc("x"))
        with mock.patch(IMAGE_CALL) as call:
            again = self.upload(PNG, name="cat.png", note=other)

        call.assert_not_called()
        self.assertEqual(again.status, Attachment.Status.READY)

    def test_another_owners_copy_is_never_reused(self):
        bobs_note = services.create_note(self.bob, title="Bob", content=doc("y"))
        self.upload(PNG, owner=self.bob, note=bobs_note)
        result = ChatResult(text="Alice's own reading", provider="fake", model="fake")
        with mock.patch(IMAGE_CALL, return_value=result) as call:
            mine = self.upload(PNG)

        call.assert_called_once()
        self.assertEqual(mine.extracted_text, "Alice's own reading")

    def test_a_failed_earlier_copy_is_not_reused(self):
        with (
            mock.patch(IMAGE_CALL, side_effect=ChatError("blocked")),
            self.assertLogs("notes.extraction", "WARNING"),
        ):
            failed = self.upload(PNG)
        services.delete_attachment(self.alice, failed.pk)
        with mock.patch(IMAGE_CALL, wraps=chat.extract_image_text) as call:
            again = self.upload(PNG)

        call.assert_called_once()
        self.assertEqual(again.status, Attachment.Status.READY)

    @override_settings(ATTACHMENT_IMAGE_TEXT_MAX_BYTES=10)
    def test_an_image_over_the_vision_cap_fails_without_a_call(self):
        with mock.patch(IMAGE_CALL) as call:
            attachment = self.upload(PNG)

        call.assert_not_called()
        self.assertEqual(attachment.error, extraction.IMAGE_TOO_LARGE)
        self.assertFalse(UsageEvent.objects.filter(key="image_text").exists())


class StatusAndSyncTests(ExtractionTestCase):
    def test_each_status_change_takes_a_revision_and_changes_carries_it(self):
        before = self.revision()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            response = self.client.post(
                f"{NOTES}{self.note.pk}/attachments/",
                {"file": SimpleUploadedFile("r.pdf", make_pdf(BOILER))},
                format="multipart",
            )
        attachment_id = response.json()["id"]
        self.assertEqual(self.revision(), before + 1)  # the upload

        for callback in callbacks:
            callback()

        # pending -> extracting, extracting -> ready.
        self.assertEqual(self.revision(), before + 3)
        results = self.client.get(CHANGES, {"after": before + 1}).json()["results"]
        sent = next(n for n in results if n["id"] == self.note.pk)["attachments"]
        self.assertEqual([(a["id"], a["status"]) for a in sent], [(attachment_id, "ready")])
        self.assertNotIn("extracted_text", sent[0])

    def test_running_the_task_twice_is_harmless(self):
        attachment = self.upload(make_pdf(BOILER))
        chunk_ids = set(self.chunks_of(attachment).values_list("pk", flat=True))
        revision = self.revision()

        extract_attachment.delay(attachment.pk)

        attachment.refresh_from_db()
        self.assertEqual(attachment.status, Attachment.Status.READY)
        self.assertEqual(set(self.chunks_of(attachment).values_list("pk", flat=True)), chunk_ids)
        self.assertEqual(self.revision(), revision)

    def test_a_redelivered_task_finishes_an_extracting_attachment_once(self):
        with self.captureOnCommitCallbacks(execute=False):
            response = self.client.post(
                f"{NOTES}{self.note.pk}/attachments/",
                {"file": SimpleUploadedFile("r.pdf", make_pdf(BOILER))},
                format="multipart",
            )
        attachment = Attachment.objects.get(pk=response.json()["id"])
        services.start_extraction(attachment.pk)  # a worker took it, then died
        revision = self.revision()

        extract_attachment.delay(attachment.pk)
        extract_attachment.delay(attachment.pk)

        attachment.refresh_from_db()
        self.assertEqual(attachment.status, Attachment.Status.READY)
        # No revision for extracting -> extracting; one for ready; none for the duplicate.
        self.assertEqual(self.revision(), revision + 1)
        count = self.chunks_of(attachment).count()
        self.assertGreater(count, 0)
        self.assertEqual(
            count, len({c.ordinal for c in self.chunks_of(attachment)}), "duplicate chunks"
        )

    def test_a_retry_after_reading_does_not_read_or_pay_again(self):
        real = extraction.embed_attachment
        calls = []

        def flaky(attachment, text):
            calls.append(text)
            if len(calls) == 1:
                raise EmbeddingTransientError("429")
            return real(attachment, text)

        with self.captureOnCommitCallbacks(execute=False):
            response = self.client.post(
                f"{NOTES}{self.note.pk}/attachments/",
                {"file": SimpleUploadedFile("w.png", PNG)},
                format="multipart",
            )
        attachment_id = response.json()["id"]
        with (
            mock.patch.object(extraction, "embed_attachment", side_effect=flaky),
            mock.patch(IMAGE_CALL, wraps=chat.extract_image_text) as image_call,
        ):
            # throw=False: apply() runs the retry inline instead of raising Retry.
            extract_attachment.apply((attachment_id,), throw=False)

        self.assertEqual(image_call.call_count, 1)
        self.assertEqual(calls, [FAKE_IMAGE_TEXT, FAKE_IMAGE_TEXT])
        self.assertEqual(Attachment.objects.get(pk=attachment_id).status, "ready")
        self.assertEqual(UsageEvent.objects.filter(key="image_text", refunded=False).count(), 1)

    def test_transient_errors_are_retried_then_fail_it_as_busy(self):
        with self.captureOnCommitCallbacks(execute=False):
            response = self.client.post(
                f"{NOTES}{self.note.pk}/attachments/",
                {"file": SimpleUploadedFile("w.png", PNG)},
                format="multipart",
            )
        attachment_id = response.json()["id"]
        with mock.patch(IMAGE_CALL, side_effect=TransientChatError("503 sk-secret")):
            with self.assertRaises(Retry):
                extract_attachment.apply((attachment_id,), retries=0)
            self.assertEqual(Attachment.objects.get(pk=attachment_id).status, "extracting")
            with self.assertLogs("notes.tasks", "ERROR"):
                extract_attachment.apply((attachment_id,), retries=extract_attachment.max_retries)

        attachment = Attachment.objects.get(pk=attachment_id)
        self.assertEqual(attachment.status, Attachment.Status.FAILED)
        self.assertEqual(attachment.error, extraction.BUSY)
        # Every attempt's use was handed back.
        self.assertFalse(UsageEvent.objects.filter(key="image_text", refunded=False).exists())

    def test_the_soft_time_limit_fails_it(self):
        with (
            mock.patch.object(extraction, "pdf_text", side_effect=SoftTimeLimitExceeded()),
            self.assertLogs("notes.tasks", "WARNING"),
        ):
            attachment = self.upload(make_pdf(BOILER))

        self.assertEqual(attachment.status, Attachment.Status.FAILED)
        self.assertEqual(attachment.error, extraction.TOO_SLOW)

    def test_an_unexpected_error_fails_it_and_is_raised(self):
        with self.captureOnCommitCallbacks(execute=False):
            response = self.client.post(
                f"{NOTES}{self.note.pk}/attachments/",
                {"file": SimpleUploadedFile("r.pdf", make_pdf(BOILER))},
                format="multipart",
            )
        attachment_id = response.json()["id"]
        with (
            mock.patch.object(extraction, "pdf_text", side_effect=RuntimeError("bug")),
            self.assertLogs("notes.tasks", "ERROR"),
            self.assertRaises(RuntimeError),
        ):
            extract_attachment.delay(attachment_id)

        attachment = Attachment.objects.get(pk=attachment_id)
        self.assertEqual((attachment.status, attachment.error), ("failed", extraction.UNEXPECTED))

    def test_a_permanent_embedding_error_fails_it(self):
        with (
            mock.patch.object(extraction, "embed_attachment", side_effect=EmbeddingError("key")),
            self.assertLogs("notes.extraction", "WARNING"),
        ):
            attachment = self.upload(make_pdf(BOILER))

        self.assertEqual(attachment.error, extraction.NOT_INDEXED)

    def test_a_broker_that_is_down_is_logged_not_raised(self):
        with (
            mock.patch.object(extract_attachment, "delay", side_effect=OSError("no broker")),
            self.assertLogs("notes.services", "ERROR"),
        ):
            attachment = self.upload(make_pdf(BOILER))

        self.assertEqual(attachment.status, Attachment.Status.PENDING)


class IndependenceTests(ExtractionTestCase):
    def test_reindexing_the_note_keeps_the_attachments_chunks(self):
        attachment = self.upload(make_pdf(BOILER))
        chunk_ids = set(self.chunks_of(attachment).values_list("pk", flat=True))

        note = services.update_note(
            self.alice, self.note.pk, expected_version=self.note.version, content=doc("New text")
        )
        index_note(note.pk, note.version)

        self.assertEqual(set(self.chunks_of(attachment).values_list("pk", flat=True)), chunk_ids)
        own = NoteChunk.objects.filter(note=self.note, source="note")
        self.assertEqual([c.text for c in own], ["New text"])

    def test_extracting_keeps_the_notes_own_chunks(self):
        own = set(NoteChunk.objects.filter(note=self.note).values_list("pk", flat=True))

        self.upload(make_pdf(BOILER))

        self.assertTrue(
            own <= set(NoteChunk.objects.filter(note=self.note).values_list("pk", flat=True))
        )

    def test_reindexing_an_attachment_reuses_its_vectors(self):
        attachment = self.upload(make_pdf(BOILER))
        with mock.patch("retrieval.indexing.embed_texts") as embed:
            self.assertTrue(index_attachment(attachment.pk))
        embed.assert_not_called()
        self.assertTrue(self.chunks_of(attachment).exists())


class DeletionTests(ExtractionTestCase):
    def test_deleting_the_attachment_removes_its_chunks_from_search(self):
        attachment = self.upload(make_pdf(BOILER))
        self.assertTrue(self.chunks_of(attachment).exists())

        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.delete(f"{ATTACHMENTS}{attachment.pk}/").status_code, 204)

        self.assertFalse(self.chunks_of(attachment).exists())
        hits = search(self.alice, "boiler serviced Ravi")
        self.assertEqual([h.attachment_id for h in hits if h.attachment_id], [])
        # The note's own chunks stay.
        self.assertTrue(NoteChunk.objects.filter(note=self.note, source="note").exists())

    def test_deleting_the_note_removes_its_attachments_chunks(self):
        attachment = self.upload(make_pdf(BOILER))

        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.delete(f"{NOTES}{self.note.pk}/").status_code, 204)

        self.assertFalse(self.chunks_of(attachment).exists())
        self.assertFalse(NoteChunk.objects.filter(note=self.note).exists())
        self.assertEqual(search(self.alice, "boiler serviced Ravi"), [])

    def test_an_attachment_deleted_while_it_is_read_is_never_indexed(self):
        real = extraction.embed_attachment

        def delete_meanwhile(attachment, text):
            result = real(attachment, text)
            services.delete_attachment(self.alice, attachment.pk)
            return result

        with mock.patch.object(extraction, "embed_attachment", side_effect=delete_meanwhile):
            attachment = self.upload(make_pdf(BOILER))

        self.assertIsNotNone(attachment.deleted_at)
        self.assertEqual(attachment.status, Attachment.Status.EXTRACTING)
        self.assertFalse(self.chunks_of(attachment).exists())

    def test_a_deleted_attachment_is_not_extracted(self):
        with self.captureOnCommitCallbacks(execute=False):
            response = self.client.post(
                f"{NOTES}{self.note.pk}/attachments/",
                {"file": SimpleUploadedFile("r.pdf", make_pdf(BOILER))},
                format="multipart",
            )
        attachment_id = response.json()["id"]
        services.delete_attachment(self.alice, attachment_id)
        revision = self.revision()

        extract_attachment.delay(attachment_id)

        attachment = Attachment.objects.get(pk=attachment_id)
        self.assertEqual(attachment.status, Attachment.Status.PENDING)
        self.assertEqual(attachment.extracted_text, "")
        self.assertEqual(self.revision(), revision)


class OwnershipTests(ExtractionTestCase):
    def test_another_users_attachment_never_appears_in_search(self):
        bobs_note = services.create_note(self.bob, title="Bob's house", content=doc("Roof"))
        bobs = self.upload(make_pdf(BOILER), owner=self.bob, note=bobs_note)
        self.assertEqual(bobs.status, Attachment.Status.READY)
        self.assertTrue(self.chunks_of(bobs).filter(owner=self.bob).exists())

        # Alice's own note is all she can find (the fake vectors match anything a little).
        hits = search(self.alice, "boiler serviced Ravi")
        self.assertEqual({(h.note_id, h.attachment_id) for h in hits}, {(self.note.pk, None)})
        body = self.client.get("/api/v1/search/", {"q": "boiler serviced Ravi"}).json()
        self.assertEqual({(h["note_id"], h["attachment_id"]) for h in body}, {(self.note.pk, None)})
        self.assertEqual(search(self.bob, "boiler serviced Ravi")[0].attachment_id, bobs.pk)

    def test_an_ask_never_cites_another_users_attachment(self):
        bobs_note = services.create_note(self.bob, title="Bob's house", content=doc("Roof"))
        self.upload(make_pdf(BOILER), owner=self.bob, note=bobs_note)
        query = AskQuery.objects.create(
            user=self.alice, question="Who serviced the boiler?", idempotency_key="k3"
        )

        answer_ask.delay(query.pk)

        query.refresh_from_db()
        self.assertTrue(all(c["attachment_id"] is None for c in query.citations))
        self.assertTrue(all(row["note_id"] == self.note.pk for row in query.retrieved))


class SweepTests(ExtractionTestCase):
    def make(self, status, age):
        with self.captureOnCommitCallbacks(execute=False):
            response = self.client.post(
                f"{NOTES}{self.note.pk}/attachments/",
                {"file": SimpleUploadedFile(f"{status}{age}.pdf", make_pdf(f"{status} {age}"))},
                format="multipart",
            )
        pk = response.json()["id"]
        Attachment.objects.filter(pk=pk).update(
            status=status, created_at=timezone.now() - timedelta(seconds=age)
        )
        return pk

    def test_fails_only_unfinished_attachments_past_the_cutoff(self):
        old = settings.ATTACHMENT_STUCK_AFTER_SECONDS + 60
        stuck_pending = self.make("pending", old)
        stuck_extracting = self.make("extracting", old)
        recent = self.make("pending", 10)
        done = self.make("ready", old)

        with self.assertLogs("notes.tasks", "WARNING"):
            self.assertEqual(sweep_stuck_attachments(), 2)

        statuses = dict(Attachment.objects.values_list("pk", "status"))
        self.assertEqual(statuses[stuck_pending], "failed")
        self.assertEqual(statuses[stuck_extracting], "failed")
        self.assertEqual(statuses[recent], "pending")
        self.assertEqual(statuses[done], "ready")
        self.assertEqual(Attachment.objects.get(pk=stuck_pending).error, extraction.TOO_SLOW)

    def test_is_scheduled(self):
        entry = settings.CELERY_BEAT_SCHEDULE["sweep-stuck-attachments"]
        self.assertEqual(entry["task"], "notes.tasks.sweep_stuck_attachments")

    def test_the_cutoff_outlasts_the_retries(self):
        task = extract_attachment
        worst = (task.max_retries + 1) * settings.ATTACHMENT_EXTRACT_TIME_LIMIT
        worst += task.max_retries * task.retry_backoff_max
        self.assertGreater(settings.ATTACHMENT_STUCK_AFTER_SECONDS, worst)
