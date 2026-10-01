"""Cross-cutting request handling: size limit, CSP, request ids.

Carried over from the reference (expense_management/config/middleware.py)
with its reasoning; trimmed to what an API-only project needs.
"""

import logging
import re
import uuid
from contextvars import ContextVar

from django.conf import settings
from django.http import JsonResponse
from django.utils.cache import patch_vary_headers

# ContextVar, not threading.local: under ASGI one thread interleaves many
# requests, and a thread-local would leak one request's id into another's
# log lines. A ContextVar is per task, which is what "this request" means.
_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def get_request_id() -> str:
    return _request_id.get()


class MaxUploadSizeMiddleware:
    """Refuse a request that is already too big, before Django reads it.

    First in MIDDLEWARE, so nothing upstream of it gets a chance to read an
    oversized body into memory. A cheap check from the header alone, not a
    guarantee: a client that lies about Content-Length, or sends the body
    chunked, lands on DATA_UPLOAD_MAX_MEMORY_SIZE instead, later. In
    production the reverse proxy's own body limit is the backstop.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        limit = settings.DATA_UPLOAD_MAX_MEMORY_SIZE
        content_length = request.META.get("CONTENT_LENGTH")

        if limit is not None and content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = None

            if declared_size is not None and declared_size > limit:
                # The API's own error shape, so a client reads one field.
                response = JsonResponse(
                    {"detail": "Request body too large.", "code": "too_large"}, status=413
                )
                self._add_cors(request, response)
                return response

        return self.get_response(request)

    @staticmethod
    def _add_cors(request, response):
        """Let an allowed origin read this 413 (DECISIONS D80).

        CorsMiddleware sits after this one, so the 413 never reaches it and
        a cross-origin browser would report a network error, not "too
        large". Rather than move it up -- which would run it, and so touch
        the request, before the size check -- repeat its rule for this one
        response: the same origin allow-list and path filter, and nothing
        for any other origin. Only CORS_ALLOWED_ORIGINS is honoured, the one
        form this project configures.
        """
        origin = request.META.get("HTTP_ORIGIN")
        if origin in settings.CORS_ALLOWED_ORIGINS and re.match(
            settings.CORS_URLS_REGEX, request.path_info
        ):
            response["Access-Control-Allow-Origin"] = origin
            response["Access-Control-Expose-Headers"] = ", ".join(settings.CORS_EXPOSE_HEADERS)
        patch_vary_headers(response, ["Origin"])


class ContentSecurityPolicyMiddleware:
    """Send a Content-Security-Policy header on every response.

    Django has no built-in CSP. The API answers JSON, which a browser never
    runs, but the admin and the Swagger UI are HTML, and an error page can
    be too -- so every response carries a close-to-lockdown policy.

    The one page that needs more is the Swagger UI at ``/api/v1/docs/``: it
    loads its JS and CSS from drf-spectacular's default CDN and boots with
    an inline ``<script>``, so that one path gets a wider policy instead of
    loosening the default everywhere.
    """

    DEFAULT_POLICY = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'"
    )

    SWAGGER_UI_HOST = "https://cdn.jsdelivr.net"
    DOCS_POLICY = (
        "default-src 'self'; "
        f"script-src 'self' 'unsafe-inline' {SWAGGER_UI_HOST}; "
        f"style-src 'self' 'unsafe-inline' {SWAGGER_UI_HOST}; "
        f"img-src 'self' data: {SWAGGER_UI_HOST}; "
        f"font-src 'self' {SWAGGER_UI_HOST}; "
        "connect-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'"
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        policy = self.DOCS_POLICY if request.path == self._docs_path() else self.DEFAULT_POLICY
        response.setdefault("Content-Security-Policy", policy)
        return response

    @staticmethod
    def _docs_path():
        # Resolved, not hardcoded, so moving the docs URL cannot silently
        # leave the Swagger UI under the strict policy (and broken).
        from django.urls import NoReverseMatch, reverse

        try:
            return reverse("api:v1:docs")
        except NoReverseMatch:
            return None


class RequestIDMiddleware:
    """Attach an id to every request, and echo it back on the response.

    An inbound X-Request-ID is honoured so an id set by a load balancer or
    a client survives into these logs. Safe to trust: the value is only
    ever written to a log and a response header, never used to look
    anything up. Truncated, so nobody can write arbitrarily long log lines.
    """

    HEADER = "HTTP_X_REQUEST_ID"
    RESPONSE_HEADER = "X-Request-ID"
    MAX_LENGTH = 64

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get(self.HEADER, "")
        request_id = incoming[: self.MAX_LENGTH].strip() or uuid.uuid4().hex[:12]

        request.request_id = request_id
        token = _request_id.set(request_id)

        try:
            response = self.get_response(request)
        finally:
            # Reset even when the view raised, or the id outlives the
            # request and labels whatever the worker handles next.
            _request_id.reset(token)

        response[self.RESPONSE_HEADER] = request_id
        return response


class ClientMinVersionMiddleware:
    """Put ``X-Client-Min-Version`` on every API response when it is configured (D106).

    A client compares it with its own build on every call, so a build that
    is too old finds out without waiting for its next app open. Only
    ``/api/`` paths; CORS exposes the header (CORS_EXPOSE_HEADERS).
    """

    HEADER = "X-Client-Min-Version"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        minimum = settings.CLIENT_MIN_VERSION
        if minimum and request.path.startswith("/api/"):
            response[self.HEADER] = minimum
        return response


class RequestIDFilter(logging.Filter):
    """Put the current request id on every log record.

    A filter rather than a formatter or an adapter, because it is the only
    hook that reaches records emitted by Django and third-party libraries.
    """

    def filter(self, record):
        record.request_id = get_request_id()
        return True
