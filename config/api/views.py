from django.conf import settings
from django.db import DatabaseError, connection
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    """Whether the app is up and can reach its database.

    For an uptime monitor, or a client deciding whether to show "we are
    down". Unauthenticated and unthrottled: a monitor polling once a minute
    would otherwise use up the anonymous rate on its own.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    @extend_schema(
        tags=["Health"],
        summary="Health check",
        responses={
            200: inline_serializer("Health", {"status": serializers.CharField()}),
            503: inline_serializer("HealthDown", {"status": serializers.CharField()}),
        },
    )
    def get(self, request, *args, **kwargs):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except DatabaseError:
            return Response({"status": "unavailable"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"status": "ok"})


class AppVersionView(APIView):
    """The newest client build and the oldest still supported (D90, D106).

    Public and unthrottled, like health: a client checks it before it can
    sign in. Either value is empty when it is not configured.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    @extend_schema(
        tags=["Health"],
        summary="Client app version",
        description="Build ids look like `YYYYMMDDHHMM-<shortsha>` and compare by their "
        "timestamp prefix.",
        responses={
            200: inline_serializer(
                "AppVersion",
                {"latest": serializers.CharField(), "min_supported": serializers.CharField()},
            )
        },
    )
    def get(self, request, *args, **kwargs):
        return Response(
            {
                "latest": settings.CLIENT_LATEST_VERSION,
                "min_supported": settings.CLIENT_MIN_VERSION,
            }
        )
