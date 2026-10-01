"""Attachments: sniffing and names, the upload/download/delete API, sync, the admin."""

import io
import os
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.storage import storages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.uploadhandler import FileUploadHandler
from django.core.handlers.asgi import ASGIRequest
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from django.utils import timezone
from rest_framework.test import APIClient, force_authenticate

from limits.models import UsageEvent
from notes import services
from notes.api.attachments import NoteAttachmentsView
from notes.attachments import (
    AttachmentStorage,
    attachment_path,
    clean_name,
    sniff_type,
)
from notes.models import Attachment, Note

from .helpers import doc, isolate_attachment_storage, make_user

NOTES = "/api/v1/notes/"
ATTACHMENTS = "/api/v1/attachments/"
CHANGES = "/api/v1/notes/changes/"

PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << >>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 24
WEBP = b"RIFF" + (28).to_bytes(4, "little") + b"WEBPVP8 " + b"\x00" * 20
EXE = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff" + b"\x00" * 48


def upload(content=PDF, name="receipt.pdf", content_type="application/pdf"):
    return SimpleUploadedFile(name, content, content_type=content_type)


def storage_limit(user, system=None):
    """LIMIT_DEFAULTS with ``storage_bytes`` set, for override_settings."""
    default = settings.LIMIT_DEFAULTS["storage_bytes"]
    return {
        **settings.LIMIT_DEFAULTS,
        "storage_bytes": {
            **default,
            "user_free": user,
            "user_premium": user,
            "system": default["system"] if system is None else system,
        },
    }


def stored_files():
    """Every file in the (test) attachment storage, relative to its root."""
    root = storages["attachments"].location
    found = set()
    for folder, _, files in os.walk(root):
        for name in files:
            found.add(os.path.relpath(os.path.join(folder, name), root))
    return found


def read_stored(name):
    with storages["attachments"].open(name) as handle:
        return handle.read()


def revision_of(user):
    user.refresh_from_db(fields=["notes_revision"])
    return user.notes_revision


class SniffTests(SimpleTestCase):
    def test_the_four_types_are_read_from_their_first_bytes(self):
        for content, expected in [
            (PDF, "application/pdf"),
            (PNG, "image/png"),
            (JPEG, "image/jpeg"),
            (WEBP, "image/webp"),
        ]:
            with self.subTest(expected):
                self.assertEqual(sniff_type(content[:16]), expected)

    def test_anything_else_is_refused(self):
        for content in [
            EXE,
            b"",
            b"<!doctype html><script>alert(1)</script>",
            b"GIF89a" + b"\x00" * 10,
            b"RIFF\x00\x00\x00\x00WAVEfmt ",
            # A PDF signature that does not start the file (a polyglot).
            b"MZ" + b"\x00" * 6 + b"%PDF-1.4",
            b"\xef\xbb\xbf%PDF-1.4",
        ]:
            with self.subTest(content[:12]):
                self.assertIsNone(sniff_type(content[:16]))


