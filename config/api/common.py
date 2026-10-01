"""Response shapes shared by every API module, named once for the schema."""

from drf_spectacular.utils import OpenApiResponse
from rest_framework import serializers


class MessageSerializer(serializers.Serializer):
    """A plain answer: what happened, and a stable code a client can branch on."""

    detail = serializers.CharField()
    code = serializers.CharField()


RATE_LIMIT_RESPONSE = OpenApiResponse(MessageSerializer, description="Too many, too recently.")

SYSTEM_LIMIT_RESPONSE = OpenApiResponse(
    MessageSerializer,
    description="`system_limit_reached`: this feature is paused for everyone (a system-wide "
    "limit). Not the caller's own quota; try again later.",
)
