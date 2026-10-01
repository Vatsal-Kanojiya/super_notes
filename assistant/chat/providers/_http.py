"""The one HTTP call the three real providers share, and its error translation.

Plain ``requests`` rather than each vendor's SDK (DECISIONS D53): three SDKs
would be three new dependencies (plan §3) for one POST each. The price is
that this module does what an SDK would -- timeouts, telling a failure
worth retrying from one that is not, and reading a Server-Sent Events
stream (DECISIONS D361) -- so it is done once, here.
"""

import json
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

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

# One SSE line longer than this is not a vendor event: a stream that never
# sends a newline would otherwise be buffered until memory runs out.
MAX_SSE_LINE_BYTES = 1_000_000


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
    response = _post(vendor, url, headers, body, stream=False)
    try:
        return response.json()
    except ValueError as exc:
        raise ChatError(f"{vendor} returned a body that is not JSON") from exc


def post_stream(vendor: str, url: str, headers: dict, body: dict) -> Iterator["ServerSentEvent"]:
    """POST `body` as JSON and yield the Server-Sent Events of the response.

    The status is checked before the first event, exactly as post_json does.
    A connection that drops or stalls mid-stream (CHAT_TIMEOUT_SECONDS
    between two reads) is a TransientChatError; so is a stream that simply
    ends, which the caller notices by never seeing its vendor's last event.
    Closing the generator closes the connection.
    """
    response = _post(vendor, url, headers, body, stream=True)
    try:
        yield from parse_sse(_chunks(vendor, response))
    finally:
        response.close()


def _post(vendor: str, url: str, headers: dict, body: dict, *, stream: bool):
    try:
        response = requests.post(
            url,
            json=body,
            headers=headers,
            timeout=(CONNECT_TIMEOUT, settings.CHAT_TIMEOUT_SECONDS),
            **({"stream": True} if stream else {}),
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise TransientChatError(f"{vendor} could not be reached: {exc}") from exc
    except requests.RequestException as exc:
        # An invalid URL and the like: a bug or bad configuration, and the
        # same call would fail identically on retry.
        raise ChatError(f"{vendor} request failed: {exc}") from exc

    status = response.status_code
    if status >= 400:
        try:
            excerpt = _excerpt(response)
        finally:
            if stream:
                response.close()
        raise status_error(vendor, status, excerpt)
    return response


def status_error(vendor: str, status: int, detail: str) -> Exception:
    """The chat error an HTTP status means -- also for an error a stream reports mid-way."""
    if status in TRANSIENT_STATUSES or status >= 500:
        return TransientChatError(f"{vendor} returned {status}: {detail}")
    # A bad key (401/403), an unknown model (404), a request the vendor
    # rejects outright (400/422): identical on every retry.
    return ChatError(f"{vendor} rejected the request ({status}): {detail}")


def _excerpt(response) -> str:
    return response.text[:ERROR_EXCERPT_CHARS]


def excerpt(text) -> str:
    """A vendor's message, cut to ERROR_EXCERPT_CHARS."""
    return str(text)[:ERROR_EXCERPT_CHARS]


def _chunks(vendor: str, response) -> Iterator[bytes]:
    try:
        # chunk_size=None with stream=True: each piece as it arrives, so a
        # delta is passed on when the vendor sends it, not when a buffer fills.
        yield from response.iter_content(chunk_size=None)
    except (
        requests.ConnectionError,
        requests.Timeout,
        requests.exceptions.ChunkedEncodingError,
    ) as exc:
        raise TransientChatError(f"{vendor}'s stream was cut off: {exc}") from exc
    except requests.RequestException as exc:
        raise ChatError(f"{vendor}'s stream could not be read: {exc}") from exc


@dataclass(frozen=True)
class ServerSentEvent:
    event: str
    data: str

    def json(self, vendor: str) -> dict:
        """The event's data as a JSON object, or ChatError."""
        try:
            value = json.loads(self.data)
        except ValueError as exc:
            raise ChatError(f"{vendor} sent an event that is not JSON") from exc
        if not isinstance(value, dict):
            raise ChatError(f"{vendor} sent an event that is not a JSON object")
        return value


def parse_sse(chunks: Iterable[bytes]) -> Iterator[ServerSentEvent]:
    """The events in a Server-Sent Events byte stream, chunked however it arrives.

    The WHATWG rules, as far as the three vendors use them: lines end in
    ``\\n`` or ``\\r\\n`` (a lone ``\\r`` is not supported: none of them sends
    one); ``event:`` names the event ("message" when absent); ``data:`` lines
    are joined with ``\\n``; a line starting with ``:`` is a comment (a
    keep-alive) and is skipped; other fields (``id:``, ``retry:``) are ignored;
    a blank line ends the event, and one with no data is not dispatched. An
    event the stream ends inside of is dropped, never parsed half-received.
    Lines are decoded only once whole, so a character split between two
    chunks arrives intact.
    """
    buffer = b""
    event = ""
    data: list[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        buffer += chunk
        *lines, buffer = buffer.split(b"\n")
        if len(buffer) > MAX_SSE_LINE_BYTES:
            raise ChatError("The chat provider sent an event line that is too long")
        for raw in lines:
            line = raw.removesuffix(b"\r").decode("utf-8", errors="replace")
            if not line:
                if data:
                    yield ServerSentEvent(event or "message", "\n".join(data))
                event, data = "", []
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "event":
                event = value
            elif field == "data":
                data.append(value)