class CleanNameTests(SimpleTestCase):
    def test_only_the_last_path_component_is_kept(self):
        self.assertEqual(clean_name("../../etc/passwd.pdf", "application/pdf"), "passwd.pdf")
        self.assertEqual(clean_name("C:\\Users\\me\\scan.pdf", "application/pdf"), "scan.pdf")

    def test_control_and_direction_characters_are_removed(self):
        # U+202E would show "invoice\u202efdp.exe" as "invoiceexe.pdf".
        self.assertEqual(
            clean_name("invoice\u202efdp.exe", "application/pdf"), "invoicefdp.exe.pdf"
        )
        self.assertEqual(clean_name("a\r\nb\x00c.png", "image/png"), "abc.png")

    def test_the_sniffed_types_extension_is_always_there(self):
        self.assertEqual(clean_name("setup.exe", "application/pdf"), "setup.exe.pdf")
        self.assertEqual(clean_name("report", "application/pdf"), "report.pdf")
        self.assertEqual(clean_name("photo.JPEG", "image/jpeg"), "photo.JPEG")
        self.assertEqual(clean_name("photo.jpg", "image/jpeg"), "photo.jpg")
        self.assertEqual(clean_name("x.pdf", "image/png"), "x.pdf.png")

    def test_empty_and_dot_names_fall_back(self):
        self.assertEqual(clean_name("", "image/webp"), "attachment.webp")
        self.assertEqual(clean_name(None, "image/webp"), "attachment.webp")
        self.assertEqual(clean_name(" .. ", "application/pdf"), "attachment.pdf")
        self.assertEqual(clean_name(".hidden.png", "image/png"), "hidden.png")

    def test_long_names_are_cut_keeping_the_extension(self):
        name = clean_name("a" * 400 + ".pdf", "application/pdf")
        self.assertEqual(len(name), 255)
        self.assertTrue(name.endswith("a.pdf"))

    def test_nfc(self):
        self.assertEqual(clean_name("cafe\u0301.pdf", "application/pdf"), "caf\u00e9.pdf")


class StorageNameTests(SimpleTestCase):
    def test_the_stored_name_is_random_and_ignores_the_clients(self):
        instance = Attachment(mime_type="application/pdf")
        first = attachment_path(instance, "../../receipt.html")
        second = attachment_path(instance, "../../receipt.html")

        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^attachments/[0-9a-f]{32}\.pdf$")

    def test_no_url_is_ever_made(self):
        with self.assertRaises(NotImplementedError):
            AttachmentStorage().url("attachments/x.pdf")

    def test_tests_store_in_a_temp_dir(self):
        location = str(storages["attachments"].location)
        self.assertNotEqual(location, str(settings.MEDIA_ROOT))
        self.assertIn("sn-test-attachments-", location)


class AttachmentApiTestCase(TestCase):
    def setUp(self):
        isolate_attachment_storage(self)
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        self.client = APIClient()
        self.client.force_authenticate(self.alice)
        self.bobs_client = APIClient()
        self.bobs_client.force_authenticate(self.bob)
        self.note = services.create_note(self.alice, title="Car service", content=doc("tyres"))
        self.files_before = stored_files()

    def post(self, file=None, note=None, client=None, **extra):
        note_id = (note or self.note).pk
        return (client or self.client).post(
            f"{NOTES}{note_id}/attachments/",
            {"file": file or upload()},
            format="multipart",
            **extra,
        )

    def new_files(self):
        return stored_files() - self.files_before


