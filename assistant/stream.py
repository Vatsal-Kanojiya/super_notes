"""An ask's answer as it is written, relayed as server-sent events (DECISIONS D370-D377).

``GET ask/<id>/stream/`` (assistant/api.py) checks who is asking and whose
ask it is, then hands the response body to :func:`events`, an async
generator that the ASGI server drives on its event loop: an open stream
holds a Redis subscription and a coroutine, never a thread.

What the client receives, as ``event: <type>`` plus one ``data:`` line of
JSON that repeats ``type``:

* ``snapshot`` -- ``{"text", "offset"}``: the answer so far, from the row's
  ``partial_answer``; replaces whatever the client had. ``offset`` is its
  length, where the next delta starts. Sent first for an unfinished ask.
* ``delta`` -- ``{"offset", "text"}``: append ``text``. Relayed deltas are
  contiguous: the server drops what the client already has (by the
  worker's offsets, assistant/events.py) and fills a gap from the row, so
  ``offset`` always equals the length of the text so far (in code points,
  as Python counts them) and the client need not check it.
* ``reset`` -- the answer is starting over (a retry): clear the text.
* ``done`` / ``failed`` -- ``{"ask"}``: the row exactly as ``GET ask/<id>/``
  returns it. The last event.
* ``timeout`` -- the stream reached ``ASK_STREAM_MAX_SECONDS``;
  ``unavailable`` -- live events are off or Redis could not be reached.
  Both are last events: the client goes on by polling ``GET ask/<id>/``.

A ``: keep-alive`` comment is sent after ``ASK_STREAM_HEARTBEAT_SECONDS``
without output, so proxies keep an idle stream open. A stream that ends
without one of the last events (a dropped connection) is the client's cue
to poll, or to open the stream again: it starts from the row's text.

Polling stays the source of truth. The row is read again every
``ASK_STREAM_RECHECK_SECONDS``, so an ask that ends without an event (the
sweeper failed it, or the worker could not reach Redis) still ends its
stream; and every ``ASK_PARTIAL_SAVE_SECONDS`` while deltas are waiting on
a gap, which the row's text then fills.
"""

import asyncio
import json
import logging
from dataclasses import dataclass

import redis
import redis.asyncio as aioredis
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder

from config.middleware import _request_id

from . import events as ask_events

logger = logging.getLogger(__name__)

HEARTBEAT = ": keep-alive\n\n"
END_TYPES = ("done", "failed")


