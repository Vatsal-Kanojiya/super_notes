"""An ask's answer as it is written: events on Redis pub/sub (DECISIONS D363-D366).

The answer task (assistant/tasks.py) publishes on channel ``ask:<id>``; the
stream endpoint relays them. Polling (``GET ask/<id>/``) is untouched and
stays the source of truth: an event that is lost costs a live update, never
an answer, and nothing here can fail the task -- every Redis error is
logged and swallowed.

Every message is one JSON object with ``seq`` and ``type``:

* ``{"seq", "type": "delta", "offset", "text"}`` -- the next piece of the
  answer. ``offset`` is where it starts in the text streamed so far (in
  code points, as Python counts them), which is also what the row's
  ``partial_answer`` holds when it was last saved: a reader that caught up
  from the row skips what it already has.
* ``{"seq", "type": "reset"}`` -- forget the text streamed so far: the
  answer is starting over (a retry after a transient error, or a run that
  takes up an ask an earlier run left running).
* ``{"seq", "type": "done" | "failed", "ask"}`` -- the end. ``ask`` is the
  row exactly as ``GET ask/<id>/`` returns it: the answer, its parsed
  citations, or the error. Published after the commit that finished it.

``seq`` counts from 1 within one run of the task and each run starts again
at 1, so 1 can follow anything; within a run, a jump means messages were
missed (pub/sub does not queue for a slow or absent reader, and a publish
that failed still uses its number) -- catch up from the row.
"""

import json
import logging

import redis
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder

logger = logging.getLogger(__name__)

# Fail fast: a publish waits at most this long for Redis, and a run that
# could not reach it stops sending deltas (each would wait again).
CONNECT_TIMEOUT_SECONDS = 0.5
SOCKET_TIMEOUT_SECONDS = 1.0

_clients: dict = {}


def channel(ask_id: int) -> str:
    return f"ask:{ask_id}"


def get_client():
    """The Redis client for ASK_EVENTS_REDIS_URL, or None when events are off.

    One per URL and process, made on first use -- after a Celery worker has
    forked, so no connection is shared between processes. A URL that cannot
    be parsed turns events off (logged), rather than failing answers.
    """
    url = settings.ASK_EVENTS_REDIS_URL
    if not url:
        return None
    client = _clients.get(url)
    if client is None:
        try:
            client = redis.Redis.from_url(
                url,
                socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
                socket_timeout=SOCKET_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception("ASK_EVENTS_REDIS_URL is not usable; answer events are off")
            return None
        _clients[url] = client
    return client


_DEFAULT = object()


class Publisher:
    """The events of one run of the answer task for one ask.

    ``client`` is anything with ``publish(channel, message)``; by default the
    configured Redis client (None: publish nothing). Tests pass a recorder.
    """

    def __init__(self, ask_id: int, client=_DEFAULT):
        self.ask_id = ask_id
        self.client = get_client() if client is _DEFAULT else client
        self.seq = 0
        # Set by the first failed publish: the rest of this run's deltas are
        # skipped, so an unreachable Redis costs one timeout, not one per
        # word. Reset and end events are still tried.
        self.unreachable = False

    def delta(self, text: str, offset: int) -> None:
        self._publish({"type": "delta", "offset": offset, "text": text}, skippable=True)

    def reset(self) -> None:
        self._publish({"type": "reset"})

    def outcome(self) -> None:
        """Publish ``done`` or ``failed`` with the row as polling returns it.

        Called after the finishing commit. Reads the row afresh, so the event
        says what a GET would say; an ask that is somehow still unfinished
        publishes nothing.
        """
        if self.client is None:
            return
        try:
            from .api import AskQuerySerializer
            from .models import AskQuery

            ask = AskQuery.objects.filter(pk=self.ask_id).first()
            if ask is None or not ask.finished:
                return
            data = AskQuerySerializer(ask).data
        except Exception:
            logger.exception("Ask %s: could not build its end event", self.ask_id)
            return
        self._publish({"type": ask.status, "ask": data})

    def _publish(self, event: dict, skippable: bool = False) -> bool:
        if self.client is None:
            return False
        self.seq += 1
        if skippable and self.unreachable:
            return False
        try:
            message = json.dumps(
                {"seq": self.seq, **event},
                cls=DjangoJSONEncoder,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            self.client.publish(channel(self.ask_id), message)
        except Exception as exc:
            if not self.unreachable:
                # Once per run: the answer goes on, and polling still has it.
                logger.warning("Ask %s: could not publish its events: %r", self.ask_id, exc)
            self.unreachable = True
            return False
        return True
