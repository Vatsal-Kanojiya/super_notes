"""What the providers share: vector normalisation and one HTTP call with the
error translation every vendor needs.

The real providers talk to the vendors' REST APIs with ``requests`` (already
a dependency) instead of their SDKs (D37), so the translation from HTTP
failures to this package's two exceptions is written once, here.
"""

import math

import requests

from ..errors import EmbeddingError, EmbeddingTransientError

# (connect, read) seconds. Connecting should be near-instant; a batch of
# 64 chunks is well under a second of vendor time, so 30 leaves plenty of
# headroom and still frees the worker from a hung socket long before
# Celery's soft time limit.
TIMEOUT = (5, 30)

# Vendor error messages are quoted into exceptions (and so into logs);
# enough to diagnose, not a whole HTML error page.
MAX_MESSAGE = 300


def l2_normalise(vector: list[float]) -> list[float]:
    """Scale to unit length, so cosine distance and dot product agree.

    A zero vector is returned as is; there is no direction to keep.
    """
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return list(vector)
    return [value / norm for value in vector]


def post_json(vendor: str, url: str, headers: dict, payload: dict) -> dict:
    """POST ``payload`` and return the decoded JSON body.

    Raises EmbeddingTransientError for what a retry can fix (429, 5xx,
    timeouts, connection failures) and EmbeddingError for everything else.
    The headers carry the API key, so neither they nor the request are
    ever put into a message.
    """
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT)
    except (requests.ConnectionError, requests.Timeout) as exc:
        reason = type(exc).__name__
        raise EmbeddingTransientError(f"{vendor} could not be reached: {reason}") from exc
    except requests.RequestException as exc:
        raise EmbeddingError(f"{vendor} request failed: {type(exc).__name__}") from exc

    status = response.status_code
    if status >= 400:
        code, message = _error_details(response)
        # OpenAI's 401 quotes the key it was sent (masked for real keys,
        # verbatim for short malformed ones). Never let it reach a log.
        for value in headers.values():
            for secret in (value, value.removeprefix("Bearer ")):
                if secret:
                    message = message.replace(secret, "[redacted]")
        detail = f"{vendor} answered {status}: {message}"
        # OpenAI answers 429 both for "slow down" and for "your account has
        # no credit left". The second will not fix itself by waiting, so it
        # is not retried.
        if status == 429 and code != "insufficient_quota":
            raise EmbeddingTransientError(detail)
        if status >= 500:
            raise EmbeddingTransientError(detail)
        # 400, 401, 403, 404: a bad key, model or request. Identical on retry.
        raise EmbeddingError(detail)

    try:
        body = response.json()
    except ValueError as exc:
        raise EmbeddingError(f"{vendor} returned a body that is not JSON") from exc
    if not isinstance(body, dict):
        raise EmbeddingError(f"{vendor} returned an unexpected response shape")
    return body


def _error_details(response) -> tuple[str, str]:
    """(code, message) from a vendor's JSON error body, when it has one.

    Both vendors answer ``{"error": {"message": ..., ...}}``; OpenAI adds a
    string ``code``, Gemini a ``status`` such as "INVALID_ARGUMENT".
    """
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        error = None
    if not isinstance(error, dict):
        return "", (response.text or "")[:MAX_MESSAGE]
    code = error.get("code") if isinstance(error.get("code"), str) else error.get("status", "")
    message = error.get("message")
    return str(code or ""), str(message or "")[:MAX_MESSAGE]
