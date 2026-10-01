"""Attachment files: where they are stored, what type they are, what they are called.

Nothing here touches the database; notes/services.py does the writes.

* **Storage.** Files go to the ``attachments`` alias of settings.STORAGES:
  S3 in production, a private local directory otherwise (DECISIONS D85,
  D320). ``AttachmentStorage`` resolves that alias on every call rather than
  once at import, so the test runner (and any test) can point it at a temp
  dir and nothing ever writes to a developer's real bucket by accident.
* **Type.** Sniffed from the first bytes, never taken from the client's
  Content-Type or the file's extension (D321). Four types only.
* **Names.** The stored name is random (``attachments/<32 hex>.<ext>``); the
  client's name is kept only as ``original_name``, cleaned for display and
  for the download's Content-Disposition (D322).
"""

from __future__ import annotations

import hashlib
import secrets
import unicodedata

from django.core.files.storage import Storage, storages

STORAGE_ALIAS = "attachments"

# Sniffed type -> the extensions a download of it may carry, preferred first.
EXTENSIONS = {
    "image/jpeg": (".jpg", ".jpeg"),
    "image/png": (".png",),
    "image/webp": (".webp",),
    "application/pdf": (".pdf",),
}
ALLOWED_TYPES = tuple(EXTENSIONS)

# Enough bytes for every signature below.
SNIFF_BYTES = 16

ORIGINAL_NAME_MAX = 255
FALLBACK_NAME = "attachment"


def sniff_type(head: bytes) -> str | None:
    """The type of a file from its first bytes; None for anything not allowed.

    Signatures at offset 0 only. PDF readers tolerate ``%PDF-`` anywhere in
    the first kilobyte; this does not, so a file that is something else
    first and a PDF later (a polyglot) is refused.
    """
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def sniff_upload(upload) -> str | None:
    """sniff_type() on an uploaded file, leaving it at the start."""
    upload.seek(0)
    head = upload.read(SNIFF_BYTES)
    upload.seek(0)
    return sniff_type(head)


def sha256_of(upload) -> str:
    """Hex SHA-256 of an uploaded file, read in chunks (it may be on disk)."""
    digest = hashlib.sha256()
    for chunk in upload.chunks():
        digest.update(chunk)
    upload.seek(0)
    return digest.hexdigest()


def clean_name(raw: str | None, mime_type: str) -> str:
    """A client's file name made safe to show and to send back in a header.

    The last path component only (either slash), NFC, without control or
    format characters (a right-to-left override can make ``exe.pdf`` read
    as ``fdp.exe``), trimmed of spaces and leading dots, at most
    ORIGINAL_NAME_MAX characters. It always ends in an extension of the
    sniffed type: ``report`` becomes ``report.pdf`` and a PDF called
    ``setup.exe`` becomes ``setup.exe.pdf``, so a saved download never opens
    as something it is not. Nothing left of the name is ``attachment.<ext>``.
    """
    name = unicodedata.normalize("NFC", raw or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if not unicodedata.category(ch).startswith("C"))
    name = name.strip().lstrip(".").strip()

    extensions = EXTENSIONS[mime_type]
    if not name.lower().endswith(extensions):
        name = f"{name or FALLBACK_NAME}{extensions[0]}"
    if len(name) > ORIGINAL_NAME_MAX:
        # Cut the stem, keep the extension.
        stem, dot, ext = name.rpartition(".")
        name = f"{stem[: ORIGINAL_NAME_MAX - len(ext) - 1].rstrip()}{dot}{ext}"
    return name


def attachment_path(instance, filename) -> str:
    """``upload_to`` for Attachment.file: a random name, never the client's.

    ``filename`` is ignored. The extension comes from the sniffed type, so
    a file is never stored as, say, ``.html``.
    """
    extension = EXTENSIONS.get(instance.mime_type, ("",))[0]
    return f"attachments/{secrets.token_hex(16)}{extension}"


class AttachmentStorage(Storage):
    """Every call goes to ``storages["attachments"]`` as configured *now*.

    A FileField builds its storage once, when the model class is created;
    pointing it at this proxy instead keeps settings.STORAGES (and
    override_settings) in charge afterwards. Only the calls a FileField and
    this app make are forwarded.
    """

    @staticmethod
    def _target():
        return storages[STORAGE_ALIAS]

    def open(self, name, mode="rb"):
        return self._target().open(name, mode)

    def save(self, name, content, max_length=None):
        return self._target().save(name, content, max_length=max_length)

    def generate_filename(self, filename):
        return self._target().generate_filename(filename)

    def get_valid_name(self, name):
        return self._target().get_valid_name(name)

    def get_available_name(self, name, max_length=None):
        return self._target().get_available_name(name, max_length=max_length)

    def delete(self, name):
        return self._target().delete(name)

    def exists(self, name):
        return self._target().exists(name)

    def size(self, name):
        return self._target().size(name)

    def url(self, name):
        # Nothing in this project links to an attachment: downloads go
        # through the owner-scoped endpoint. Refusing here keeps it so.
        raise NotImplementedError("Attachments are never served by URL.")

    def path(self, name):
        return self._target().path(name)

    def listdir(self, path):
        return self._target().listdir(path)


attachment_storage_instance = AttachmentStorage()


def attachment_storage():
    """The FileField's ``storage`` callable (a stable path for migrations)."""
    return attachment_storage_instance
