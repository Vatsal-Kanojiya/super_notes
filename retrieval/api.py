"""``GET search/``: hybrid retrieval over the caller's notes (plan §6.4, §7).

Synchronous: one query embedding plus three SQL queries, well inside a
request. It returns chunks, not notes -- the same list Ask will build its
prompt from -- so a client can show which part of a note matched. The
``search`` throttle scope applies because every call may cost an
embedding.
"""

from django.conf import settings
from django.urls import path
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from config.api.common import RATE_LIMIT_RESPONSE, MessageSerializer

from .search import search

SEARCH_TAG = ["Search"]
QUERY_MAX_LENGTH = 500  # a question, not a document: bounds the embedding cost
SNIPPET_CHARS = 200


class SearchQuerySerializer(serializers.Serializer):
    q = serializers.CharField(max_length=QUERY_MAX_LENGTH)
    k = serializers.IntegerField(min_value=1, max_value=settings.SEARCH_MAX_K, required=False)


class SearchHitSerializer(serializers.Serializer):
    """One matching chunk of a note (retrieval.search.SearchHit), plus a snippet."""

    chunk_id = serializers.IntegerField()
    note_id = serializers.IntegerField()
    title = serializers.CharField()
    heading_path = serializers.CharField(help_text='The headings above the chunk, "A > B".')
    text = serializers.CharField(help_text="The whole chunk.")
    snippet = serializers.SerializerMethodField(help_text="The chunk's start, cut at a word.")
    score = serializers.FloatField(
        help_text="Fused rank score: orders the hits, says nothing about relevance alone."
    )
    similarity = serializers.FloatField(
        allow_null=True, help_text="Cosine similarity to the query; null if found by keyword only."
    )
    keyword_rank = serializers.FloatField(
        allow_null=True, help_text="Full-text rank; null if found by meaning only."
    )

    def get_snippet(self, hit) -> str:
        return snippet(hit.text)


def snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    """``text`` up to ``limit`` characters, ending at a whole word."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return cut + "…"


class SearchView(APIView):
    throttle_scope = "search"

    @extend_schema(
        tags=SEARCH_TAG,
        summary="Search your notes",
        description=(
            "The best-matching chunks of your notes, best first: by meaning (embeddings) and by "
            "keyword, merged by reciprocal rank fusion, at most "
            f"{settings.SEARCH_MAX_CHUNKS_PER_NOTE} per note. Deleted notes never appear. A "
            "note edited in the last few seconds may still match its previous text until it is "
            "re-indexed."
        ),
        parameters=[
            OpenApiParameter("q", OpenApiTypes.STR, required=True, description="What to find."),
            OpenApiParameter(
                "k",
                OpenApiTypes.INT,
                description=f"1-{settings.SEARCH_MAX_K}, default {settings.SEARCH_DEFAULT_K}.",
            ),
        ],
        responses={
            200: SearchHitSerializer(many=True),
            400: MessageSerializer,
            401: OpenApiResponse(MessageSerializer, description="Not signed in."),
            429: RATE_LIMIT_RESPONSE,
        },
    )
    def get(self, request, *args, **kwargs):
        params = SearchQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        hits = search(request.user, params.validated_data["q"], params.validated_data.get("k"))
        return Response(SearchHitSerializer(hits, many=True).data)


urlpatterns = [path("search/", SearchView.as_view(), name="search")]
