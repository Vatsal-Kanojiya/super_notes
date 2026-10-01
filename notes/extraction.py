"""Reading an attachment's text and indexing it (DECISIONS D340-D349).

``run(attachment_id)`` is what the ``extract_attachment`` task does, after
an upload commits (notes/services.py, ``_after_attachment_added``):

1. **Claim** it: pending -> extracting, under the owner's lock, the note
   stamped with a revision so ``notes/changes/`` shows the new status. A
   finished, deleted or missing attachment ends the run here.
2. **Read** the text: a PDF's with pypdf, an image's with the chat
   provider's vision call (``chat.extract_image_text``). Kept on the row
   at once, so a retry further on never reads the file or pays again. The
   same bytes the owner uploaded before, already read, are not read again:
   that attachment's text is reused (D524).
3. **Embed** its chunks, with no lock held (the network call).
4. **Finish**: under the owner's lock, still live and still extracting,
   the chunks are written and the status set to ready, with a revision.

Every way out ends ``ready`` or ``failed``: a file that cannot be read is
failed here with a message a person can read (``ExtractionFailed``), and
the task fails the rest (retries used up, the soft time limit, a bug).
The stored ``error`` is always one of the fixed strings below, never a
library's or a vendor's message, which goes to the log.

Running twice is harmless: the claim and the finish are conditional on the
status, so a duplicate finds nothing to claim, or re-does the work and
finds nothing to finish.

**Hostile files.** A PDF is parsed here, in the worker, so it is bounded on
every axis it could blow up on: pages read (ATTACHMENT_PDF_MAX_PAGES),
text kept (ATTACHMENT_TEXT_MAX_CHARS), bytes any one compressed stream may
inflate to (ATTACHMENT_PDF_MAX_STREAM_BYTES, applied through pypdf's own
limits), and the task's time (ATTACHMENT_EXTRACT_SOFT_TIME_LIMIT). Any
error pypdf raises -- a damaged file, a limit reached, a recursion -- fails
the attachment, never the worker. Images are never decoded here at all:
their bytes go to the vendor as they are.
"""

from __future__ import annotations

import io
import logging
import re

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from pypdf import PdfReader, apply_configuration

from assistant import chat
from limits import service as limits
from retrieval.embeddings import EmbeddingError
from retrieval.indexing import embed_attachment, write_attachment_chunks

from . import services
from .models import Attachment

logger = logging.getLogger(__name__)

IMAGE_TEXT_KEY = "image_text"

# What the person reads on a failed attachment. Fixed strings only.
ENCRYPTED = "This PDF is password-protected, so its text can't be read."
UNREADABLE_PDF = "This PDF couldn't be read. The file may be damaged."
UNREADABLE_FILE = "This file couldn't be read."
IMAGE_TOO_LARGE = "This image is too large to read its text."
IMAGE_UNREADABLE = "The text in this image couldn't be read."
IMAGE_NOT_SUPPORTED = "Reading text from images isn't available right now."
IMAGE_PAUSED = "Reading text from images is paused for now. Please try again later."
IMAGE_LIMIT_REACHED = (
    "You've reached this month's limit for reading text from images. Please try again next month."
)
MISSING_FILE = "This file is missing, so its text can't be read."
NOT_INDEXED = "This file's text couldn't be made searchable."
BUSY = "Reading this file's text is taking longer than usual. Please try again later."
TOO_SLOW = "This file took too long to read."
UNEXPECTED = "Something went wrong reading this file."

# Control characters other than tab, newline and form feed: Postgres refuses
# NUL in text, and the rest are noise from a PDF's encoding.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0d-\x1f\x7f]")


