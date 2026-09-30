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
from django.http import Http404
from django.urls import path
from django.views.decorators.debug import sensitive_variables
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from config.api.common import RATE_LIMIT_RESPONSE, MessageSerializer

from . import audit, ratelimit
from .google import GoogleSignInError, google_signin_enabled, sign_in_with_google

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


# --- Helpers --------------------------------------------------------------


def issue_tokens(user, request=None):
    """A fresh access/refresh pair, plus the profile a client needs at once.

    Every path that signs a device in ends here.
    """
    refresh = RefreshToken.for_user(user)
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
        "blacklisted at once: presenting it again is a 401.",
        request=RefreshSerializer,
        responses={
            200: RefreshedPairSerializer,
            401: OpenApiResponse(MessageSerializer, description="Invalid, expired or revoked."),
            429: RATE_LIMIT_RESPONSE,
        },
    )
    @sensitive_variables()
    def post(self, request, *args, **kwargs):
        try:
            return super().post(request, *args, **kwargs)
        except User.DoesNotExist:
            # simplejwt looks the token's user up with a bare .get(); a token
            # outliving its account would otherwise be a 500.
            raise InvalidToken("No active account found for the given token.") from None


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


urlpatterns = [
    path("auth/google/", GoogleLoginView.as_view(), name="auth-google"),
    path("auth/refresh/", RefreshView.as_view(), name="auth-refresh"),
    path("auth/logout/", LogoutView.as_view(), name="auth-logout"),
    path("me/", MeView.as_view(), name="me"),
]
