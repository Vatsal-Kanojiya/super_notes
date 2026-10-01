"""Note serializers: what a client may send, and the one size limit.

Clients send ``type``, ``title`` and ``content``. Everything else -- the
owner, ``content_text``, ``version``, ``revision``, the dates -- is set by
the server in notes/services.py, and a payload carrying them is not an
error: DRF drops fields a serializer does not declare as writable. The
tests assert they have no effect.
"""

import json
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import serializers

from notes import services
from notes.content import InvalidContent, validate_doc
from notes.models import (
    REMINDER_LEAD_DAYS_DEFAULT,
    REMINDER_LEAD_DAYS_MAX,
    Attachment,
    Note,
    Reminder,
)


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
    summary_stale = serializers.SerializerMethodField(
        help_text="True when the note has been edited since its `summary` was made "
        "(`summary_version != version`). False with no summary."
    )

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
            "summary",
            "summary_version",
            "summary_stale",
            "created_at",
            "updated_at",
            "deleted_at",
        ]
        read_only_fields = [
            "content_text",
            "version",
            "revision",
            "summary",
            "summary_version",
            "summary_stale",
            "created_at",
            "updated_at",
            "deleted_at",
        ]

    def get_summary_stale(self, note) -> bool:
        return bool(note.summary) and note.summary_version != note.version

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


# Reminders ---------------------------------------------------------------


class AwareDateTimeField(serializers.DateTimeField):
    """An ISO 8601 date-time that must carry its offset (D126).

    DRF would read a bare ``2026-10-25T09:00`` in the server's timezone,
    which for a reminder is a guess at what the user meant. Refusing it
    makes the client say.
    """

    default_error_messages = {
        "naive": "Include a timezone offset, e.g. 2026-10-25T09:00:00+01:00 or ...Z.",
    }

    def to_internal_value(self, value):
        if isinstance(value, str):
            parsed = parse_datetime(value.strip())
            if parsed is not None and parsed.tzinfo is None:
                self.fail("naive")
        elif isinstance(value, datetime) and value.tzinfo is None:
            self.fail("naive")
        return super().to_internal_value(value)


class ReminderSerializer(serializers.ModelSerializer):
    """A reminder, as every endpoint returns it.

    Keep this the only schema component with a reminder ``status``: the
    schema's enums are named by field, and a second component carrying it
    would collide with the asks' ``status`` (see ReminderInRangeSerializer).
    """

    class Meta:
        model = Reminder
        fields = [
            "id",
            "note",
            "due_at",
            "lead_days",
            "channels",
            "status",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class ReminderWriteSerializer(serializers.Serializer):
    """POST notes/<id>/reminders/ and PATCH reminders/<id>/ (partial)."""

    due_at = AwareDateTimeField(help_text="When it is due, with an offset. Must be in the future.")
    lead_days = serializers.IntegerField(
        min_value=0,
        max_value=REMINDER_LEAD_DAYS_MAX,
        required=False,
        help_text=(
            "Daily heads-ups for this many days before, at the same local time. "
            f"0-{REMINDER_LEAD_DAYS_MAX}, default {REMINDER_LEAD_DAYS_DEFAULT}."
        ),
    )
    channels = serializers.ListField(
        child=serializers.ChoiceField(choices=Reminder.Channel.choices),
        min_length=1,
        required=False,
        help_text="Default both.",
    )

    def validate_due_at(self, value):
        if value <= timezone.now():
            raise serializers.ValidationError("Must be in the future.", code="in_past")
        return value

    def validate_channels(self, value):
        # Stored in one order, once each, whatever the client sent.
        return [c for c in Reminder.Channel.values if c in value]


REMINDER_RANGE_MAX_DAYS = 62


class ReminderRangeQuerySerializer(serializers.Serializer):
    """``GET reminders/?from=&to=``: a half-open range, at most 62 days."""

    # ``from`` is a keyword, so the fields cannot be class attributes.
    def get_fields(self):
        return {"from": AwareDateTimeField(), "to": AwareDateTimeField()}

    def validate(self, attrs):
        start, end = attrs["from"], attrs["to"]
        if end <= start:
            raise serializers.ValidationError({"to": "Must be after 'from'."})
        if end - start > timedelta(days=REMINDER_RANGE_MAX_DAYS):
            raise serializers.ValidationError(
                {"to": f"The range is at most {REMINDER_RANGE_MAX_DAYS} days."}
            )
        return attrs


class ReminderInRangeSerializer(serializers.Serializer):
    """A reminder in the calendar, with its notifications that fall in the range.

    The reminder is nested, not flattened, so its fields (``status`` among
    them) live in the one ``Reminder`` component (D129).
    """

    reminder = ReminderSerializer(source="*", read_only=True)
    note_title = serializers.CharField(source="note.title", read_only=True)
    occurrences = serializers.ListField(
        child=serializers.DateTimeField(),
        source="occurrences_in_range",
        read_only=True,
        help_text="Notification times inside the range, oldest first.",
    )


# Attachments --------------------------------------------------------------


class AttachmentSerializer(serializers.ModelSerializer):
    """An attachment's metadata, as every endpoint (and ``changes``) returns it.

    Never its bytes (``GET attachments/<id>/file/``) and never its extracted
    text: that is for search, and can be long.
    """

    original_name = serializers.CharField(
        read_only=True,
        help_text="The uploaded file's name, cleaned; always ends in the type's extension.",
    )
    mime_type = serializers.CharField(
        read_only=True, help_text="Sniffed from the file's bytes, not taken from the upload."
    )
    size = serializers.IntegerField(read_only=True, help_text="Bytes.")

    class Meta:
        model = Attachment
        fields = [
            "id",
            "note",
            "original_name",
            "mime_type",
            "size",
            "sha256",
            "status",
            "error",
            "summary",
            "created_at",
        ]
        read_only_fields = fields


class AttachmentUploadSerializer(serializers.Serializer):
    """``POST notes/<id>/attachments/``: one file, as multipart form data."""

    file = serializers.FileField(
        help_text="A JPEG, PNG, WebP image or a PDF. The type is read from the bytes; "
        "the name and Content-Type you send are not trusted."
    )


class NoteSyncSerializer(NoteSerializer):
    """A live note in ``changes``, with its reminders and attachments (deleted ones left out)."""

    reminders = ReminderSerializer(many=True, read_only=True, source="live_reminders")
    attachments = AttachmentSerializer(many=True, read_only=True, source="live_attachments")

    class Meta(NoteSerializer.Meta):
        fields = [*NoteSerializer.Meta.fields, "reminders", "attachments"]