class ExtractionFailed(Exception):
    """The file's text cannot be read, for a reason worth telling the person."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def run(attachment_id: int) -> None:
    """Extract, index and finish one attachment. See the module docstring."""
    attachment = services.start_extraction(attachment_id)
    if attachment is None:
        return

    text = attachment.extracted_text
    if not text:
        earlier = services.earlier_extracted_text(attachment)
        if earlier is not None:
            text = earlier
        else:
            try:
                text = read_text(attachment)
            except ExtractionFailed as failed:
                services.fail_extraction(attachment_id, failed.message)
                return
        if text and not services.save_extracted_text(attachment_id, text):
            return  # Deleted, or finished by another run, meanwhile.

    if not text:
        # Nothing to search: a scan without a text layer, a photo without
        # words. Ready all the same (D347).
        services.finish_extraction(attachment_id)
        return

    try:
        chunks, vectors, model_id = embed_attachment(attachment, text)
    except EmbeddingError as exc:
        logger.warning("Attachment %s: could not embed its text: %r", attachment_id, exc)
        services.fail_extraction(attachment_id, NOT_INDEXED)
        return

    def write(locked):
        write_attachment_chunks(locked, chunks, vectors, model_id, locked.note.version)

    services.finish_extraction(attachment_id, write_chunks=write)


def fail(attachment_id: int, message: str) -> bool:
    """Fail the attachment if it is not finished yet (the task's way out)."""
    return services.fail_extraction(attachment_id, message)


def read_text(attachment: Attachment) -> str:
    """The attachment's text, cleaned and capped. Raises ExtractionFailed."""
    data = _read_file(attachment)
    if attachment.mime_type == "application/pdf":
        text = pdf_text(data)
    else:
        text = image_text(attachment, data)
    return clean_text(text)[: settings.ATTACHMENT_TEXT_MAX_CHARS]


def _read_file(attachment: Attachment) -> bytes:
    limit = settings.ATTACHMENT_MAX_BYTES
    try:
        with attachment.file.open("rb") as handle:
            # One byte over the cap is enough to know it is over.
            data = handle.read(limit + 1)
    except FileNotFoundError as exc:
        logger.error("Attachment %s has no file in storage.", attachment.pk)
        raise ExtractionFailed(MISSING_FILE) from exc
    if len(data) > limit:
        # The upload refused this already; a file changed behind our back
        # is not read.
        logger.error("Attachment %s is over ATTACHMENT_MAX_BYTES in storage.", attachment.pk)
        raise ExtractionFailed(UNREADABLE_FILE)
    return data


def clean_text(text: str) -> str:
    """Text that Postgres stores and a person can read: no NUL or stray controls.

    Lone surrogates (a broken font map can produce them) become U+FFFD, and
    trailing spaces and runs of blank lines are trimmed.
    """
    text = text.encode("utf-8", "replace").decode("utf-8")
    text = _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [line.rstrip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# PDFs ---------------------------------------------------------------------


def pdf_text(data: bytes) -> str:
    """The text of a PDF's first pages, pages separated by form feeds.

    Stops after ATTACHMENT_PDF_MAX_PAGES pages, or once
    ATTACHMENT_TEXT_MAX_CHARS characters are read. A PDF that opens without
    a password (encrypted only to restrict printing or copying) is read;
    one that needs a password fails with ENCRYPTED. Anything pypdf raises
    fails with UNREADABLE_PDF -- except the soft time limit, which the task
    handles.
    """
    cap = settings.ATTACHMENT_PDF_MAX_STREAM_BYTES
    max_pages = settings.ATTACHMENT_PDF_MAX_PAGES
    max_chars = settings.ATTACHMENT_TEXT_MAX_CHARS
    limits_ = {
        "maximum_declared_stream_length": cap,
        "array_based_stream_maximum_output_length": cap,
        "jbig2_maximum_output_length": cap,
        "lzw_maximum_output_length": cap,
        "run_length_maximum_output_length": cap,
        "zlib_maximum_output_length": cap,
        "image_maximum_buffer_size": cap,
        # Never run an external decoder on a user's file.
        "jbig2dec_binary": None,
    }
    pages_text = []
    try:
        with apply_configuration(**limits_):
            reader = PdfReader(io.BytesIO(data), strict=False)
            if reader.is_encrypted and not _opens_without_password(reader):
                raise ExtractionFailed(ENCRYPTED)
            pages = reader.pages
            total = 0
            for index in range(min(len(pages), max_pages)):
                page = (pages[index].extract_text() or "").strip()
                if page:
                    pages_text.append(page)
                    total += len(page)
                if total >= max_chars:
                    break
    except (ExtractionFailed, SoftTimeLimitExceeded):
        raise
    except Exception as exc:
        # pypdf's errors, a limit reached, a recursion in a hostile file,
        # and whatever else a parser of untrusted bytes can raise.
        logger.warning("Could not read a PDF: %r", exc)
        raise ExtractionFailed(UNREADABLE_PDF) from exc
    return "\f".join(pages_text)


def _opens_without_password(reader: PdfReader) -> bool:
    try:
        return bool(reader.decrypt(""))
    except Exception as exc:
        # An encryption pypdf cannot handle is as unreadable as a password.
        logger.warning("Could not decrypt a PDF with no password: %r", exc)
        return False


# Images -------------------------------------------------------------------


def image_text(attachment: Attachment, data: bytes) -> str:
    """The text in an image, read by the chat provider (D343, D344, D520).

    One ``image_text`` use per call, the owner's (a per-user and a system
    limit), consumed under the owner's lock and refunded if the call fails
    for any reason, a transient one included (the retry consumes its own).
    A transient error is re-raised for the task to retry.
    """
    if len(data) > settings.ATTACHMENT_IMAGE_TEXT_MAX_BYTES:
        raise ExtractionFailed(IMAGE_TOO_LARGE)
    try:
        event = services.consume_for_owner(attachment.owner_id, IMAGE_TEXT_KEY)
    except limits.UserLimitExceeded as exc:
        raise ExtractionFailed(IMAGE_LIMIT_REACHED) from exc
    except limits.SystemLimitExceeded as exc:
        raise ExtractionFailed(IMAGE_PAUSED) from exc

    try:
        result = chat.extract_image_text(data, attachment.mime_type)
    except BaseException as exc:
        limits.refund(event)
        if isinstance(exc, chat.ImageTextNotSupported):
            logger.warning("Attachment %s: %s", attachment.pk, exc)
            raise ExtractionFailed(IMAGE_NOT_SUPPORTED) from exc
        if isinstance(exc, chat.ChatError):
            logger.warning("Attachment %s: the provider could not read it: %s", attachment.pk, exc)
            raise ExtractionFailed(IMAGE_UNREADABLE) from exc
        raise

    limits.describe_where(
        {"pk": event.pk},
        provider=result.provider,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )
    return result.text
