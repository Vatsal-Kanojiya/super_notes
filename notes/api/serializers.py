"""Note serializers: what a client may send, and the one size limit.

Clients send ``type``, ``title`` and ``content``. Everything else -- the
owner, ``content_text``, ``version``, ``revision``, the dates -- is set by
the server in notes/services.py, and a payload carrying them is not an
error: DRF drops fields a serializer does not declare as writable. The
tests assert they have no effect.
"""

import json

from django.conf import settings
from rest_framework import serializers

from notes import services
from notes.content import InvalidContent, validate_doc
from notes.models import Note


def validate_content(value):
    """A TipTap document, no larger than NOTE_CONTENT_MAX_BYTES serialised.

    Shape first: validate_doc checks depth without recursing, so the
    json.dumps that measures the size can never be handed something deep
    enough to blow the stack. Size is measured as compact UTF-8 JSON --
    what Postgres stores near enough -- not as the request body, which may
    be pretty-printed or escaped.
    """
    try:
        validate_doc(value)
    except InvalidContent as exc:
        raise serializers.ValidationError(str(exc)) from exc

    size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
    limit = settings.NOTE_CONTENT_MAX_BYTES
    if size > limit:
        raise serializers.ValidationError(
            f"Content is {size} bytes; the limit is {limit} bytes.", code="too_large"
        )
    return value


class NoteSerializer(serializers.ModelSerializer):
    """A live note, as every endpoint returns it."""

    content = serializers.JSONField(required=False, validators=[validate_content])

    class Meta:
        model = Note
        fields = [
            "id",
            "type",
            "title",
            "content",
            "content_text",
            "version",
            "revision",
            "created_at",
            "updated_at",
            "deleted_at",
        ]
        read_only_fields = [
            "content_text",
            "version",
            "revision",
            "created_at",
            "updated_at",
            "deleted_at",
        ]

    def create(self, validated_data):
        return services.create_note(self.context["request"].user, **validated_data)


class NoteUpdateSerializer(serializers.Serializer):
    """PATCH: the version the client edited, plus whatever it changed."""

    version = serializers.IntegerField(
        min_value=1, help_text="The version you loaded. A newer one on the server is a 409."
    )
    type = serializers.ChoiceField(choices=Note.Type.choices, required=False)
    title = serializers.CharField(max_length=500, allow_blank=True, required=False)
    content = serializers.JSONField(required=False, validators=[validate_content])


class NoteTombstoneSerializer(serializers.ModelSerializer):
    """A deleted note in ``changes``: enough to drop it, none of its content."""

    # Never null here, unlike on a live note. That, and the missing
    # content_text, keep the two apart in the schema's oneOf.
    deleted_at = serializers.DateTimeField(read_only=True)

    class Meta:
        model = Note
        fields = ["id", "type", "version", "revision", "updated_at", "deleted_at"]
        read_only_fields = fields


class NoteListQuerySerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=Note.Type.choices, required=False)
    q = serializers.CharField(max_length=200, required=False, allow_blank=True)


CHANGES_DEFAULT_LIMIT = 500
CHANGES_MAX_LIMIT = 1000


class ChangesQuerySerializer(serializers.Serializer):
    after = serializers.IntegerField(min_value=0, default=0)
    limit = serializers.IntegerField(
        min_value=1, max_value=CHANGES_MAX_LIMIT, default=CHANGES_DEFAULT_LIMIT
    )