def sse(event_type: str, **data) -> str:
    """One server-sent event: its type, and one line of JSON that repeats it."""
    payload = json.dumps(
        {"type": event_type, **data},
        cls=DjangoJSONEncoder,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # json.dumps escapes newlines, so the data is one line, as SSE needs.
    return f"event: {event_type}\ndata: {payload}\n\n"


# --- The subscription -----------------------------------------------------


class Unavailable(Exception):
    """Live events are off, or Redis could not be reached."""


class RedisSubscription:
    """The ask's channel on ``ASK_EVENTS_REDIS_URL``, through redis' asyncio client.

    A connection of its own per stream (a subscribed connection can do
    nothing else), closed with the stream. Opening waits for Redis to
    confirm the subscription: only then is every later publish sure to
    reach it, which is what makes reading the row afterwards gap-free.
    """

    def __init__(self, url: str, channel: str):
        self.client = aioredis.Redis.from_url(
            url, socket_connect_timeout=ask_events.CONNECT_TIMEOUT_SECONDS
        )
        self.pubsub = self.client.pubsub()
        self.channel = channel

    async def open(self) -> None:
        await self.pubsub.subscribe(self.channel)
        loop = asyncio.get_running_loop()
        wait = ask_events.CONNECT_TIMEOUT_SECONDS + ask_events.SOCKET_TIMEOUT_SECONDS
        deadline = loop.time() + wait
        while loop.time() < deadline:
            message = await self.pubsub.get_message(timeout=deadline - loop.time())
            if message is not None and message["type"] == "subscribe":
                return
        raise Unavailable("Redis did not confirm the subscription")

    async def get(self, timeout: float):
        """The next message's data, or None if none came within ``timeout`` seconds."""
        message = await self.pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
        return None if message is None else message["data"]

    async def close(self) -> None:
        try:
            await self.pubsub.aclose()
        finally:
            await self.client.aclose()


async def subscribe(ask_id: int):
    """An open subscription to the ask's events; raises :class:`Unavailable`.

    Tests replace this with a fake that has the same ``get`` and ``close``.
    """
    url = settings.ASK_EVENTS_REDIS_URL
    if not url:
        raise Unavailable("ASK_EVENTS_REDIS_URL is empty")
    try:
        subscription = RedisSubscription(url, ask_events.channel(ask_id))
    except ValueError as exc:  # a URL redis cannot parse
        raise Unavailable(repr(exc)) from exc
    try:
        await subscription.open()
    except (redis.RedisError, OSError, Unavailable) as exc:
        await _close_quietly(subscription)
        raise Unavailable(repr(exc)) from exc
    return subscription


async def _close_quietly(subscription) -> None:
    try:
        await subscription.close()
    except Exception:
        logger.warning("Could not close an answer stream's subscription", exc_info=True)


# --- The row --------------------------------------------------------------


@dataclass
class Row:
    status: str
    partial: str
    data: dict | None  # as GET ask/<id>/ returns it, once finished

    @property
    def finished(self) -> bool:
        return self.data is not None


def _read_row(ask_id: int, user_id: int) -> Row | None:
    from .api import AskQuerySerializer, _visible_asks

    ask = _visible_asks(user_id).filter(pk=ask_id).first()
    if ask is None:
        return None
    data = AskQuerySerializer(ask).data if ask.finished else None
    return Row(status=ask.status, partial=ask.partial_answer, data=data)


read_row = sync_to_async(_read_row, thread_sensitive=True)


# --- Deduplication --------------------------------------------------------


class Relay:
    """The text the client has, and what to send it next.

    Pure bookkeeping, no I/O. ``text`` is what the client holds; each method
    returns the ``(offset, text)`` deltas that extend it, never anything it
    already has. A worker's delta that starts beyond ``text`` (it was
    published before this stream subscribed, and the row's last save did not
    have it yet) waits in ``pending`` until the row's text fills the gap.
    """

    def __init__(self, text: str = ""):
        self.text = text
        self.pending: list[tuple[int, str]] = []

    @property
    def behind(self) -> bool:
        return bool(self.pending)

    def delta(self, offset: int, piece: str) -> list[tuple[int, str]]:
        if offset > len(self.text):
            self.pending.append((offset, piece))
            return []
        return self._take(offset, piece) + self._drain()

    def row(self, partial: str) -> list[tuple[int, str]]:
        """Catch up from the row's text, if it carries on from ours.

        A row whose text does not start with ours belongs to a newer run of
        the task; its ``reset`` is on the way, so it is ignored until then.
        """
        sent = []
        if len(partial) > len(self.text) and partial.startswith(self.text):
            sent.append((len(self.text), partial[len(self.text) :]))
            self.text = partial
        return sent + self._drain()

    def reset(self) -> None:
        self.text = ""
        self.pending.clear()

    def _take(self, offset: int, piece: str) -> list[tuple[int, str]]:
        have = len(self.text)
        if offset + len(piece) <= have:
            return []  # the client has all of it
        new = piece[have - offset :]
        self.text += new
        return [(have, new)]

    def _drain(self) -> list[tuple[int, str]]:
        sent = []
        while True:
            ready = [p for p in self.pending if p[0] <= len(self.text)]
            if not ready:
                return sent
            self.pending = [p for p in self.pending if p[0] > len(self.text)]
            for offset, piece in sorted(ready):
                sent += self._take(offset, piece)


# --- The stream -----------------------------------------------------------


def catch_up(ask_id: int, user_id: int) -> list[str]:
    """The catch-up alone, then ``unavailable``: the stream under WSGI.

    ``runserver`` would buffer an async stream whole and send it when it
    ends, so there the endpoint answers at once from the row instead
    (DECISIONS D375).
    """
    row = _read_row(ask_id, user_id)
    if row is None:
        return []
    if row.finished:
        return [sse(row.status, ask=row.data)]
    return [_snapshot(row.partial), sse("unavailable")]


def _snapshot(text: str) -> str:
    return sse("snapshot", text=text, offset=len(text))


async def events(ask_id: int, user_id: int, *, request_id: str = "-"):
    """The ask's events as SSE text, for a StreamingHttpResponse under ASGI.

    Subscribes first and reads the row second, so nothing published in
    between is lost: either the row has it or the subscription does (and
    the Relay drops what is in both). The server cancelling the body (the
    client went away) or closing the generator closes the subscription.
    """
    # The request id middleware has reset it by the time the body is sent;
    # this generator runs in the server's task for this request alone.
    _request_id.set(request_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.ASK_STREAM_MAX_SECONDS
    subscription = None
    try:
        try:
            subscription = await subscribe(ask_id)
        except Unavailable as exc:
            logger.warning("Ask %s: streaming without live events: %s", ask_id, exc)

        row = await read_row(ask_id, user_id)
        if row is None:  # deleted since the view looked it up
            return
        if row.finished:
            yield sse(row.status, ask=row.data)
            return
        relay = Relay(row.partial)
        yield _snapshot(relay.text)
        if subscription is None:
            yield sse("unavailable")
            return

        heartbeat_at = loop.time() + settings.ASK_STREAM_HEARTBEAT_SECONDS
        check_at = loop.time() + settings.ASK_STREAM_RECHECK_SECONDS
        while True:
            now = loop.time()
            if now >= deadline:
                yield sse("timeout")
                return

            out = []
            if now >= check_at:
                row = await read_row(ask_id, user_id)
                if row is None:
                    return
                if row.finished:
                    yield sse(row.status, ask=row.data)
                    return
                out += relay.row(row.partial)
                check_at = loop.time() + _check_interval(relay)

            if not out and now >= heartbeat_at:
                out.append(HEARTBEAT)
            for item in out:
                yield item if item == HEARTBEAT else sse("delta", offset=item[0], text=item[1])
            if out:
                heartbeat_at = loop.time() + settings.ASK_STREAM_HEARTBEAT_SECONDS

            wait = min(deadline, check_at, heartbeat_at) - loop.time()
            try:
                data = await subscription.get(timeout=max(wait, 0.0))
            except (redis.RedisError, OSError) as exc:
                logger.warning("Ask %s: lost its live events: %r", ask_id, exc)
                yield sse("unavailable")
                return
            if data is None:
                continue

            event = _parse(ask_id, data)
            kind = event.get("type")
            if kind in END_TYPES and isinstance(event.get("ask"), dict):
                yield sse(kind, ask=event["ask"])
                return
            sent = []
            if kind == "reset":
                relay.reset()
                sent.append(sse("reset"))
            elif kind == "delta" and _is_delta(event):
                sent += [
                    sse("delta", offset=o, text=t)
                    for o, t in relay.delta(event["offset"], event["text"])
                ]
                if relay.behind:
                    check_at = min(check_at, loop.time() + settings.ASK_PARTIAL_SAVE_SECONDS)
            for item in sent:
                yield item
            if sent:
                heartbeat_at = loop.time() + settings.ASK_STREAM_HEARTBEAT_SECONDS
    finally:
        if subscription is not None:
            await _close_quietly(subscription)


def _check_interval(relay: Relay) -> float:
    # While deltas wait on a gap, the row's next save fills it.
    if relay.behind:
        return settings.ASK_PARTIAL_SAVE_SECONDS
    return settings.ASK_STREAM_RECHECK_SECONDS


def _parse(ask_id: int, data) -> dict:
    try:
        event = json.loads(data)
    except (TypeError, ValueError):
        logger.warning("Ask %s: an event that is not JSON was ignored", ask_id)
        return {}
    return event if isinstance(event, dict) else {}


def _is_delta(event: dict) -> bool:
    offset, text = event.get("offset"), event.get("text")
    return (
        isinstance(offset, int)
        and not isinstance(offset, bool)
        and offset >= 0
        and isinstance(text, str)
    )
