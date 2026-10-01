"""The Ask API: asks, and conversations of them (plan §6.5, §7; V2 plan §5 phase 1).

``POST ask/``, ``GET ask/``, ``GET ask/<id>/`` and ``GET ask/<id>/stream/``;
``POST/GET conversations/``,
``GET/PATCH/DELETE conversations/<id>/`` and ``POST conversations/<id>/turns/``;
and the user's memory: ``GET/DELETE memory/facts/`` and
``DELETE memory/facts/<id>/`` (assistant/memory.py, DECISIONS D423).

Asynchronous, in the reference's job shape: POST creates the AskQuery and
returns it pending (202), the task answers it, and the client polls the
detail URL until the status is done or failed. A conversation turn is an
AskQuery too, polled at the same URL. All of the rules -- the quota,
idempotency, the lock, turns being sequential -- live in
assistant/services.py; this module is the HTTP around them.

Every query is filtered by ``user=request.user`` in SQL, so another user's
ask or conversation is a 404, never a 403 that says it exists. A deleted
conversation is a 404 too, and so are its turns (DECISIONS D145).
"""

import re

from django.core.handlers.asgi import ASGIRequest
from django.db.models import Prefetch
from django.http import StreamingHttpResponse
from django.urls import path
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import generics, serializers, status
from rest_framework.negotiation import BaseContentNegotiation
from rest_framework.pagination import CursorPagination
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from config.api.common import SYSTEM_LIMIT_RESPONSE, MessageSerializer
from config.middleware import get_request_id

from . import memory, stream
from .models import QUESTION_MAX_CHARS, TITLE_MAX_CHARS, AskQuery, Conversation, UserFact
from .services import (
    ConversationNotFound,
    IdempotencyKeyReused,
    QuotaExceeded,
    TurnInProgress,
    create_ask,
    create_conversation,
    create_turn,
    delete_conversation,
    rename_conversation,
)

ASK_TAG = ["Ask"]
CONVERSATION_TAG = ["Conversations"]
MEMORY_TAG = ["Memory"]

IDEMPOTENCY_HEADER = "Idempotency-Key"
# Letters, digits, "-" and "_": a UUID fits, and so does anything a client
# builds from one. Nothing that needs escaping in a log line (D75).
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_-]{1,100}")


# --- Shapes ---------------------------------------------------------------


class CitationSerializer(serializers.Serializer):
    n = serializers.IntegerField(help_text="The `[n]` marker in the answer this cites.")
    note_id = serializers.IntegerField()
    chunk_id = serializers.IntegerField()
    title = serializers.CharField(help_text="The note's title.")
    attachment_id = serializers.IntegerField(
        allow_null=True,
        help_text="The attachment whose text is cited; null when it is the note's own text.",
    )
    attachment_name = serializers.CharField(
        allow_null=True,
        help_text="That attachment's file name; null when it is the note's own text.",
    )
    snippet = serializers.CharField(help_text="The cited passage, on one line, cut at a word.")

    def to_representation(self, instance):
        # Answers stored before attachments were searchable have no such
        # keys: they cite the note's own text.
        return super().to_representation({**NO_ATTACHMENT, **instance})


NO_ATTACHMENT = {"attachment_id": None, "attachment_name": None}


class AskQuerySerializer(serializers.ModelSerializer):
    """An ask (or a conversation turn) as the client shows it.

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
            "conversation",
            "position",
            "question",
            "status",
            "answer",
            "citations",
            "error",
            "created_at",
            "completed_at",
        ]
        read_only_fields = fields
        extra_kwargs = {
            "conversation": {
                "help_text": "The conversation this is a turn of; null for a plain ask."
            },
            "position": {
                "help_text": "The turn's number in its conversation, from 1; null for a plain ask."
            },
        }


class AskCreateSerializer(serializers.Serializer):
    question = serializers.CharField(max_length=QUESTION_MAX_CHARS)


class AskUsageSerializer(serializers.Serializer):
    """This month's asks: the `chat_turns` limit. Failed asks are not counted."""

    used = serializers.IntegerField()
    limit = serializers.IntegerField(allow_null=True, help_text="Null: unlimited.")
    resets_at = serializers.DateTimeField(
        allow_null=True,
        help_text="When `used` goes back to 0: the start of next month, Asia/Kolkata. "
        "Null if it never resets.",
    )


