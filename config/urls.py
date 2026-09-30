from django.conf import settings
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    # ADMIN_URL moves the admin off the address every scanner tries first.
    path(settings.ADMIN_URL, admin.site.urls),
    path("api/", include("config.api.urls")),
]
