"""The account API: sign in with Google, refresh, log out, and who am I.

Carried over from the reference (expense_management/accounts/api.py), minus
everything a Google-only app has no use for: sign-up forms, passwords,
email verification, two-step codes. What is left is the transport around
accounts/google.py -- a Google ID token in, a bearer token pair out -- and
the refresh-token lifecycle.

The views a signed-out client calls set ``authentication_classes = []``, so
an expired access token sent out of habit cannot turn a sign-in or a
refresh into a 401. They share the ``auth`` throttle scope
(config/settings.py), on top of the per-address cap on failed sign-ins in
accounts/ratelimit.py.
"""

import zoneinfo
from functools import cache

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import update_last_login
from django.db import transaction
from django.db.models import F
from django.http import Http404
from django.urls import path
from django.views.decorators.debug import sensitive_variables
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_field
from rest_framework import serializers, status
from rest_framework.exceptions import APIException, NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from assistant import quota
from assistant.api import AskUsageSerializer
from config.api.common import RATE_LIMIT_RESPONSE, MessageSerializer

from . import audit, devices, lifecycle, push, ratelimit, signals
from .google import GoogleSignInError, google_signin_enabled, sign_in_with_google
from .models import PushSubscription, SignedInDevice

User = get_user_model()

AUTH_TAG = ["Account"]


# --- Shapes ---------------------------------------------------------------


class MeSerializer(serializers.ModelSerializer):
    """The signed-in account, as a client shows it."""

    ask_usage = serializers.SerializerMethodField(
        help_text="This month's asks, counted from AskQuery rows (assistant/quota.py)."
    )

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "name",
            "avatar_url",
            "plan",
            "date_joined",
            "timezone",
            "memory_enabled",
            "memory_choice_explicit",
            "ask_usage",
        ]
        read_only_fields = fields

    @extend_schema_field(AskUsageSerializer)
    def get_ask_usage(self, user):
        return AskUsageSerializer(quota.usage(user)).data


@cache
def _timezone_names():
    return frozenset(zoneinfo.available_timezones())


class MeUpdateSerializer(serializers.Serializer):
    """What ``PATCH me/`` accepts. Anything else is ignored."""

    timezone = serializers.CharField(
        required=False, max_length=64, help_text="An IANA timezone name, e.g. `Asia/Kolkata`."
    )
    memory_enabled = serializers.BooleanField(
        required=False,
        help_text="Also marks the choice as the user's own (`memory_choice_explicit`).",
    )

    def validate_timezone(self, value):
        if value not in _timezone_names():
            raise serializers.ValidationError("Not a valid IANA timezone name.")
        return value


class SessionOpenSerializer(serializers.Serializer):
    platform = serializers.ChoiceField(choices=["web", "android"])
    app_version = serializers.CharField(
        max_length=64, allow_blank=True, help_text="The client's build id."
    )
    reason = serializers.ChoiceField(choices=["launch", "resume"])


class NoticeSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=["update", "memory"])
    required = serializers.BooleanField(
        required=False, help_text="`update` only: the build is older than `min_supported`."
    )
    style = serializers.ChoiceField(
        choices=["prominent", "subtle"], required=False, help_text="`memory` only."
    )
    state = serializers.ChoiceField(
        choices=["on", "off"], required=False, help_text="`memory` only."
    )


class SessionOpenResponseSerializer(serializers.Serializer):
    notices = NoticeSerializer(many=True)
    server_time = serializers.DateTimeField()


class TokenPairSerializer(serializers.Serializer):
    access = serializers.CharField(help_text="Send as `Authorization: Bearer <access>`.")
    refresh = serializers.CharField(help_text="Exchange at `auth/refresh/` for a new pair.")
    user = MeSerializer()


class GoogleLoginSerializer(serializers.Serializer):
    id_token = serializers.CharField(
        help_text="The ID token Google Identity Services (web) or the Android sign-in "
        "plugin returns."
    )


class RefreshSerializer(serializers.Serializer):
    refresh = serializers.CharField()


class RefreshedPairSerializer(serializers.Serializer):
    access = serializers.CharField()
    refresh = serializers.CharField(help_text="The next refresh token. The one sent is now dead.")


