"""Routes for notes.

A SimpleRouter: the API root view a DefaultRouter adds would be one more
page listing endpoints, for no client that needs it.
"""

from rest_framework.routers import SimpleRouter

from .reminders import ReminderViewSet
from .views import NoteViewSet

router = SimpleRouter()
router.register("notes", NoteViewSet, basename="note")
router.register("reminders", ReminderViewSet, basename="reminder")

urlpatterns = router.urls
