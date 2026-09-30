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

from django.contrib.auth import get_user_model
from django.contrib.auth.models import update_last_login
from django.db import transaction
from django.http import Http404
from django.urls import path
from django.views.decorators.debug import sensitive_variables
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_field
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from config.api.common import RATE_LIMIT_RESPONSE, MessageSerializer

from . import audit, devices, ratelimit
from .google import GoogleSignInError, google_signin_enabled, sign_in_with_google
from .models import SignedInDevice

User = get_user_model()

AUTH_TAG = ["Account"]


# --- Shapes ---------------------------------------------------------------


class MeSerializer(serializers.ModelSerializer):
    """The signed-in account, as a client shows it."""

    # Phase 5 (ask) adds the month's ask usage here -- `ask_usage: {used,
    # limit, resets_at}` -- counted from AskQuery rows. Not before: there is
    # nothing to count yet.

    class Meta:
        model = User
        fields = ["id", "email", "name", "avatar_url", "plan", "date_joined"]
        read_only_fields = fields


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


def issue_tokens(user, request=None):
    """A fresh access/refresh pair, plus the profile a client needs at once.

    Every path that signs a device in ends here, so this is where the new
    refresh-token chain is registered as a device -- and where the oldest
    device is signed out if that makes one too many (accounts/devices.py).
    """
    refresh = RefreshToken.for_user(user)
    device = devices.register(user, str(refresh["jti"]), request)
    refresh[DEVICE_CLAIM] = device.pk
    update_last_login(None, user)
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
            user, _created = sign_in_with_google(body.validated_data["id_token"], request=request)
        except GoogleSignInError:
            ratelimit.record_google_login_failure(request)
            return Response(GOOGLE_FAILED, status=status.HTTP_400_BAD_REQUEST)

        return Response(issue_tokens(user, request))


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


urlpatterns = [
    path("auth/google/", GoogleLoginView.as_view(), name="auth-google"),
    path("auth/refresh/", RefreshView.as_view(), name="auth-refresh"),
    path("auth/logout/", LogoutView.as_view(), name="auth-logout"),
    path("auth/devices/", DeviceListView.as_view(), name="auth-devices"),
    path("auth/devices/<int:pk>/", DeviceDetailView.as_view(), name="auth-device"),
    path("me/", MeView.as_view(), name="me"),
]
