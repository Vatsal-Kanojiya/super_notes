"""The summary API: ``POST notes/<id>/summarize/``, ``POST attachments/<id>/summarize/``
and ``GET summary-jobs/<id>/``.

In the job shape of the Format API (notes/api/format.py): POST creates the
SummaryJob and returns it pending (202), the task writes the summary onto the
note (or the attachment), and the client polls the detail URL until the status
is done or failed. The rules -- owner lock, the ``summary`` limit, idempotency
-- live in notes/summary_service.py.

Every query is filtered by ``owner=request.user`` in SQL, so another user's
note, file or job is a 404, never a 403 that says it exists.
"""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import generics, serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from assistant.api import IDEMPOTENCY_HEADER, IDEMPOTENCY_KEY
from config.api.common import SYSTEM_LIMIT_RESPONSE, MessageSerializer
from limits.service import UserLimitExceeded
from notes import summary_service
from notes.models import Attachment, Note, SummaryJob

TAG = ["Summaries"]


class SummaryJobSerializer(serializers.ModelSerializer):
    """A job as the client shows it. Not the provider, model, prompt or tokens."""

    note_id = serializers.IntegerField(read_only=True)
    attachment_id = serializers.IntegerField(
        read_only=True, allow_null=True, help_text="Set when the job summarises a file."
    )
    summary = serializers.CharField(
        read_only=True,
        help_text="The summary, when `status` is `done`; otherwise empty. It is already "
        "stored on the note (`Note.summary`) or the attachment.",
    )

    class Meta:
        model = SummaryJob
        fields = [
            "id",
            "note_id",
            "attachment_id",
            "status",
            "base_version",
            "summary",
            "error_code",
            "error",
            "created_at",
            "completed_at",
        ]
        read_only_fields = fields


class SummaryUsageSerializer(serializers.Serializer):
    """This month's summaries: the `summary` limit."""

    used = serializers.IntegerField()
    limit = serializers.IntegerField(allow_null=True, help_text="Null: unlimited.")
    resets_at = serializers.DateTimeField(allow_null=True)


class SummaryQuotaExceededSerializer(MessageSerializer, SummaryUsageSerializer):
    pass


def _problem(detail, code, http_status, **extra):
    return Response({"detail": detail, "code": code, **extra}, status=http_status)


IDEMPOTENCY_PARAMETER = OpenApiParameter(
    IDEMPOTENCY_HEADER,
    OpenApiTypes.STR,
    OpenApiParameter.HEADER,
    required=True,
    description="1-100 of `A-Z a-z 0-9 - _`; a UUID is ideal.",
)

_REPLAY = (
    "Send a fresh `Idempotency-Key` per request. Resending one (a retry after a dropped "
    "connection) returns the job it already made, with 200, and does not count again; so does "
    "asking again while a job for the same target is still running. Reusing a key for another "
    "target is a 422."
)


def _responses(extra_400, not_found):
    return {
        202: OpenApiResponse(SummaryJobSerializer, description="Started; poll for the result."),
        200: OpenApiResponse(
            SummaryJobSerializer, description="A replayed key, or a job still running: that job."
        ),
        400: OpenApiResponse(
            MessageSerializer,
            description=f"{extra_400}, `idempotency_key_required` or `idempotency_key_invalid`.",
        ),
        401: OpenApiResponse(MessageSerializer, description="Not signed in."),
        404: OpenApiResponse(MessageSerializer, description=not_found),
        422: OpenApiResponse(
            MessageSerializer,
            description="`idempotency_key_reused`: the key was used for another target.",
        ),
        429: OpenApiResponse(
            SummaryQuotaExceededSerializer,
            description="`quota_exceeded` (with usage), or `throttled`.",
        ),
        503: SYSTEM_LIMIT_RESPONSE,
    }


