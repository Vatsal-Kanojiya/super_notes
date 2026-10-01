"""The attachments API: upload to a note, list, metadata, download, delete.

* ``POST notes/<id>/attachments/`` takes one file as multipart form data.
  The checks run cheapest first: the per-file size (413), the type sniffed
  from the bytes (415, D321), then -- in notes/services.py, under the
  owner's lock -- the ``storage_bytes`` limit (429 for the user, 503 for
  everyone, D84). The same bytes already on the note return that
  attachment with 200 and cost nothing.
* ``GET attachments/<id>/file/`` streams the bytes back as a download, with
  the sniffed type, ``nosniff`` and a sandboxing CSP, so a browser never
  renders or runs one in the app's origin (D329).

Ownership is in SQL, as everywhere: every query is filtered by
``owner=request.user``, so another user's note or attachment is a 404 from
every endpoint, and so is a deleted one or one whose note is deleted.
"""

import logging

from django.conf import settings
from django.http import FileResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import generics, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from config.api.common import SYSTEM_LIMIT_RESPONSE, MessageSerializer
from limits.serializers import LimitUsageSerializer
from limits.service import UserLimitExceeded
from notes import services
from notes.attachments import ALLOWED_TYPES, clean_name, sha256_of, sniff_upload
from notes.models import Attachment, Note

from .serializers import AttachmentSerializer, AttachmentUploadSerializer

logger = logging.getLogger(__name__)

TAG = ["Attachments"]

# Sent with every download: the bytes are the user's, not the app's, and must
# never be rendered or run in its origin, even if a browser is told to show
# one inline after all.
DOWNLOAD_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Cache-Control": "private, no-store",
}


class StorageQuotaExceededSerializer(MessageSerializer, LimitUsageSerializer):
    pass


def _problem(detail, code, http_status, **extra):
    return Response({"detail": detail, "code": code, **extra}, status=http_status)


def _megabytes(size):
    return f"{size / (1024 * 1024):g} MB"


