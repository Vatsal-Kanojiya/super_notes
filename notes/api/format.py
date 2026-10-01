"""The format API: ``POST notes/<id>/format/`` and ``GET format-jobs/<id>/``.

Asynchronous, in the job shape of the Ask API (assistant/api.py): POST creates
the FormatJob and returns it pending (202), the task proposes a restructured
note, and the client polls the detail URL until the status is done or failed.
The rules -- the owner lock, the ``format`` limit, idempotency -- live in
notes/format_service.py; this module is the HTTP around them.

There is no "apply" endpoint on purpose: the server never writes the note
from a job. The client applies the proposal with ``PATCH notes/<id>/``
carrying ``version = base_version``, so a note edited since gets the usual 409.

Every query is filtered by ``owner=request.user`` in SQL, so another user's
note or job is a 404, never a 403 that says it exists.
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
from notes import format_service
from notes.models import FormatJob, Note

TAG = ["Format"]


class FormatJobSerializer(serializers.ModelSerializer):
    """A job as the client shows it. Not the provider, model, prompt or tokens."""

    note_id = serializers.IntegerField(read_only=True)
    proposed_content = serializers.JSONField(
        read_only=True,
        allow_null=True,
        help_text="The restructured document, when `status` is `done`; otherwise null. "
        "Show it beside the note and, on Apply, `PATCH` the note with it and "
        "`version = base_version`.",
    )

    class Meta:
        model = FormatJob
        fields = [
            "id",
            "note_id",
            "status",
            "base_version",
            "proposed_content",
            "error_code",
            "error",
            "created_at",
            "completed_at",
        ]
        read_only_fields = fields


class FormatUsageSerializer(serializers.Serializer):
    """This month's formats: the `format` limit. Failed jobs are not counted."""

    used = serializers.IntegerField()
    limit = serializers.IntegerField(allow_null=True, help_text="Null: unlimited.")
    resets_at = serializers.DateTimeField(allow_null=True)


class FormatQuotaExceededSerializer(MessageSerializer, FormatUsageSerializer):
    pass


def _problem(detail, code, http_status, **extra):
    return Response({"detail": detail, "code": code, **extra}, status=http_status)


class FormatCreateView(APIView):
    throttle_scope = "format"

    @extend_schema(
        tags=TAG,
        summary="Format a note",
        request=None,
        description=(
            "Starts restructuring the note (headings, lists, checklists, obvious typos) and "
            "returns at once with the job `pending` (202): poll `GET format-jobs/<id>/` until "
            "`status` is `done` or `failed`. The note is not changed. A `done` job carries "
            "`proposed_content` and the `base_version` it was made from: show it, and on Apply "
            "send `PATCH notes/<id>/` with that content and `version = base_version` (a note "
            "edited meanwhile is the usual 409).\n\n"
            "A proposal that adds or drops words, numbers or dates is refused: the job fails "
            "with `format_changed_content`. A failed job does not count against the month's "
            "`format` limit (see `me/`).\n\n"
            "Send a fresh `Idempotency-Key` per request. Resending one (a retry after a "
            "dropped connection) returns the job it already made, with 200, and does not "
            "count again. Reusing it for another note is a 422."
        ),
        parameters=[
            OpenApiParameter(
                IDEMPOTENCY_HEADER,
                OpenApiTypes.STR,
                OpenApiParameter.HEADER,
                required=True,
                description="1-100 of `A-Z a-z 0-9 - _`; a UUID is ideal.",
            )
        ],
        responses={
            202: OpenApiResponse(FormatJobSerializer, description="Started; poll for the result."),
            200: OpenApiResponse(FormatJobSerializer, description="A replayed key: that job."),
            400: OpenApiResponse(
                MessageSerializer,
                description="`note_empty`, `note_too_long`, `idempotency_key_required` or "
                "`idempotency_key_invalid`.",
            ),
            401: OpenApiResponse(MessageSerializer, description="Not signed in."),
            404: OpenApiResponse(MessageSerializer, description="No such note of yours."),
            422: OpenApiResponse(
                MessageSerializer,
                description="`idempotency_key_reused`: the key was used for another note.",
            ),
            429: OpenApiResponse(
                FormatQuotaExceededSerializer,
                description="`quota_exceeded` (with usage), or `throttled`.",
            ),
            503: SYSTEM_LIMIT_RESPONSE,
        },
    )
    def post(self, request, pk):
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
            job, created = format_service.create_format_job(request.user, pk, key)
        except Note.DoesNotExist as exc:
            raise NotFound from exc
        except format_service.NothingToFormat:
            return _problem(
                "This note is empty: there is nothing to format.",
                "note_empty",
                status.HTTP_400_BAD_REQUEST,
            )
        except format_service.NoteTooLong:
            return _problem(
                "This note is too long to format in one go.",
                "note_too_long",
                status.HTTP_400_BAD_REQUEST,
            )
        except format_service.IdempotencyKeyReused:
            return _problem(
                f"This {IDEMPOTENCY_HEADER} was already used for a different note.",
                "idempotency_key_reused",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except UserLimitExceeded as exceeded:
            return _problem(
                "You've used this month's formats.",
                "quota_exceeded",
                status.HTTP_429_TOO_MANY_REQUESTS,
                **FormatUsageSerializer(
                    {
                        "used": exceeded.used,
                        "limit": exceeded.limit,
                        "resets_at": exceeded.resets_at,
                    }
                ).data,
            )

        return Response(
            FormatJobSerializer(job).data,
            status=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK,
        )


class FormatJobDetailView(generics.RetrieveAPIView):
    serializer_class = FormatJobSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return FormatJob.objects.none()
        return FormatJob.objects.filter(owner=self.request.user)

    @extend_schema(
        tags=TAG,
        summary="Get a format job",
        description="Poll this until `status` is `done` or `failed`; a failed job has an "
        "`error_code` and a user-facing `error`, and does not count against the limit.",
        responses={
            200: FormatJobSerializer,
            401: OpenApiResponse(MessageSerializer, description="Not signed in."),
            404: OpenApiResponse(MessageSerializer, description="No such job of yours."),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)