class DeviceSerializer(serializers.ModelSerializer):
    """A signed-in device, as the list shows it. Never its token id."""

    current = serializers.SerializerMethodField(
        help_text="True for the device making this request, found from the `device` claim "
        "its access token carries."
    )

    class Meta:
        model = SignedInDevice
        fields = ["id", "label", "created_at", "last_seen_at", "current"]
        read_only_fields = fields

    @extend_schema_field(serializers.BooleanField)
    def get_current(self, device):
        auth = self.context["request"].auth
        return auth is not None and auth.get(DEVICE_CLAIM) == device.pk


# --- Helpers --------------------------------------------------------------


# The SignedInDevice id, carried in the refresh token and so in every access
# token made from it (simplejwt copies custom claims into both, and keeps
# them through rotation). It lets a request say which device it is --
# `current` in the devices list -- which the reference could not do for an
# API device (D18).
DEVICE_CLAIM = "device"


def issue_tokens(user, request=None, created=False):
    """A fresh access/refresh pair, plus the profile a client needs at once.

    Every path that signs a device in ends here, so this is where the new
    refresh-token chain is registered as a device -- and where the oldest
    device is signed out if that makes one too many (accounts/devices.py).
    """
    refresh = RefreshToken.for_user(user)
    device = devices.register(user, str(refresh["jti"]), request)
    refresh[DEVICE_CLAIM] = device.pk
    update_last_login(None, user)
    signals.send(signals.user_signed_in, user=user, request=request, device=device, created=created)
    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
        "user": MeSerializer(user).data,
    }


def rate_limited(detail):
    return Response(
        {"detail": detail, "code": "rate_limited"}, status=status.HTTP_429_TOO_MANY_REQUESTS
    )


GOOGLE_FAILED = {"detail": "Google sign-in failed.", "code": "google_failed"}