@extend_schema(tags=TAG)
class NoteAttachmentsView(generics.GenericAPIView):
    """``notes/<id>/attachments/``: list a note's attachments, or add one."""

    serializer_class = AttachmentSerializer
    # A file comes as multipart; nothing else is accepted.
    parser_classes = [MultiPartParser]

    def get_throttles(self):
        # Uploads only: listing costs nothing.
        self.throttle_scope = "upload" if self.request.method == "POST" else None
        return super().get_throttles()

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Attachment.objects.none()
        return Attachment.objects.filter(
            owner=self.request.user,
            note_id=self.kwargs["pk"],
            deleted_at__isnull=True,
            note__deleted_at__isnull=True,
        )

    def _require_note(self, pk):
        notes = Note.objects.filter(pk=pk, owner=self.request.user, deleted_at__isnull=True)
        if not notes.exists():
            raise NotFound

    @extend_schema(
        summary="List a note's attachments",
        description="Newest first. Metadata only; download a file with "
        "`GET attachments/<id>/file/`.",
        responses={
            200: AttachmentSerializer(many=True),
            404: OpenApiResponse(MessageSerializer, description="No such note of yours."),
        },
    )
    def get(self, request, pk):
        self._require_note(pk)
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    @extend_schema(
        summary="Attach a file to a note",
        description=(
            "One file in the `file` field of a multipart form: a JPEG, PNG or WebP image, or a "
            "PDF, of at most "
            f"{_megabytes(settings.ATTACHMENT_MAX_BYTES)}. The type is read from the file's "
            "bytes; the name and Content-Type sent are not trusted, and a file of any other "
            "type is refused with 415 whatever it is called. The name is kept for display "
            "and downloads, cleaned, and always ends in the type's extension.\n\n"
            "The same file again on the same note is not stored twice: you get the existing "
            "attachment with 200. A new one is 201, `status: pending`. Its text is then "
            "read in the background (`extracting`), and once it is `ready` search and Ask "
            "find it, naming the file; a file whose text cannot be read is `failed`, with "
            "the reason in `error`, and stays attached. It counts against "
            "your `storage_bytes` (see `me/`) until it is deleted. The note is sent again "
            "by `notes/changes/` with its attachments, on every status change too."
        ),
        request={"multipart/form-data": AttachmentUploadSerializer},
        responses={
            201: OpenApiResponse(AttachmentSerializer, description="Stored."),
            200: OpenApiResponse(
                AttachmentSerializer, description="This file is already on the note."
            ),
            400: OpenApiResponse(MessageSerializer, description="No file, or an empty one."),
            404: OpenApiResponse(MessageSerializer, description="No such note of yours."),
            413: OpenApiResponse(MessageSerializer, description="`too_large`: over the cap."),
            415: OpenApiResponse(
                MessageSerializer,
                description="`unsupported_file_type`: not a JPEG, PNG, WebP or PDF; or "
                "`unsupported_media_type`: the request is not multipart.",
            ),
            429: OpenApiResponse(
                StorageQuotaExceededSerializer,
                description="`quota_exceeded`: your storage is full (with usage, in bytes), "
                "or `throttled`.",
            ),
            503: SYSTEM_LIMIT_RESPONSE,
        },
    )
    def post(self, request, pk):
        self._require_note(pk)
        payload = AttachmentUploadSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        upload = payload.validated_data["file"]

        limit = settings.ATTACHMENT_MAX_BYTES
        if upload.size > limit:
            return _problem(
                f"A file can be at most {_megabytes(limit)}.",
                "too_large",
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        mime_type = sniff_upload(upload)
        if mime_type is None:
            return _problem(
                "Only JPEG, PNG and WebP images and PDFs can be attached.",
                "unsupported_file_type",
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            )

        try:
            attachment, created = services.add_attachment(
                request.user,
                pk,
                upload,
                original_name=clean_name(upload.name, mime_type),
                mime_type=mime_type,
                sha256=sha256_of(upload),
            )
        except Note.DoesNotExist as exc:
            # Deleted between the check above and the lock.
            raise NotFound from exc
        except UserLimitExceeded as exceeded:
            return _problem(
                "Your storage is full. Delete some attachments to add more.",
                "quota_exceeded",
                status.HTTP_429_TOO_MANY_REQUESTS,
                **LimitUsageSerializer(
                    {
                        "used": exceeded.used,
                        "limit": exceeded.limit,
                        "resets_at": exceeded.resets_at,
                    }
                ).data,
            )

        return Response(
            AttachmentSerializer(attachment).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


@extend_schema_view(
    retrieve=extend_schema(
        summary="Get an attachment's metadata",
        responses={
            200: AttachmentSerializer,
            404: OpenApiResponse(MessageSerializer, description="No such attachment of yours."),
        },
    ),
    destroy=extend_schema(
        summary="Delete an attachment",
        description="Frees its storage and deletes the file. Its note is sent again by "
        "`notes/changes/` without it.",
        responses={
            204: None,
            404: OpenApiResponse(MessageSerializer, description="No such attachment of yours."),
        },
    ),
)
@extend_schema(tags=TAG)
class AttachmentViewSet(
    mixins.RetrieveModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet
):
    serializer_class = AttachmentSerializer
    lookup_value_regex = r"\d+"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Attachment.objects.none()
        return Attachment.objects.filter(
            owner=self.request.user, deleted_at__isnull=True, note__deleted_at__isnull=True
        )

    def perform_destroy(self, instance):
        try:
            services.delete_attachment(self.request.user, instance.pk)
        except Attachment.DoesNotExist as exc:
            # Deleted between get_object() and the lock.
            raise NotFound from exc

    def perform_content_negotiation(self, request, force=False):
        # A download answers with the file's own type, whatever the client
        # accepts; an error on the way still renders as JSON.
        if self.action == "file":
            force = True
        return super().perform_content_negotiation(request, force=force)

    @extend_schema(
        summary="Download an attachment",
        description="The file's bytes, as a download (`Content-Disposition: attachment`) with "
        "its sniffed type.",
        responses={
            **{
                (200, mime_type): OpenApiResponse(OpenApiTypes.BINARY, description="The file.")
                for mime_type in ALLOWED_TYPES
            },
            (404, "application/json"): OpenApiResponse(
                MessageSerializer, description="No such attachment of yours."
            ),
        },
    )
    @action(detail=True, methods=["get"])
    def file(self, request, *args, **kwargs):
        attachment = self.get_object()
        try:
            handle = attachment.file.open("rb")
        except FileNotFoundError as exc:
            # A live row without its file is a bug, not the client's doing.
            logger.error("Attachment %s has no file in storage.", attachment.pk)
            raise NotFound from exc
        response = FileResponse(
            handle,
            as_attachment=True,
            filename=attachment.original_name,
            content_type=attachment.mime_type,
        )
        for header, value in DOWNLOAD_HEADERS.items():
            response[header] = value
        return response