class _SummarizeView(APIView):
    """Both POSTs: the key check, the service, and the error mapping."""

    throttle_scope = "summary"

    def _create(self, request, note_id, attachment_id=None):
        key = request.headers.get(IDEMPOTENCY_HEADER, "")
        if not key:
            return _problem(
                f"Send an {IDEMPOTENCY_HEADER} header: a fresh UUID per request.",
                "idempotency_key_required",
                status.HTTP_400_BAD_REQUEST,
            )
        if not IDEMPOTENCY_KEY.fullmatch(key):
            return _problem(
                f"{IDEMPOTENCY_HEADER} must be 1-100 letters, digits, '-' or '_'.",
                "idempotency_key_invalid",
                status.HTTP_400_BAD_REQUEST,
            )

        try:
            job, created = summary_service.create_summary_job(
                request.user, note_id, key, attachment_id
            )
        except (Note.DoesNotExist, Attachment.DoesNotExist) as exc:
            raise NotFound from exc
        except summary_service.NothingToSummarize:
            return _problem(
                "There is no text to summarise.",
                "nothing_to_summarize",
                status.HTTP_400_BAD_REQUEST,
            )
        except summary_service.AttachmentNotReady:
            return _problem(
                "This file's text has not been read yet, or could not be.",
                "attachment_not_ready",
                status.HTTP_400_BAD_REQUEST,
            )
        except summary_service.IdempotencyKeyReused:
            return _problem(
                f"This {IDEMPOTENCY_HEADER} was already used for something else.",
                "idempotency_key_reused",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except UserLimitExceeded as exceeded:
            return _problem(
                "You've used this month's summaries.",
                "quota_exceeded",
                status.HTTP_429_TOO_MANY_REQUESTS,
                **SummaryUsageSerializer(
                    {
                        "used": exceeded.used,
                        "limit": exceeded.limit,
                        "resets_at": exceeded.resets_at,
                    }
                ).data,
            )
        return Response(
            SummaryJobSerializer(job).data,
            status=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK,
        )


class NoteSummarizeView(_SummarizeView):
    @extend_schema(
        tags=TAG,
        summary="Summarize a note",
        request=None,
        description=(
            "Starts summarising the note and returns at once with the job `pending` (202): "
            "poll `GET summary-jobs/<id>/` until `status` is `done` or `failed`. A finished "
            "summary is stored on the note as `summary`, with `summary_version`, the note "
            "`version` it was made from; the note's own `version` does not change, so "
            "nothing conflicts. `summary_stale` is true once the note has been edited since. "
            "The summary is also searchable, so broad questions find the note. A failed job "
            "does not count against the month's `summary` limit (see `me/`), except one whose "
            "answer the assistant generated but could not be used.\n\n" + _REPLAY
        ),
        parameters=[IDEMPOTENCY_PARAMETER],
        responses=_responses("`nothing_to_summarize`", "No such note of yours."),
    )
    def post(self, request, pk):
        return self._create(request, pk)


class AttachmentSummarizeView(_SummarizeView):
    @extend_schema(
        tags=TAG,
        summary="Summarize an attachment",
        request=None,
        description=(
            "As summarising a note, for a file whose text has been read (`status` `ready`) "
            "and is not empty. The summary is stored on the attachment (`summary`), which "
            "its note sends again in `notes/changes/`.\n\n" + _REPLAY
        ),
        parameters=[IDEMPOTENCY_PARAMETER],
        responses=_responses(
            "`nothing_to_summarize` or `attachment_not_ready`", "No such attachment of yours."
        ),
    )
    def post(self, request, pk):
        row = (
            Attachment.objects.filter(
                pk=pk, owner=request.user, deleted_at__isnull=True, note__deleted_at__isnull=True
            )
            .values_list("note_id", flat=True)
            .first()
        )
        if row is None:
            raise NotFound
        return self._create(request, row, pk)


class SummaryJobDetailView(generics.RetrieveAPIView):
    serializer_class = SummaryJobSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return SummaryJob.objects.none()
        return SummaryJob.objects.filter(owner=self.request.user)

    @extend_schema(
        tags=TAG,
        summary="Get a summary job",
        description="Poll this until `status` is `done` or `failed`; a failed job has an "
        "`error_code` and a user-facing `error`.",
        responses={
            200: SummaryJobSerializer,
            401: OpenApiResponse(MessageSerializer, description="Not signed in."),
            404: OpenApiResponse(MessageSerializer, description="No such job of yours."),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)