class UploadTests(AttachmentApiTestCase):
    def test_a_pdf_is_stored_under_a_random_name(self):
        response = self.post()

        self.assertEqual(response.status_code, 201, response.data)
        body = response.json()
        self.assertEqual(body["note"], self.note.pk)
        self.assertEqual(body["original_name"], "receipt.pdf")
        self.assertEqual(body["mime_type"], "application/pdf")
        self.assertEqual(body["size"], len(PDF))
        self.assertEqual(body["status"], "pending")
        self.assertEqual(len(body["sha256"]), 64)
        self.assertNotIn("file", body)
        self.assertNotIn("extracted_text", body)

        attachment = Attachment.objects.get(pk=body["id"])
        self.assertEqual(attachment.owner, self.alice)
        self.assertNotIn("receipt", attachment.file.name)
        self.assertEqual(self.new_files(), {attachment.file.name})
        self.assertEqual(read_stored(attachment.file.name), PDF)

    def test_each_allowed_type_is_accepted(self):
        for content, mime_type in [
            (PNG, "image/png"),
            (JPEG, "image/jpeg"),
            (WEBP, "image/webp"),
        ]:
            with self.subTest(mime_type):
                response = self.post(upload(content, "picture", "application/octet-stream"))
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.json()["mime_type"], mime_type)

    def test_a_renamed_exe_is_refused_by_its_bytes(self):
        response = self.post(upload(EXE, "invoice.pdf", "application/pdf"))

        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json()["code"], "unsupported_file_type")
        self.assertFalse(Attachment.objects.exists())
        self.assertFalse(UsageEvent.objects.filter(key="storage_bytes").exists())
        self.assertEqual(self.new_files(), set())

    def test_the_clients_type_and_extension_are_ignored(self):
        response = self.post(upload(PNG, "scan.pdf", "application/pdf"))

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["mime_type"], "image/png")
        self.assertEqual(response.json()["original_name"], "scan.pdf.png")

    @override_settings(ATTACHMENT_MAX_BYTES=64)
    def test_a_file_over_the_cap_is_413(self):
        response = self.post(upload(PDF + b"x" * 64))

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["code"], "too_large")
        self.assertFalse(Attachment.objects.exists())
        self.assertEqual(self.new_files(), set())

    @override_settings(ATTACHMENT_MAX_BYTES=len(PDF))
    def test_a_file_at_the_cap_is_accepted(self):
        self.assertEqual(self.post().status_code, 201)

    @override_settings(ATTACHMENT_MAX_BYTES=64, CORS_ALLOWED_ORIGINS=["http://app.test"])
    def test_the_413_is_readable_cross_origin(self):
        response = self.post(upload(PDF + b"x" * 64), HTTP_ORIGIN="http://app.test")

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response["Access-Control-Allow-Origin"], "http://app.test")

    def test_no_file_or_an_empty_one_is_400(self):
        response = self.client.post(
            f"{NOTES}{self.note.pk}/attachments/", {"other": "x"}, format="multipart"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("file", response.json())

        self.assertEqual(self.post(upload(b"")).status_code, 400)

    def test_json_is_not_accepted(self):
        response = self.client.post(
            f"{NOTES}{self.note.pk}/attachments/", {"file": "JVBERi0="}, format="json"
        )
        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json()["code"], "unsupported_media_type")

    def test_the_same_file_again_returns_the_existing_attachment(self):
        first = self.post().json()
        revision = revision_of(self.alice)

        response = self.post(upload(PDF, "copy.pdf"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], first["id"])
        self.assertEqual(response.json()["original_name"], "receipt.pdf")
        self.assertEqual(Attachment.objects.count(), 1)
        self.assertEqual(UsageEvent.objects.filter(key="storage_bytes").count(), 1)
        self.assertEqual(len(self.new_files()), 1)
        self.assertEqual(revision_of(self.alice), revision)

    def test_a_duplicate_found_under_the_lock_deletes_the_bytes_it_stored(self):
        first = self.post().json()
        existing = Attachment.objects.get(pk=first["id"])
        # As if the unlocked look-up ran before the first upload committed.
        with mock.patch.object(services, "_live_attachment", side_effect=[None, existing]):
            response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], first["id"])
        self.assertEqual(len(self.new_files()), 1)

    def test_the_same_file_on_another_note_is_a_new_attachment(self):
        other = services.create_note(self.alice, title="Other")
        self.post()

        response = self.post(note=other)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Attachment.objects.count(), 2)

    def test_the_same_file_after_a_delete_is_a_new_attachment(self):
        first = self.post().json()
        self.client.delete(f"{ATTACHMENTS}{first['id']}/")

        response = self.post()

        self.assertEqual(response.status_code, 201)
        self.assertNotEqual(response.json()["id"], first["id"])

    def test_another_users_note_is_404_and_nothing_is_stored(self):
        response = self.post(client=self.bobs_client)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Attachment.objects.exists())
        self.assertEqual(self.new_files(), set())

    def test_a_deleted_note_is_404(self):
        services.delete_note(self.alice, self.note.pk)
        self.assertEqual(self.post().status_code, 404)

    def test_a_note_deleted_before_the_lock_is_404_and_its_bytes_are_deleted(self):
        real = services._locked_live_note

        def deleted_meanwhile(owner, note_id):
            Note.objects.filter(pk=note_id).update(deleted_at=timezone.now())
            return real(owner, note_id)

        with mock.patch.object(services, "_locked_live_note", side_effect=deleted_meanwhile):
            response = self.post()

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.new_files(), set())

    def test_an_upload_stamps_the_notes_revision_but_not_its_version(self):
        before = revision_of(self.alice)
        updated_at = self.note.updated_at

        self.post()

        self.note.refresh_from_db()
        self.assertEqual(self.note.revision, before + 1)
        self.assertEqual(revision_of(self.alice), before + 1)
        self.assertEqual(self.note.version, 1)
        self.assertEqual(self.note.updated_at, updated_at)

    def test_an_upload_records_its_size_against_storage(self):
        self.post()

        event = UsageEvent.objects.get(key="storage_bytes")
        self.assertEqual((event.user, event.amount, event.refunded), (self.alice, len(PDF), False))
        self.assertEqual(Attachment.objects.get().usage_event, event)
        limits = self.client.get("/api/v1/me/").json()["limits"]
        self.assertEqual(limits["storage_bytes"]["used"], len(PDF))


