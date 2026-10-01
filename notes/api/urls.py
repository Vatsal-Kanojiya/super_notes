"""Routes for notes.

A SimpleRouter: the API root view a DefaultRouter adds would be one more
page listing endpoints, for no client that needs it.
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from .attachments import AttachmentViewSet, NoteAttachmentsView
from .format import FormatCreateView, FormatJobDetailView
from .reminders import ReminderViewSet
from .views import NoteViewSet

router = SimpleRouter()
router.register("notes", NoteViewSet, basename="note")
router.register("reminders", ReminderViewSet, basename="reminder")
router.register("attachments", AttachmentViewSet, basename="attachment")

urlpatterns = [
    path("notes/<int:pk>/format/", FormatCreateView.as_view(), name="note-format"),
    # Named in settings.UPLOAD_SIZE_ALLOWANCES: renaming it drops the allowance.
    path("notes/<int:pk>/attachments/", NoteAttachmentsView.as_view(), name="note-attachments"),
    path("format-jobs/<int:pk>/", FormatJobDetailView.as_view(), name="format-job-detail"),
    *router.urls,
]
