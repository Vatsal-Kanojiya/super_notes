"""The notes API: CRUD with conflict detection, keyword search, and sync.

Ownership lives in ``get_queryset()``, as in the reference: every query is
filtered by ``owner=request.user`` in SQL, so another user's note is a 404
from every endpoint, list included, and its existence is never confirmed.

Writes go through notes/services.py, never ``serializer.save()`` on an
instance: the service is what takes the owner's lock and the next revision.
"""

from django.contrib.auth import get_user_model
from django.http import Http404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    PolymorphicProxySerializer,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from config.api.common import MessageSerializer
from notes import services
from notes.models import Note
from notes.search import keyword_search

from .serializers import (
    CHANGES_DEFAULT_LIMIT,
    CHANGES_MAX_LIMIT,
    ChangesQuerySerializer,
    NoteListQuerySerializer,
    NoteSerializer,
    NoteTombstoneSerializer,
    NoteUpdateSerializer,
)

User = get_user_model()

CONFLICT_DETAIL = "This note was changed elsewhere. 'current' is the server's copy."

ConflictSerializer = inline_serializer(
    "NoteConflict",
    {
        "detail": serializers.CharField(),
        "code": serializers.CharField(),
        "current": NoteSerializer(),
    },
)

NoteChangeSerializer = PolymorphicProxySerializer(
    component_name="NoteChange",
    serializers=[NoteSerializer, NoteTombstoneSerializer],
    resource_type_field_name=None,
    many=True,
)


@extend_schema_view(
    list=extend_schema(
        summary="List notes, newest first",
        description=(
            "Deleted notes are left out. `q` is a keyword search over the title and body "
            "(whole words and their stems; quotes, `or` and `-word` work). Results stay in "
            "newest-first order, not by relevance. Follow `next` for more."
        ),
        parameters=[
            OpenApiParameter("type", OpenApiTypes.STR, enum=Note.Type.values),
            OpenApiParameter("q", OpenApiTypes.STR, description="Keywords."),
            OpenApiParameter("page_size", OpenApiTypes.INT, description="1-100, default 25."),
        ],
    ),
    create=extend_schema(summary="Create a note"),
    retrieve=extend_schema(summary="Get a note"),
    partial_update=extend_schema(
        summary="Change a note",
        description=(
            "Send the `version` you loaded, plus the fields you changed. If the note has "
            "moved on since, nothing is written and you get `409` with the current copy: "
            "merge, or resend with its version."
        ),
        request=NoteUpdateSerializer,
        responses={
            200: NoteSerializer,
            409: OpenApiResponse(ConflictSerializer, "Someone else saved first."),
        },
    ),
    destroy=extend_schema(
        summary="Delete a note",
        description="A soft delete: other devices see it as a tombstone in `changes`.",
    ),
)
@extend_schema(tags=["Notes"])
class NoteViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = NoteSerializer
    # No PUT: a full replace would have to invent values for every field the
    # client left out. The router offers PATCH because partial_update exists
    # and update does not.
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    lookup_value_regex = r"\d+"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Note.objects.none()
        queryset = Note.objects.filter(owner=self.request.user, deleted_at__isnull=True)
        if self.action == "list":
            params = NoteListQuerySerializer(data=self.request.query_params)
            params.is_valid(raise_exception=True)
            if kind := params.validated_data.get("type"):
                queryset = queryset.filter(type=kind)
            if query := params.validated_data.get("q", "").strip():
                queryset = keyword_search(queryset, query)
        return queryset

    def partial_update(self, request, *args, **kwargs):
        note = self.get_object()
        payload = NoteUpdateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        changes = dict(payload.validated_data)
        expected = changes.pop("version")

        try:
            note = services.update_note(request.user, note.pk, expected_version=expected, **changes)
        except services.VersionConflict as conflict:
            return Response(
                {
                    "detail": CONFLICT_DETAIL,
                    "code": "version_conflict",
                    "current": NoteSerializer(conflict.current).data,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except Note.DoesNotExist as exc:
            # Deleted between get_object() and the lock.
            raise Http404 from exc
        return Response(NoteSerializer(note).data)

    def perform_destroy(self, instance):
        try:
            services.delete_note(self.request.user, instance.pk)
        except Note.DoesNotExist as exc:
            raise Http404 from exc

    @extend_schema(
        summary="What changed since a revision",
        description=(
            "Every note written after revision `after`, deleted ones included as tombstones "
            "(no title or content, `deleted_at` set), oldest write first. Store "
            "`latest_revision` and send it as `after` next time. If `has_more` is true, call "
            "again straight away with it: the batch was cut at `limit`. A first sync sends "
            "`after=0`."
        ),
        parameters=[
            OpenApiParameter("after", OpenApiTypes.INT, description="Default 0: everything."),
            OpenApiParameter(
                "limit",
                OpenApiTypes.INT,
                description=f"1-{CHANGES_MAX_LIMIT}, default {CHANGES_DEFAULT_LIMIT}.",
            ),
        ],
        responses={
            200: inline_serializer(
                "NoteChanges",
                {
                    "results": NoteChangeSerializer,
                    "latest_revision": serializers.IntegerField(),
                    "has_more": serializers.BooleanField(),
                },
            ),
            400: MessageSerializer,
        },
    )
    @action(detail=False, methods=["get"], pagination_class=None)
    def changes(self, request, *args, **kwargs):
        """Revision-based sync (DECISIONS D5, D25).

        The ceiling is read *first*. Every note with a revision at or below
        it is already committed -- the revision and the note were written
        in one transaction -- so the list below it is complete. Reading the
        notes first and the counter second would let a write commit in
        between: the client would store a latest_revision covering a change
        it was never sent, and skip it forever.

        A note written twice since ``after`` appears once, at its newest
        revision: the row holds only the latest.
        """
        params = ChangesQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        after = params.validated_data["after"]
        limit = params.validated_data["limit"]

        ceiling = User.objects.values_list("notes_revision", flat=True).get(pk=request.user.pk)
        batch = list(
            Note.objects.filter(
                owner=request.user, revision__gt=after, revision__lte=ceiling
            ).order_by("revision")[: limit + 1]
        )
        has_more = len(batch) > limit
        batch = batch[:limit]
        # Revisions are unique per user, so resuming after the last one sent
        # neither repeats nor skips a note.
        latest = batch[-1].revision if has_more else ceiling

        results = [
            NoteTombstoneSerializer(note).data if note.deleted_at else NoteSerializer(note).data
            for note in batch
        ]
        return Response({"results": results, "latest_revision": latest, "has_more": has_more})
