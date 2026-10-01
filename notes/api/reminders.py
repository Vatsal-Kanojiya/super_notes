"""The reminders API: change, finish and delete a reminder; the calendar range.

Reminders are created on their note (``POST notes/<id>/reminders/``, in
views.py). Ownership is in ``get_queryset()`` as for notes: another user's
reminder, a deleted one, and one whose note is deleted are all 404.
Writes go through notes/services.py, which takes the owner's lock and
stamps the note's revision so sync carries the change.
"""

from datetime import timedelta

from django.http import Http404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from config.api.common import MessageSerializer
from notes import services
from notes.models import REMINDER_LEAD_DAYS_MAX, Reminder
from notes.schedule import occurrences, user_timezone

from .serializers import (
    REMINDER_RANGE_MAX_DAYS,
    ReminderInRangeSerializer,
    ReminderRangeQuerySerializer,
    ReminderSerializer,
    ReminderWriteSerializer,
)

# How far after a range's end a reminder can be due and still notify inside
# it: its longest lead, plus a day for DST shifts and skipped-time rounding.
_LOOKAHEAD = timedelta(days=REMINDER_LEAD_DAYS_MAX + 1)


@extend_schema_view(
    partial_update=extend_schema(
        summary="Change a reminder",
        description=(
            "Any of `due_at`, `lead_days`, `channels`. Its status stays as it is: a done "
            "reminder stays done."
        ),
        request=ReminderWriteSerializer,
        responses={200: ReminderSerializer, 400: MessageSerializer},
    ),
    destroy=extend_schema(summary="Delete a reminder"),
)
@extend_schema(tags=["Reminders"])
class ReminderViewSet(mixins.DestroyModelMixin, viewsets.GenericViewSet):
    serializer_class = ReminderSerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    lookup_value_regex = r"\d+"
    pagination_class = None

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Reminder.objects.none()
        return Reminder.objects.filter(
            owner=self.request.user, deleted_at__isnull=True, note__deleted_at__isnull=True
        )

    @extend_schema(
        summary="Reminders in a date range (the calendar)",
        description=(
            "Every reminder with at least one notification in `[from, to)`, with those "
            "notifications in `occurrences` and its note's title, earliest due first. Done "
            "reminders are included (`reminder.status` says so). Both bounds need an offset "
            "(`Z` works; a `+` must be URL-encoded as `%2B`); "
            f"the range is at most {REMINDER_RANGE_MAX_DAYS} days."
        ),
        parameters=[
            OpenApiParameter("from", OpenApiTypes.DATETIME, required=True),
            OpenApiParameter("to", OpenApiTypes.DATETIME, required=True),
        ],
        responses={
            200: inline_serializer(
                "ReminderRange", {"results": ReminderInRangeSerializer(many=True)}
            ),
            400: MessageSerializer,
        },
    )
    def list(self, request, *args, **kwargs):
        params = ReminderRangeQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        start, end = params.validated_data["from"], params.validated_data["to"]

        # Every occurrence is at or before due_at and at most lead_days
        # before it, so this bounds the candidates in SQL; the exact cut is
        # made below on the computed schedule.
        candidates = (
            self.get_queryset()
            .filter(due_at__gte=start, due_at__lt=end + _LOOKAHEAD)
            .select_related("note")
            .order_by("due_at", "id")
        )
        tz = user_timezone(request.user)
        results = []
        for reminder in candidates:
            in_range = [
                at
                for at in occurrences(reminder.due_at, reminder.lead_days, tz)
                if start <= at < end
            ]
            if in_range:
                reminder.occurrences_in_range = in_range
                results.append(reminder)
        return Response({"results": ReminderInRangeSerializer(results, many=True).data})

    def partial_update(self, request, *args, **kwargs):
        reminder = self.get_object()
        payload = ReminderWriteSerializer(data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        try:
            reminder = services.update_reminder(request.user, reminder.pk, **payload.validated_data)
        except Reminder.DoesNotExist as exc:
            raise Http404 from exc
        return Response(ReminderSerializer(reminder).data)

    def perform_destroy(self, instance):
        try:
            services.delete_reminder(self.request.user, instance.pk)
        except Reminder.DoesNotExist as exc:
            raise Http404 from exc

    @extend_schema(
        summary="Mark a reminder done",
        description="Stops the rest of its notifications. Doing it twice is harmless.",
        request=None,
        responses={200: ReminderSerializer},
    )
    @action(detail=True, methods=["post"])
    def done(self, request, *args, **kwargs):
        reminder = self.get_object()
        try:
            reminder = services.mark_reminder_done(request.user, reminder.pk)
        except Reminder.DoesNotExist as exc:
            raise Http404 from exc
        return Response(ReminderSerializer(reminder).data, status=status.HTTP_200_OK)