class PublicView(APIView):
    """For clients that are not signed in: no authentication, auth-scoped throttle."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_scope = "auth"


# --- Views ----------------------------------------------------------------
#
# @sensitive_variables() on every method that holds an ID token or a raw
# access/refresh token as a local variable -- its own, or one of a helper
# it calls, such as issue_tokens(). Without it, an unhandled exception here
# would print that value in full in the DEBUG error page and in the
# mail_admins traceback (config/settings.py's LOGGING):
# SafeExceptionReporterFilter blanks a local only when a decorator names it.


class GoogleLoginView(PublicView):
    """Sign in, or sign up on first use, with a Google ID token.

    404 when ``GOOGLE_OAUTH_CLIENT_IDS`` is empty: the feature does not exist
    until it is configured. Rate-limited by address on failures
    (accounts/ratelimit.py): before the token is verified there is no
    account to key an attempt on.
    """

    def dispatch(self, request, *args, **kwargs):
        if not google_signin_enabled():
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    @extend_schema(
        tags=AUTH_TAG,
        summary="Sign in with Google",
        description="Verifies the ID token (signature, audience = any configured client id, "
        "expiry, issuer, verified email), finds or creates the account, and returns a token "
        "pair. Every refusal is the same `google_failed`, whatever the cause.",
        request=GoogleLoginSerializer,
        responses={
            200: TokenPairSerializer,
            400: OpenApiResponse(MessageSerializer, description="Google sign-in failed."),
            404: OpenApiResponse(MessageSerializer, description="Sign-in is not configured."),
            429: RATE_LIMIT_RESPONSE,
        },
    )
    @sensitive_variables()
    def post(self, request, *args, **kwargs):
        if ratelimit.google_login_blocked(request):
            audit.record("login_blocked", request=request, via="google")
            return rate_limited("Too many attempts. Wait a few minutes and try again.")

        body = GoogleLoginSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        try:
            user, created = sign_in_with_google(body.validated_data["id_token"], request=request)
        except GoogleSignInError:
            ratelimit.record_google_login_failure(request)
            return Response(GOOGLE_FAILED, status=status.HTTP_400_BAD_REQUEST)

        return Response(issue_tokens(user, request, created=created))


class RefreshView(TokenRefreshView):
    """Exchange a refresh token for a new pair. The old refresh token dies."""

    authentication_classes = []
    throttle_scope = "auth"

    @extend_schema(
        tags=AUTH_TAG,
        summary="Refresh tokens",
        description="Exchange a refresh token for a new pair. The old refresh token is "
        "blacklisted at once: presenting it again is a 401. So is a device that was signed "
        "out -- from the devices list, or by a newer sign-in once the account is on "
        "`MAX_SIGNED_IN_DEVICES` devices (default 2): sign in again.",
        request=RefreshSerializer,
        responses={
            200: RefreshedPairSerializer,
            401: OpenApiResponse(MessageSerializer, description="Invalid, expired or revoked."),
            429: RATE_LIMIT_RESPONSE,
        },
    )
    @sensitive_variables()
    def post(self, request, *args, **kwargs):
        raw = request.data.get("refresh") if isinstance(request.data, dict) else None
        old_jti = _refresh_jti(raw)
        with transaction.atomic():
            if old_jti:
                # Two requests racing with the same refresh token would both
                # pass simplejwt's blacklist check before either wrote to it,
                # and both walk away with a live chain -- the very replay
                # rotation exists to stop. Locking the token's row makes the
                # second wait, then see the blacklist entry: a 401 (D20).
                OutstandingToken.objects.select_for_update().filter(jti=old_jti).first()
            try:
                response = super().post(request, *args, **kwargs)
            except User.DoesNotExist:
                # simplejwt looks the token's user up with a bare .get(); a
                # token outliving its account would otherwise be a 500.
                raise InvalidToken("No active account found for the given token.") from None

            # The token rotated: the device keeps one row for its whole
            # chain, moved to the new token.
            if response.status_code == 200:
                new = RefreshToken(response.data["refresh"], verify=False)
                user = User.objects.get(pk=new["user_id"])
                devices.rotate(old_jti, str(new["jti"]), user, request)
        return response


def _refresh_jti(raw):
    """The ``jti`` of a valid refresh token, else ``None``. Changes nothing."""
    if not isinstance(raw, str):
        return None
    try:
        return str(RefreshToken(raw)["jti"])
    except TokenError:
        return None


class LogoutView(PublicView):
    """Revoke one refresh token: this device is signed out, others are not."""

    @extend_schema(
        tags=AUTH_TAG,
        summary="Log out",
        description="Blacklists this refresh token. The access token beside it keeps working "
        "until it expires (`JWT_ACCESS_MINUTES`, default 30); a client should drop both.",
        request=RefreshSerializer,
        responses={
            204: None,
            400: OpenApiResponse(MessageSerializer, description="Invalid or already revoked."),
            429: RATE_LIMIT_RESPONSE,
        },
    )
    @sensitive_variables()
    def post(self, request, *args, **kwargs):
        body = RefreshSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        try:
            token = RefreshToken(body.validated_data["refresh"])
            token.blacklist()
            devices.forget(str(token["jti"]))
        except TokenError:
            return Response(
                {"detail": "That token is invalid or already revoked.", "code": "token_invalid"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # The claim, not request.user: this endpoint takes no authentication,
        # only the refresh token itself, so its subject is the only "who".
        user = User.objects.filter(pk=token.payload.get("user_id")).first()
        audit.record("logged_out", request=request, user=user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    """The signed-in account."""

    @extend_schema(
        tags=AUTH_TAG,
        summary="Your profile",
        responses={
            200: MeSerializer,
            401: OpenApiResponse(MessageSerializer, description="Not signed in."),
        },
    )
    def get(self, request, *args, **kwargs):
        return Response(MeSerializer(request.user).data)

    @extend_schema(
        tags=AUTH_TAG,
        summary="Update your settings",
        description="`timezone` (an IANA name) and `memory_enabled`. Setting `memory_enabled` "
        "also records that the user chose it themselves.",
        request=MeUpdateSerializer,
        responses={
            200: MeSerializer,
            400: OpenApiResponse(description="Invalid timezone."),
            401: OpenApiResponse(MessageSerializer, description="Not signed in."),
        },
    )
    def patch(self, request, *args, **kwargs):
        body = MeUpdateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        changed = dict(body.validated_data)
        if "memory_enabled" in changed:
            changed["memory_choice_explicit"] = True
        if changed:
            for field, value in changed.items():
                setattr(request.user, field, value)
            request.user.save(update_fields=list(changed))
        return Response(MeSerializer(request.user).data)


class DeviceListView(APIView):
    """The devices this account is signed in on."""

    @extend_schema(
        tags=AUTH_TAG,
        summary="Signed-in devices",
        description="At most `MAX_SIGNED_IN_DEVICES` (default 2): a further sign-in signs the "
        "least recently used device out. Most recently seen first; a device that has gone "
        "away (its refresh token expired or was revoked) is not listed.",
        responses={
            200: DeviceSerializer(many=True),
            401: OpenApiResponse(MessageSerializer, description="Not signed in."),
        },
    )
    def get(self, request, *args, **kwargs):
        found = devices.live_devices(request.user)
        return Response(DeviceSerializer(found, many=True, context={"request": request}).data)


class DeviceDetailView(APIView):
    """Sign one device out."""

    @extend_schema(
        tags=AUTH_TAG,
        summary="Sign a device out",
        description="Revokes that device's refresh token, so its next refresh is a 401. Its "
        "current access token keeps working until it expires (`JWT_ACCESS_MINUTES`, default "
        "30). Another account's device id is 404.",
        responses={
            204: None,
            401: OpenApiResponse(MessageSerializer, description="Not signed in."),
            404: OpenApiResponse(MessageSerializer, description="No such device of yours."),
        },
    )
    def delete(self, request, pk, *args, **kwargs):
        # Scoped to the caller in the query, so someone else's id is a 404,
        # never a 403 that says it exists.
        device = SignedInDevice.objects.filter(pk=pk, user=request.user).first()
        if device is None:
            raise NotFound("No such device.")
        devices.end(device, request=request, reason="user")
        return Response(status=status.HTTP_204_NO_CONTENT)


class SessionOpenView(APIView):
    """The app-open hook (D90)."""

    @extend_schema(
        tags=AUTH_TAG,
        summary="Report an app open",
        description="Sent when the app launches with a valid session, or resumes after "
        "5 idle hours (D93). Returns the notices to show: `update` when `app_version` is older "
        "than the latest build (`required` when older than the minimum supported), and "
        "`memory` when it is due (first open, then every `MEMORY_NOTICE_EVERY_OPENS` opens "
        "since it was last seen; confirm with `me/memory-notice/seen/`). A repeat from the "
        "same device within `APP_OPEN_MIN_INTERVAL_SECONDS` is not counted; notices come "
        "back either way.",
        request=SessionOpenSerializer,
        responses={
            200: SessionOpenResponseSerializer,
            400: OpenApiResponse(description="Invalid body."),
            401: OpenApiResponse(MessageSerializer, description="Not signed in."),
        },
    )
    def post(self, request, *args, **kwargs):
        body = SessionOpenSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        # The device claim names a row; only the caller's own is honoured.
        device_id = request.auth.get(DEVICE_CLAIM) if request.auth is not None else None
        device = lifecycle.device_for(request.user, device_id)
        notices, server_time = lifecycle.open_app(
            request.user, device, request=request, **body.validated_data
        )
        return Response({"notices": notices, "server_time": server_time})


class MemoryNoticeSeenView(APIView):
    """The client showed the memory notice."""

    @extend_schema(
        tags=AUTH_TAG,
        summary="Memory notice seen",
        description="Restarts the count to the next memory notice (D94).",
        request=None,
        responses={
            204: None,
            401: OpenApiResponse(MessageSerializer, description="Not signed in."),
        },
    )
    def post(self, request, *args, **kwargs):
        User.objects.filter(pk=request.user.pk).update(
            memory_notice_seen_at_open=F("app_open_count")
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class VapidKeySerializer(serializers.Serializer):
    public_key = serializers.CharField(
        help_text="The VAPID public key, for `pushManager.subscribe`."
    )


class PushSubscriptionSerializer(serializers.Serializer):
    """The browser's `PushSubscription.toJSON()`, flattened."""

    endpoint = serializers.CharField(
        help_text="An https URL on a known push service (`PUSH_ENDPOINT_HOSTS`), at most 1000 "
        "characters, else 400 `invalid_endpoint`."
    )
    p256dh = serializers.CharField(max_length=200, help_text="base64url, 65 bytes decoded.")
    auth = serializers.CharField(max_length=200, help_text="base64url, 16 to 32 bytes decoded.")

    def validate_p256dh(self, value):
        if not push.valid_key(value, push.P256DH_BYTES):
            raise serializers.ValidationError("Not a valid p256dh key.")
        return value

    def validate_auth(self, value):
        if not push.valid_key(value, push.AUTH_BYTES):
            raise serializers.ValidationError("Not a valid auth secret.")
        return value


