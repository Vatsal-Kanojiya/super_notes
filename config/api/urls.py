"""API routes, versioned in the path.

A path is the version a person can read in a log and paste into a browser.
The schema lives inside the version, so /api/v1/schema/ is exactly the v1
contract and a future v2 gets its own. Each app owns its own routes and
exposes them as ``urlpatterns``; this module only mounts them.
"""

from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from .views import HealthView

app_name = "api"

v1 = [
    path("health/", HealthView.as_view(), name="health"),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(url_name="api:v1:schema"), name="docs"),
    path("", include("accounts.api")),
    path("", include("notes.api.urls")),
    path("", include("retrieval.api")),
    path("", include("assistant.api")),
]

urlpatterns = [path("v1/", include((v1, "v1")))]