class StorageQuotaTests(AttachmentApiTestCase):
    @override_settings(LIMIT_DEFAULTS=storage_limit(len(PDF) + 10))
    def test_a_file_that_does_not_fit_is_429_and_not_kept(self):
        self.assertEqual(self.post().status_code, 201)

        response = self.post(upload(PNG, "photo.png"))

        self.assertEqual(response.status_code, 429)
        body = response.json()
        self.assertEqual(body["code"], "quota_exceeded")
        self.assertEqual((body["used"], body["limit"]), (len(PDF), len(PDF) + 10))
        self.assertIsNone(body["resets_at"])
        self.assertEqual(Attachment.objects.count(), 1)
        self.assertEqual(UsageEvent.objects.filter(key="storage_bytes").count(), 1)
        self.assertEqual(len(self.new_files()), 1)

    @override_settings(LIMIT_DEFAULTS=storage_limit(len(PDF) + 10))
    def test_deleting_frees_the_space(self):
        first = self.post().json()
        self.assertEqual(self.post(upload(PNG, "photo.png")).status_code, 429)

        self.client.delete(f"{ATTACHMENTS}{first['id']}/")

        self.assertEqual(self.post(upload(PNG, "photo.png")).status_code, 201)

    @override_settings(LIMIT_DEFAULTS=storage_limit(None, system=len(PDF) + 10))
    def test_the_system_limit_is_503_and_not_kept(self):
        services_note = services.create_note(self.bob, title="Bob's")
        self.assertEqual(self.post(note=services_note, client=self.bobs_client).status_code, 201)

        response = self.post(upload(PNG, "photo.png"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "system_limit_reached")
        self.assertEqual(Attachment.objects.count(), 1)
        self.assertEqual(len(self.new_files()), 1)

    @override_settings(LIMIT_DEFAULTS=storage_limit(len(PDF)))
    def test_a_duplicate_costs_nothing_even_when_full(self):
        self.post()
        self.assertEqual(self.post().status_code, 200)


class ReadTests(AttachmentApiTestCase):
    def setUp(self):
        super().setUp()
        self.pdf = Attachment.objects.get(pk=self.post().json()["id"])
        self.png = Attachment.objects.get(pk=self.post(upload(PNG, "photo.png")).json()["id"])

    def test_a_notes_attachments_newest_first(self):
        response = self.client.get(f"{NOTES}{self.note.pk}/attachments/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([a["id"] for a in response.json()["results"]], [self.png.pk, self.pdf.pk])

    def test_deleted_attachments_are_left_out(self):
        self.client.delete(f"{ATTACHMENTS}{self.png.pk}/")
        response = self.client.get(f"{NOTES}{self.note.pk}/attachments/")
        self.assertEqual([a["id"] for a in response.json()["results"]], [self.pdf.pk])

    def test_metadata(self):
        response = self.client.get(f"{ATTACHMENTS}{self.pdf.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["original_name"], "receipt.pdf")

    def test_download(self):
        response = self.client.get(f"{ATTACHMENTS}{self.pdf.pk}/file/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), PDF)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="receipt.pdf"')
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Content-Security-Policy"], "default-src 'none'; sandbox")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["Content-Length"], str(len(PDF)))

    @override_settings(DEBUG=False)
    def test_download_headers_hold_without_debug_too(self):
        response = self.client.get(f"{ATTACHMENTS}{self.png.pk}/file/", secure=True)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_download_whatever_the_client_accepts(self):
        response = self.client.get(f"{ATTACHMENTS}{self.png.pk}/file/", HTTP_ACCEPT="image/png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")

        missing = self.client.get(f"{ATTACHMENTS}999999/file/", HTTP_ACCEPT="application/pdf")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing["Content-Type"], "application/json")

    def test_an_unusual_name_is_sent_safely(self):
        upload_ = upload(JPEG, 'tax "2026" \u00e9t\u00e9.jpg', "image/jpeg")
        attachment_id = self.post(upload_).json()["id"]

        response = self.client.get(f"{ATTACHMENTS}{attachment_id}/file/")

        disposition = response["Content-Disposition"]
        self.assertTrue(disposition.startswith("attachment; filename*=utf-8''"), disposition)
        self.assertNotIn('"2026"', disposition)
        self.assertNotIn("\n", disposition)

    def test_another_user_gets_404_everywhere(self):
        for path in [
            f"{ATTACHMENTS}{self.pdf.pk}/",
            f"{ATTACHMENTS}{self.pdf.pk}/file/",
            f"{NOTES}{self.note.pk}/attachments/",
        ]:
            with self.subTest(path):
                self.assertEqual(self.bobs_client.get(path).status_code, 404)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.bobs_client.delete(f"{ATTACHMENTS}{self.pdf.pk}/")
        self.assertEqual(response.status_code, 404)
        self.pdf.refresh_from_db()
        self.assertIsNone(self.pdf.deleted_at)
        self.assertEqual(read_stored(self.pdf.file.name), PDF)

    def test_a_deleted_attachment_is_404(self):
        self.client.delete(f"{ATTACHMENTS}{self.pdf.pk}/")
        for path in [f"{ATTACHMENTS}{self.pdf.pk}/", f"{ATTACHMENTS}{self.pdf.pk}/file/"]:
            with self.subTest(path):
                self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.delete(f"{ATTACHMENTS}{self.pdf.pk}/").status_code, 404)

    def test_a_missing_file_is_404_and_logged(self):
        storages["attachments"].delete(self.pdf.file.name)

        with self.assertLogs("notes.api.attachments", "ERROR"):
            response = self.client.get(f"{ATTACHMENTS}{self.pdf.pk}/file/")

        self.assertEqual(response.status_code, 404)

    def test_signed_out_is_401(self):
        self.assertEqual(APIClient().get(f"{ATTACHMENTS}{self.pdf.pk}/file/").status_code, 401)


class DeleteTests(AttachmentApiTestCase):
    def setUp(self):
        super().setUp()
        self.attachment = Attachment.objects.get(pk=self.post().json()["id"])
        self.name = self.attachment.file.name

    def test_delete_releases_storage_and_removes_the_file_on_commit(self):
        before = revision_of(self.alice)

        with self.captureOnCommitCallbacks() as callbacks:
            response = self.client.delete(f"{ATTACHMENTS}{self.attachment.pk}/")
            self.assertEqual(response.status_code, 204)
            # Not before the commit: a rolled-back delete must keep its file.
            self.assertIn(self.name, stored_files())
        for callback in callbacks:
            callback()

        self.assertNotIn(self.name, stored_files())
        self.attachment.refresh_from_db()
        self.assertIsNotNone(self.attachment.deleted_at)
        self.assertTrue(self.attachment.usage_event.refunded)
        self.assertEqual(revision_of(self.alice), before + 1)
        limits = self.client.get("/api/v1/me/").json()["limits"]
        self.assertEqual(limits["storage_bytes"]["used"], 0)

    def test_a_file_that_cannot_be_deleted_is_logged_not_raised(self):
        with mock.patch.object(
            type(storages["attachments"]), "delete", side_effect=OSError("disk gone")
        ):
            with self.assertLogs("notes.services", "ERROR"):
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.delete(f"{ATTACHMENTS}{self.attachment.pk}/")

        self.assertEqual(response.status_code, 204)

    def test_deleting_the_note_deletes_its_attachments(self):
        other_note = services.create_note(self.alice, title="Keep")
        kept = Attachment.objects.get(pk=self.post(upload(PNG), note=other_note).json()["id"])

        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.delete(f"{NOTES}{self.note.pk}/").status_code, 204)

        self.attachment.refresh_from_db()
        self.assertIsNotNone(self.attachment.deleted_at)
        self.assertTrue(self.attachment.usage_event.refunded)
        self.assertNotIn(self.name, stored_files())
        kept.refresh_from_db()
        self.assertIsNone(kept.deleted_at)
        self.assertIn(kept.file.name, stored_files())
        limits = self.client.get("/api/v1/me/").json()["limits"]
        self.assertEqual(limits["storage_bytes"]["used"], len(PNG))
        self.assertEqual(self.client.get(f"{ATTACHMENTS}{self.attachment.pk}/").status_code, 404)


class ChangesTests(AttachmentApiTestCase):
    def changed_note(self, after):
        results = self.client.get(CHANGES, {"after": after}).json()["results"]
        return next(n for n in results if n["id"] == self.note.pk)

    def test_changes_carries_a_notes_attachments_as_metadata(self):
        after = revision_of(self.alice)
        attachment_id = self.post().json()["id"]

        note = self.changed_note(after)

        self.assertEqual([a["id"] for a in note["attachments"]], [attachment_id])
        sent = note["attachments"][0]
        self.assertEqual(sent["original_name"], "receipt.pdf")
        self.assertNotIn("file", sent)
        self.assertNotIn("extracted_text", sent)

    def test_a_delete_sends_the_note_again_without_it(self):
        attachment_id = self.post().json()["id"]
        after = revision_of(self.alice)

        self.client.delete(f"{ATTACHMENTS}{attachment_id}/")

        self.assertEqual(self.changed_note(after)["attachments"], [])

    def test_a_tombstone_carries_none(self):
        self.post()
        after = revision_of(self.alice)
        self.client.delete(f"{NOTES}{self.note.pk}/")

        tombstone = self.changed_note(after)

        self.assertIsNotNone(tombstone["deleted_at"])
        self.assertNotIn("attachments", tombstone)


@override_settings(
    DATA_UPLOAD_MAX_MEMORY_SIZE=10,
    UPLOAD_SIZE_ALLOWANCES={"api:v1:note-attachments": 2000},
    CORS_ALLOWED_ORIGINS=["http://app.test"],
)
class UploadSizeAllowanceTests(AttachmentApiTestCase):
    """The attachment endpoint, and only it, may send more than the default body limit."""

    def test_the_upload_endpoint_gets_its_allowance(self):
        self.assertEqual(self.post().status_code, 201)

    def test_past_the_allowance_is_413_before_the_view_and_cors_readable(self):
        response = self.post(upload(PDF + b"x" * 2000), HTTP_ORIGIN="http://app.test")

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["code"], "too_large")
        self.assertEqual(response["Access-Control-Allow-Origin"], "http://app.test")
        self.assertEqual(self.new_files(), set())

    def test_other_endpoints_keep_the_default(self):
        response = self.client.post(
            NOTES, {"file": upload()}, format="multipart", HTTP_ORIGIN="http://app.test"
        )
        self.assertEqual(response.status_code, 413)

    def test_other_methods_on_the_same_path_keep_the_default(self):
        response = self.client.put(
            f"{NOTES}{self.note.pk}/attachments/", {"file": upload()}, format="multipart"
        )
        self.assertEqual(response.status_code, 413)