class QuotaExceededSerializer(MessageSerializer, AskUsageSerializer):
    pass


class ConversationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Conversation
        fields = ["id", "title", "created_at", "updated_at"]
        read_only_fields = fields
        extra_kwargs = {
            "title": {
                "help_text": "The first question, shortened, until renamed. Blank until the "
                "first turn."
            },
            "updated_at": {"help_text": "When the last turn was asked. The list is ordered by it."},
        }


class ConversationDetailSerializer(ConversationSerializer):
    turns = AskQuerySerializer(many=True, read_only=True, help_text="Oldest first.")

    class Meta(ConversationSerializer.Meta):
        fields = [*ConversationSerializer.Meta.fields, "turns"]
        read_only_fields = fields


class ConversationCreateSerializer(serializers.Serializer):
    question = serializers.CharField(
        max_length=QUESTION_MAX_CHARS,
        required=False,
        help_text="Optional: asks it as turn 1 at once (then `Idempotency-Key` is required).",
    )


class ConversationRenameSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=TITLE_MAX_CHARS)


class UserFactSerializer(serializers.ModelSerializer):
    """A fact the assistant remembers about the user. Read-only: facts are only learned."""

    class Meta:
        model = UserFact
        fields = ["id", "text", "kind", "valid_until", "created_at"]
        read_only_fields = fields
        extra_kwargs = {
            "text": {"help_text": 'One short sentence, e.g. "User is vegetarian."'},
            "kind": {
                "help_text": "`static`: true until you say otherwise. `dynamic`: true for now; "
                "forgotten at `valid_until`."
            },
            "valid_until": {"help_text": "When a dynamic fact is forgotten; null for static."},
            "created_at": {"help_text": "When it was learned."},
        }


class TurnInProgressSerializer(MessageSerializer):
    turn = serializers.IntegerField(help_text="The unfinished turn: poll `GET ask/<turn>/`.")


# --- Helpers --------------------------------------------------------------


def _problem(detail, code, http_status, **extra):
    return Response({"detail": detail, "code": code, **extra}, status=http_status)


def _idempotency_key(request):
    """(key, None) from the header, or (None, the 400 to answer with)."""
    key = request.headers.get(IDEMPOTENCY_HEADER, "")
    if not key:
        return None, _problem(
            f"Send an {IDEMPOTENCY_HEADER} header: a fresh UUID per question.",
            "idempotency_key_required",
            status.HTTP_400_BAD_REQUEST,
        )
    if not IDEMPOTENCY_KEY.fullmatch(key):
        return None, _problem(
            f"{IDEMPOTENCY_HEADER} must be 1-100 letters, digits, '-' or '_'.",
            "idempotency_key_invalid",
            status.HTTP_400_BAD_REQUEST,
        )
    return key, None


def _quota_exceeded(exceeded):
    return _problem(
        "You've used this month's asks.",
        "quota_exceeded",
        status.HTTP_429_TOO_MANY_REQUESTS,
        **AskUsageSerializer(exceeded).data,
    )


