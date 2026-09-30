"""The Ask API: ``POST ask/``, ``GET ask/`` and ``GET ask/<id>/`` (plan §6.5, §7).

Asynchronous, in the reference's job shape: POST creates the AskQuery and
returns it pending (202), the task answers it, and the client polls the
detail URL until the status is done or failed. All of the rules -- the
quota, idempotency, the lock -- live in assistant/services.py; this module
is the HTTP around them.

Every query is filtered by ``user=request.user`` in SQL, so another user's
ask is a 404, never a 403 that says it exists.
"""

import re

from django.urls import path
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import generics, serializers, status
from rest_framework.response import Response

from config.api.common import MessageSerializer

from .models import QUESTION_MAX_CHARS, AskQuery
from .services import IdempotencyKeyReused, QuotaExceeded, create_ask

ASK_TAG = ["Ask"]

IDEMPOTENCY_HEADER = "Idempotency-Key"
# Letters, digits, "-" and "_": a UUID fits, and so does anything a client
# builds from one. Nothing that needs escaping in a log line (D75).
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_-]{1,100}")


# --- Shapes ---------------------------------------------------------------


class CitationSerializer(serializers.Serializer):
    n = serializers.IntegerField(help_text="The `[n]` marker in the answer this cites.")
    note_id = serializers.IntegerField()
    chunk_id = serializers.IntegerField()
    title = serializers.CharField()
    snippet = serializers.CharField(help_text="The cited passage, on one line, cut at a word.")


class AskQuerySerializer(serializers.ModelSerializer):
    """An ask as the client shows it.

    Not ``retrieved`` or the token counts: they are for debugging and cost,
    and the scores in ``retrieved`` would invite a client to rank with them.
    """

    citations = CitationSerializer(
        many=True,
        read_only=True,
        help_text="The notes the answer cites, in first-mention order. Link only these "
        "numbers: an `[n]` in the answer with no citation points at nothing.",
    )

    class Meta:
        model = AskQuery
        fields = [
            "id",
            "question",
            "status",
            "answer",
            "citations",
            "error",
            "created_at",
            "completed_at",
        ]
        read_only_fields = fields


class AskCreateSerializer(serializers.Serializer):
    question = serializers.CharField(max_length=QUESTION_MAX_CHARS)


class AskUsageSerializer(serializers.Serializer):
    """This month's asks. Failed asks are not counted."""

    used = serializers.IntegerField()
    limit = serializers.IntegerField()
    resets_at = serializers.DateTimeField(
        help_text="When `used` goes back to 0: the start of next month, Asia/Kolkata."
    )


class QuotaExceededSerializer(MessageSerializer, AskUsageSerializer):
    pass


# --- Views ----------------------------------------------------------------


def _problem(detail, code, http_status, **extra):
    return Response({"detail": detail, "code": code, **extra}, status=http_status)


class AskListView(generics.ListAPIView):
    serializer_class = AskQuerySerializer

    @property
    def throttle_scope(self):
        # Only asking costs a provider call. Listing and polling are left to
        # the general user rate: a client polling an answer must not use up
        # the scope that guards the expensive part.
        return "ask" if self.request.method == "POST" else None

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return AskQuery.objects.none()
        return AskQuery.objects.filter(user=self.request.user)

    @extend_schema(
        tags=ASK_TAG,
        summary="Your asks, newest first",
        description="Follow `next` for more.",
        parameters=[
            OpenApiParameter("page_size", OpenApiTypes.INT, description="1-100, default 25.")
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        tags=ASK_TAG,
        summary="Ask your notes a question",
        description=(
            "Starts answering and returns at once with the ask `pending` (202): poll "
            "`GET ask/<id>/` until `status` is `done` or `failed`. The answer comes from your "
            "notes only, with `[n]` markers matching `citations`.\n\n"
            "Send a fresh `Idempotency-Key` per question. Resending one (a retry after a "
            "dropped connection) returns the ask it already made, with 200, and does not count "
            "again. Reusing it for a different question is a 422.\n\n"
            "Each ask that does not fail counts against the month's quota (see `me/`)."
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
        request=AskCreateSerializer,
        responses={
            202: OpenApiResponse(AskQuerySerializer, description="Asked; poll for the answer."),
            200: OpenApiResponse(AskQuerySerializer, description="A replayed key: that ask."),
            400: OpenApiResponse(
                MessageSerializer,
                description="Invalid question, or `idempotency_key_required` / "
                "`idempotency_key_invalid`.",
            ),
            401: OpenApiResponse(MessageSerializer, description="Not signed in."),
            422: OpenApiResponse(
                MessageSerializer,
                description="`idempotency_key_reused`: the key was used for another question.",
            ),
            429: OpenApiResponse(
                QuotaExceededSerializer,
                description="`quota_exceeded` (with usage), or `throttled`.",
            ),
        },
    )
    def post(self, request, *args, **kwargs):
        key = request.headers.get(IDEMPOTENCY_HEADER, "")
        if not key:
            return _problem(
                f"Send an {IDEMPOTENCY_HEADER} header: a fresh UUID per question.",
                "idempotency_key_required",
                status.HTTP_400_BAD_REQUEST,
            )
        if not IDEMPOTENCY_KEY.fullmatch(key):
            return _problem(
                f"{IDEMPOTENCY_HEADER} must be 1-100 letters, digits, '-' or '_'.",
                "idempotency_key_invalid",
                status.HTTP_400_BAD_REQUEST,
            )

        body = AskCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        try:
            ask, created = create_ask(request.user, body.validated_data["question"], key)
        except QuotaExceeded as exceeded:
            usage = AskUsageSerializer(exceeded).data
            return _problem(
                "You've used this month's asks.",
                "quota_exceeded",
                status.HTTP_429_TOO_MANY_REQUESTS,
                **usage,
            )
        except IdempotencyKeyReused:
            return _problem(
                f"This {IDEMPOTENCY_HEADER} was already used for a different question.",
                "idempotency_key_reused",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        return Response(
            AskQuerySerializer(ask).data,
            status=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK,
        )


class AskDetailView(generics.RetrieveAPIView):
    serializer_class = AskQuerySerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return AskQuery.objects.none()
        return AskQuery.objects.filter(user=self.request.user)

    @extend_schema(
        tags=ASK_TAG,
        summary="Get an ask",
        description="Poll this until `status` is `done` or `failed`; a failed ask has a "
        "user-facing `error` and does not count against the quota.",
        responses={
            200: AskQuerySerializer,
            401: OpenApiResponse(MessageSerializer, description="Not signed in."),
            404: OpenApiResponse(MessageSerializer, description="No such ask of yours."),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


urlpatterns = [
    path("ask/", AskListView.as_view(), name="ask-list"),
    path("ask/<int:pk>/", AskDetailView.as_view(), name="ask-detail"),
]
