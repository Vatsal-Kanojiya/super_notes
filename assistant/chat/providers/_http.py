"""The one HTTP call the three real providers share, and its error translation.

Plain ``requests`` rather than each vendor's SDK (DECISIONS D53): three SDKs
would be three new dependencies (plan §3) for one POST each. The price is
that this module does what an SDK would -- timeouts, and telling a failure
worth retrying from one that is not -- so it is done once, here.
"""

import os

import requests
from django.conf import settings

from ..errors import ChatError, TransientChatError

# Connecting should be quick; it is the answer that takes time.
CONNECT_TIMEOUT = 5

# Statuses that mean "not now" rather than "not ever": request timeout,
# conflict (Anthropic's SDK retries it too), and rate limiting. Every 5xx
# joins them, including Anthropic's 529 "overloaded".
TRANSIENT_STATUSES = {408, 409, 429}

# Enough of a vendor's error body to diagnose from AskQuery.error, not so
# much that a verbose error page fills the column.
ERROR_EXCERPT_CHARS = 300


def api_key(vendor: str, *names: str) -> str:
    """The first of these env vars that is set, or ChatError.

    A missing key is configuration, not an outage: retrying cannot fix it.
    """
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise ChatError(f"{vendor} is the chat provider but {' / '.join(names)} is not set")


def post_json(vendor: str, url: str, headers: dict, body: dict) -> dict:
    """POST `body` as JSON and return the parsed response, or raise a chat error."""
    try:
        response = requests.post(
            url,
            json=body,
            headers=headers,
            timeout=(CONNECT_TIMEOUT, settings.CHAT_TIMEOUT_SECONDS),
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise TransientChatError(f"{vendor} could not be reached: {exc}") from exc
    except requests.RequestException as exc:
        # An invalid URL and the like: a bug or bad configuration, and the
        # same call would fail identically on retry.
        raise ChatError(f"{vendor} request failed: {exc}") from exc

    status = response.status_code
    if status in TRANSIENT_STATUSES or status >= 500:
        raise TransientChatError(f"{vendor} returned {status}: {_excerpt(response)}")
    if status >= 400:
        # A bad key (401/403), an unknown model (404), a request the vendor
        # rejects outright (400/422): identical on every retry.
        raise ChatError(f"{vendor} rejected the request ({status}): {_excerpt(response)}")

    try:
        return response.json()
    except ValueError as exc:
        raise ChatError(f"{vendor} returned a body that is not JSON") from exc


def _excerpt(response) -> str:
    return response.text[:ERROR_EXCERPT_CHARS]