def _key_reused():
    return _problem(
        f"This {IDEMPOTENCY_HEADER} was already used for a different question.",
        "idempotency_key_reused",
        status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


def _not_found():
    return _problem("Not found.", "not_found", status.HTTP_404_NOT_FOUND)


def _visible_asks(user):
    """The user's asks, less the turns of deleted conversations (DECISIONS D145).

    ``conversation__deleted_at__isnull`` is a LEFT JOIN, so a plain ask (no
    conversation) matches as well as a live conversation's turn.
    """
    return AskQuery.objects.filter(user=user, conversation__deleted_at__isnull=True)


def _live_conversations(user):
    return Conversation.objects.filter(user=user, deleted_at__isnull=True)


def _with_turns(conversations):
    return conversations.prefetch_related(
        Prefetch("turns", queryset=AskQuery.objects.order_by("position"))
    )


IDEMPOTENCY_PARAMETER = OpenApiParameter(
    IDEMPOTENCY_HEADER,
    OpenApiTypes.STR,
    OpenApiParameter.HEADER,
    required=True,
    description="1-100 of `A-Z a-z 0-9 - _`; a UUID is ideal.",
)
BAD_REQUEST = OpenApiResponse(
    MessageSerializer,
    description="Invalid question, or `idempotency_key_required` / `idempotency_key_invalid`.",
)
UNAUTHORIZED = OpenApiResponse(MessageSerializer, description="Not signed in.")
KEY_REUSED = OpenApiResponse(
    MessageSerializer,
    description="`idempotency_key_reused`: the key was used for another question, or for an "
    "ask somewhere else (a plain ask, another conversation).",
)
QUOTA = OpenApiResponse(
    QuotaExceededSerializer, description="`quota_exceeded` (with usage), or `throttled`."
)
NO_CONVERSATION = OpenApiResponse(
    MessageSerializer, description="No such conversation of yours (or it was deleted)."
)


# --- Asks -----------------------------------------------------------------


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
        return _visible_asks(self.request.user)

    @extend_schema(
        tags=ASK_TAG,
        summary="Your asks, newest first",
        description="Conversation turns included, except those of deleted conversations. "
        "Follow `next` for more.",
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
            "again. Reusing it for a different question, or one first sent as a conversation "
            "turn, is a 422.\n\n"
            "Each ask that does not fail counts against the month's quota (see `me/`). When "
            "the service-wide monthly budget is used up, asking pauses for everyone: 503 "
            "`system_limit_reached`."
        ),
        parameters=[IDEMPOTENCY_PARAMETER],
        request=AskCreateSerializer,
        responses={
            202: OpenApiResponse(AskQuerySerializer, description="Asked; poll for the answer."),
            200: OpenApiResponse(AskQuerySerializer, description="A replayed key: that ask."),
            400: BAD_REQUEST,
            401: UNAUTHORIZED,
            422: KEY_REUSED,
            429: QUOTA,
            503: SYSTEM_LIMIT_RESPONSE,
        },
    )
    def post(self, request, *args, **kwargs):
        key, problem = _idempotency_key(request)
        if problem:
            return problem

        body = AskCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        try:
            ask, created = create_ask(request.user, body.validated_data["question"], key)
        except QuotaExceeded as exceeded:
            return _quota_exceeded(exceeded)
        except IdempotencyKeyReused:
            return _key_reused()

        return Response(
            AskQuerySerializer(ask).data,
            status=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK,
        )