class PushSubscriptionRemoveSerializer(serializers.Serializer):
    endpoint = serializers.CharField(max_length=2000)


class InvalidEndpoint(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "That is not an accepted push endpoint."
    default_code = "invalid_endpoint"


class VapidKeyView(APIView):
    """The public key a browser subscribes with."""

    @extend_schema(
        tags=AUTH_TAG,
        summary="Web push public key",
        description="404 when web push is not configured on this server.",
        responses={
            200: VapidKeySerializer,
            401: OpenApiResponse(MessageSerializer, description="Not signed in."),
            404: OpenApiResponse(MessageSerializer, description="Web push is off."),
        },
    )
    def get(self, request, *args, **kwargs):
        if not push.push_enabled():
            raise NotFound("Web push is not enabled.")
        return Response({"public_key": settings.VAPID_PUBLIC_KEY})


class PushSubscriptionView(APIView):
    """Register or remove this browser's push subscription."""

    @extend_schema(
        tags=AUTH_TAG,
        summary="Register a push subscription",
        description="Upsert by endpoint. An endpoint registered to another account moves to "
        "the caller (one browser profile, one owner). 404 when web push is off.",
        request=PushSubscriptionSerializer,
        responses={
            204: None,
            400: OpenApiResponse(MessageSerializer, description="Invalid subscription."),
            401: OpenApiResponse(MessageSerializer, description="Not signed in."),
            404: OpenApiResponse(MessageSerializer, description="Web push is off."),
        },
    )
    def post(self, request, *args, **kwargs):
        if not push.push_enabled():
            raise NotFound("Web push is not enabled.")
        # Checked before anything else about the body: the server will POST here (D173).
        endpoint = request.data.get("endpoint") if hasattr(request.data, "get") else None
        if not push.endpoint_allowed(endpoint):
            raise InvalidEndpoint()
        data = PushSubscriptionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        PushSubscription.objects.update_or_create(
            endpoint=v["endpoint"],
            defaults={
                "user": request.user,
                "p256dh": v["p256dh"],
                "auth": v["auth"],
                "user_agent": request.META.get("HTTP_USER_AGENT", "")[:200],
            },
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        tags=AUTH_TAG,
        summary="Remove a push subscription",
        description="By endpoint, among the caller's own. Removing one that is not there "
        "(or is another account's) is still 204: nothing to remove.",
        request=PushSubscriptionRemoveSerializer,
        responses={
            204: None,
            400: OpenApiResponse(MessageSerializer, description="Invalid request."),
            401: OpenApiResponse(MessageSerializer, description="Not signed in."),
        },
    )
    def delete(self, request, *args, **kwargs):
        data = PushSubscriptionRemoveSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        PushSubscription.objects.filter(
            user=request.user, endpoint=data.validated_data["endpoint"]
        ).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


urlpatterns = [
    path("push/vapid-key/", VapidKeyView.as_view(), name="push-vapid-key"),
    path("me/push-subscriptions/", PushSubscriptionView.as_view(), name="me-push-subscriptions"),
    path("auth/google/", GoogleLoginView.as_view(), name="auth-google"),
    path("auth/refresh/", RefreshView.as_view(), name="auth-refresh"),
    path("auth/logout/", LogoutView.as_view(), name="auth-logout"),
    path("auth/devices/", DeviceListView.as_view(), name="auth-devices"),
    path("auth/devices/<int:pk>/", DeviceDetailView.as_view(), name="auth-device"),
    path("me/", MeView.as_view(), name="me"),
    path("me/memory-notice/seen/", MemoryNoticeSeenView.as_view(), name="me-memory-notice-seen"),
    path("session/open/", SessionOpenView.as_view(), name="session-open"),
]