class AttachmentAdminTests(AttachmentApiTestCase):
    def setUp(self):
        super().setUp()
        self.attachment = Attachment.objects.get(pk=self.post().json()["id"])
        admin = get_user_model().objects.create_superuser("root@example.com", "pw-12345!")
        self.client.force_login(admin)
        self.base = f"/{settings.ADMIN_URL}notes/attachment/"

    def test_list_and_detail_are_readable_without_the_file(self):
        self.assertContains(self.client.get(self.base), "application/pdf")
        detail = self.client.get(f"{self.base}{self.attachment.pk}/change/")
        self.assertContains(detail, "receipt.pdf")
        self.assertNotContains(detail, self.attachment.file.name)

    def test_add_change_and_delete_are_refused(self):
        self.assertEqual(self.client.get(f"{self.base}add/").status_code, 403)
        self.assertEqual(
            self.client.get(f"{self.base}{self.attachment.pk}/delete/").status_code, 403
        )
        self.client.post(f"{self.base}{self.attachment.pk}/change/", {"original_name": "x"})

        self.assertEqual(Attachment.objects.get().original_name, "receipt.pdf")


@override_settings(ATTACHMENT_MAX_BYTES=2000, ATTACHMENT_UPLOAD_HEADROOM_BYTES=500)
class UploadCapHandlerTests(AttachmentApiTestCase):
    """The upload counts its bytes, whatever Content-Length says (D528).

    Built as uvicorn hands a request to Django (an ASGIRequest over the whole
    body), with a Content-Length that lies: the middleware lets it through,
    and without the handler the parser would read every byte.
    """

    def asgi_post(self, content, declared_length):
        body = encode_multipart(BOUNDARY, {"file": upload(content)})
        headers = [(b"content-type", MULTIPART_CONTENT.encode())]
        if declared_length is not None:
            headers.append((b"content-length", str(declared_length).encode()))
        scope = {
            "type": "http",
            "method": "POST",
            "path": f"{NOTES}{self.note.pk}/attachments/",
            "query_string": b"",
            "headers": headers,
        }
        request = ASGIRequest(scope, io.BytesIO(body))
        seen = []

        class Counting(FileUploadHandler):
            def receive_data_chunk(self, raw_data, start):
                seen.append(len(raw_data))
                return raw_data

            def file_complete(self, file_size):
                return None

        # Ahead of the defaults; the view then puts its cap ahead of this.
        request.upload_handlers.insert(0, Counting(request))
        force_authenticate(request, user=self.alice)
        response = NoteAttachmentsView.as_view()(request, pk=self.note.pk)
        return response, sum(seen)

    def test_a_body_past_the_cap_is_stopped_while_parsing(self):
        big = PDF + b"x" * 200_000
        response, parsed = self.asgi_post(big, declared_length=100)

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.data["code"], "too_large")
        # Far less than the 200 KB sent: the parse stopped at the cap.
        self.assertLessEqual(parsed, 2500)
        self.assertFalse(Attachment.objects.exists())

    def test_a_file_within_the_cap_still_uploads(self):
        response, _ = self.asgi_post(PDF, declared_length=None)
        # No Content-Length: Django reads no body at all, so there is no file.
        self.assertEqual(response.status_code, 400)

        body = encode_multipart(BOUNDARY, {"file": upload(PDF)})
        response, _ = self.asgi_post(PDF, declared_length=len(body))
        self.assertEqual(response.status_code, 201, response.data)