class AskDetailView(generics.RetrieveAPIView):
    serializer_class = AskQuerySerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return AskQuery.objects.none()
        return _visible_asks(self.request.user)

    @extend_schema(
        tags=ASK_TAG,
        summary="Get an ask",
        description="Poll this until `status` is `done` or `failed`; a failed ask has a "
        "user-facing `error` and does not count against the quota. Conversation turns are "
        "polled here too.",
        responses={
            200: AskQuerySerializer,
            401: UNAUTHORIZED,
            404: OpenApiResponse(MessageSerializer, description="No such ask of yours."),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class JSONErrorsOnly(BaseContentNegotiation):
    """Answer in the first renderer whatever the client accepts.

    The stream endpoint's client sends ``Accept: text/event-stream``; its
    errors (401, 404, 429) are still the API's JSON, not a 406.
    """

    def select_parser(self, request, parsers):
        return parsers[0]

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


STREAM_DESCRIPTION = """\
The answer as it is written, as server-sent events (`text/event-stream`). Read it with
`fetch` and a streaming body reader (an `EventSource` cannot send the `Authorization` header).
Polling `GET ask/<id>/` keeps working and stays the source of truth: on any error, or a stream
that ends without `done` or `failed`, poll (or open the stream again: it catches up).

Each event is `event: <type>` and one `data:` line of JSON that repeats `type`:

- `snapshot` `{text, offset}`: the answer so far; replaces what you have. `offset` is its
  length. First, for an unfinished ask.
- `delta` `{offset, text}`: append `text`. Deltas are contiguous: `offset` is always the length
  of the text so far (in code points).
- `reset`: the answer is starting over (a retry); clear the text.
- `done` / `failed` `{ask}`: the ask exactly as `GET ask/<id>/` returns it, citations
  included. Last.
- `timeout`: the stream reached its time limit (5 minutes by default). Last: poll.
- `unavailable`: live events are off or unreachable, or the server is not running under ASGI.
  Last: poll.

A finished ask gets its `done` or `failed` at once. A `: keep-alive` comment line comes after
15 seconds without an event. Opening a stream counts against the general request rate, not the
`ask` scope."""


class AskStreamView(APIView):
    """``GET ask/<id>/stream/``: who and whose here, the events in assistant/stream.py.

    A DRF view, so authentication, throttles, the error shape and the
    schema are the API's own (DECISIONS D370). It is synchronous: under
    ASGI, Django runs it in a worker thread, and it does no more than an
    ownership check. The body it returns is an async generator that the
    ASGI server drives on its event loop for as long as the stream is open.
    """

    renderer_classes = [JSONRenderer]
    content_negotiation_class = JSONErrorsOnly

    @extend_schema(
        tags=ASK_TAG,
        summary="Stream an ask's answer",
        description=STREAM_DESCRIPTION,
        responses={
            (200, "text/event-stream"): OpenApiResponse(
                OpenApiTypes.STR, description="Server-sent events until the ask ends."
            ),
            401: UNAUTHORIZED,
            404: OpenApiResponse(MessageSerializer, description="No such ask of yours."),
            429: OpenApiResponse(MessageSerializer, description="`throttled`."),
        },
    )
    def get(self, request, pk):
        if not _visible_asks(request.user).filter(pk=pk).exists():
            return _not_found()
        if isinstance(request._request, ASGIRequest):
            body = stream.events(pk, request.user.pk, request_id=get_request_id())
        else:
            body = stream.catch_up(pk, request.user.pk)
        response = StreamingHttpResponse(body, content_type="text/event-stream; charset=utf-8")
        response["Cache-Control"] = "no-cache"
        # nginx: pass each event on as it comes, not when its buffer fills.
        response["X-Accel-Buffering"] = "no"
        return response


# --- Conversations --------------------------------------------------------


class ConversationPagination(CursorPagination):
    """Most recently active first (DECISIONS D146).

    ``updated_at`` moves, which config/api/pagination.py warns against: a
    conversation that gets a new turn while a client pages jumps to the
    top, and a client already past page one does not see it again on this
    pass. It only ever moves up, so nothing is shown twice, and the next
    fetch of page one has it. ``-id`` breaks ties.
    """

    page_size = 25
    max_page_size = 100
    page_size_query_param = "page_size"
    ordering = ("-updated_at", "-id")


class ConversationListView(generics.ListAPIView):
    serializer_class = ConversationSerializer
    pagination_class = ConversationPagination

    @property
    def throttle_scope(self):
        # A first question costs a provider call, as an ask does.
        return "ask" if self.request.method == "POST" else None

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Conversation.objects.none()
        return _live_conversations(self.request.user)

    @extend_schema(
        tags=CONVERSATION_TAG,
        summary="Your conversations, most recently active first",
        description="Follow `next` for more.",
        parameters=[
            OpenApiParameter("page_size", OpenApiTypes.INT, description="1-100, default 25.")
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        tags=CONVERSATION_TAG,
        summary="Start a conversation",
        description=(
            "Without a `question`, makes an empty conversation (201). With one, also asks it "
            "as turn 1, exactly as `POST conversations/<id>/turns/` would, and then needs an "
            "`Idempotency-Key`: a retry with the same key returns the same conversation (200) "
            "and does not count again. Poll the turn with `GET ask/<id>/`."
        ),
        parameters=[
            OpenApiParameter(
                IDEMPOTENCY_HEADER,
                OpenApiTypes.STR,
                OpenApiParameter.HEADER,
                description="Required with a `question`. 1-100 of `A-Z a-z 0-9 - _`.",
            )
        ],
        request=ConversationCreateSerializer,
        responses={
            201: OpenApiResponse(ConversationDetailSerializer, description="Started."),
            200: OpenApiResponse(
                ConversationDetailSerializer, description="A replayed key: that conversation."
            ),
            400: BAD_REQUEST,
            401: UNAUTHORIZED,
            422: KEY_REUSED,
            429: QUOTA,
            503: SYSTEM_LIMIT_RESPONSE,
        },
    )
    def post(self, request, *args, **kwargs):
        body = ConversationCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        question = body.validated_data.get("question")

        key = None
        if question is not None:
            key, problem = _idempotency_key(request)
            if problem:
                return problem

        try:
            conversation, created = create_conversation(request.user, question, key)
        except QuotaExceeded as exceeded:
            return _quota_exceeded(exceeded)
        except IdempotencyKeyReused:
            return _key_reused()

        conversation = _with_turns(Conversation.objects).get(pk=conversation.pk)
        return Response(
            ConversationDetailSerializer(conversation).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class ConversationDetailView(generics.RetrieveAPIView):
    serializer_class = ConversationDetailSerializer
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Conversation.objects.none()
        return _with_turns(_live_conversations(self.request.user))

    @extend_schema(
        tags=CONVERSATION_TAG,
        summary="Get a conversation and its turns",
        responses={200: ConversationDetailSerializer, 401: UNAUTHORIZED, 404: NO_CONVERSATION},
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        tags=CONVERSATION_TAG,
        summary="Rename a conversation",
        description="Does not move it in the list, which follows new turns.",
        request=ConversationRenameSerializer,
        responses={
            200: ConversationSerializer,
            400: OpenApiResponse(MessageSerializer, description="Invalid title."),
            401: UNAUTHORIZED,
            404: NO_CONVERSATION,
        },
    )
    def patch(self, request, *args, **kwargs):
        body = ConversationRenameSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            conversation = rename_conversation(
                request.user, self.kwargs["pk"], body.validated_data["title"]
            )
        except ConversationNotFound:
            return _not_found()
        return Response(ConversationSerializer(conversation).data)

    @extend_schema(
        tags=CONVERSATION_TAG,
        summary="Delete a conversation",
        description="Its turns go from `ask/` too. The asks it used still count this month.",
        responses={204: None, 401: UNAUTHORIZED, 404: NO_CONVERSATION},
    )
    def delete(self, request, *args, **kwargs):
        try:
            delete_conversation(request.user, self.kwargs["pk"])
        except ConversationNotFound:
            return _not_found()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TurnCreateView(generics.GenericAPIView):
    serializer_class = AskCreateSerializer
    throttle_scope = "ask"

    @extend_schema(
        tags=CONVERSATION_TAG,
        summary="Ask the next question in a conversation",
        description=(
            "`POST ask/`, in a conversation: returns the turn `pending` (202); poll "
            "`GET ask/<id>/` until it is `done` or `failed`. Turns are sequential: while the "
            "previous turn is pending or running this is a 409 `turn_in_progress`, naming "
            "that turn. A replayed `Idempotency-Key` returns its turn (200), even then.\n\n"
            "Each turn counts against the month's asks, as an ask does."
        ),
        parameters=[IDEMPOTENCY_PARAMETER],
        request=AskCreateSerializer,
        responses={
            202: OpenApiResponse(AskQuerySerializer, description="Asked; poll for the answer."),
            200: OpenApiResponse(AskQuerySerializer, description="A replayed key: that turn."),
            400: BAD_REQUEST,
            401: UNAUTHORIZED,
            404: NO_CONVERSATION,
            409: OpenApiResponse(
                TurnInProgressSerializer,
                description="`turn_in_progress`: the previous turn has not finished.",
            ),
            422: KEY_REUSED,
            429: QUOTA,
            503: SYSTEM_LIMIT_RESPONSE,
        },
    )
    def post(self, request, *args, **kwargs):
        key, problem = _idempotency_key(request)
        if problem:
            return problem

        body = AskCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        try:
            turn, created = create_turn(
                request.user, self.kwargs["pk"], body.validated_data["question"], key
            )
        except ConversationNotFound:
            return _not_found()
        except TurnInProgress as busy:
            return _problem(
                "The previous question in this conversation is still being answered.",
                "turn_in_progress",
                status.HTTP_409_CONFLICT,
                turn=busy.turn.pk,
            )
        except QuotaExceeded as exceeded:
            return _quota_exceeded(exceeded)
        except IdempotencyKeyReused:
            return _key_reused()

        return Response(
            AskQuerySerializer(turn).data,
            status=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK,
        )


# --- Memory ---------------------------------------------------------------


class FactListView(generics.ListAPIView):
    serializer_class = UserFactSerializer
    http_method_names = ["get", "delete", "head", "options"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return UserFact.objects.none()
        # Live only: a superseded or expired fact is no longer used, and
        # is not shown either (owner-scoped in SQL by ``live``).
        return UserFact.objects.live(self.request.user)

    @extend_schema(
        tags=MEMORY_TAG,
        summary="What the assistant remembers about you, newest first",
        description="Facts learned from what you said about yourself in conversations, and "
        "used to shape later answers (never cited as a source). Only facts in use are "
        "listed. Follow `next` for more.",
        parameters=[
            OpenApiParameter("page_size", OpenApiTypes.INT, description="1-100, default 25.")
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        tags=MEMORY_TAG,
        summary="Forget everything",
        operation_id="memory_facts_forget_all",
        description="Deletes every fact the assistant remembers about you. A conversation "
        "turn asked before this teaches nothing afterwards, even if it is still being "
        "answered. Memory stays on (`PATCH me/` turns it off).",
        request=None,
        responses={204: None, 401: UNAUTHORIZED},
    )
    def delete(self, request, *args, **kwargs):
        memory.forget_all(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class FactDetailView(APIView):
    http_method_names = ["delete", "options"]

    @extend_schema(
        tags=MEMORY_TAG,
        summary="Forget one fact",
        operation_id="memory_facts_forget",
        description="Also forgets the older facts it replaced.",
        request=None,
        responses={
            204: None,
            401: UNAUTHORIZED,
            404: OpenApiResponse(MessageSerializer, description="No such fact of yours."),
        },
    )
    def delete(self, request, pk, *args, **kwargs):
        if not memory.delete_fact(request.user, pk):
            return _not_found()
        return Response(status=status.HTTP_204_NO_CONTENT)


urlpatterns = [
    path("ask/", AskListView.as_view(), name="ask-list"),
    path("ask/<int:pk>/", AskDetailView.as_view(), name="ask-detail"),
    path("ask/<int:pk>/stream/", AskStreamView.as_view(), name="ask-stream"),
    path("conversations/", ConversationListView.as_view(), name="conversation-list"),
    path("conversations/<int:pk>/", ConversationDetailView.as_view(), name="conversation-detail"),
    path("conversations/<int:pk>/turns/", TurnCreateView.as_view(), name="conversation-turns"),
    path("memory/facts/", FactListView.as_view(), name="memory-facts"),
    path("memory/facts/<int:pk>/", FactDetailView.as_view(), name="memory-fact-detail"),
]
