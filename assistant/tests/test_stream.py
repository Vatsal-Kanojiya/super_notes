"""GET ask/<id>/stream/: the answer as server-sent events (DECISIONS D370-D377).

Most tests drive the view through Django's AsyncClient with a fake
subscription in place of Redis (``assistant.stream.subscribe`` patched),
publishing into it while reading the stream. The disconnect test and the
real-Redis test (database 15, skipped without one) go through Django's own
ASGI handler, as uvicorn would. Relay, the deduplication, is tested on its
own first.
"""

import asyncio
import json
import random
import unittest
from unittest import mock

import redis
from asgiref.sync import sync_to_async
from django.core.handlers.asgi import ASGIHandler
from django.test import (
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.throttling import UserRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

from assistant import events, stream
from assistant.api import AskQuerySerializer, AskStreamView
from assistant.models import AskQuery, Conversation
from notes.tests.helpers import make_user

QUIET = {
    # Long enough that nothing fires unless a test lowers it.
    "ASK_STREAM_MAX_SECONDS": 10,
    "ASK_STREAM_HEARTBEAT_SECONDS": 10,
    "ASK_STREAM_RECHECK_SECONDS": 10,
    "ASK_PARTIAL_SAVE_SECONDS": 0.05,
}
READ_TIMEOUT = 3.0
# Before any test patches it.
REAL_SUBSCRIBE = stream.subscribe


def url(ask_id):
    return f"/api/v1/ask/{ask_id}/stream/"


def bearer(user):
    return {"Authorization": f"Bearer {RefreshToken.for_user(user).access_token}"}


def parse(chunk: bytes) -> dict:
    text = chunk.decode()
    if text.startswith(":"):
        return {"type": "keep-alive"}
    lines = dict(line.split(": ", 1) for line in text.strip().split("\n"))
    data = json.loads(lines["data"])
    assert data["type"] == lines["event"], text
    return data


class FakeSubscription:
    """What stream.subscribe returns, fed by the test instead of Redis."""

    def __init__(self):
        self.queue = asyncio.Queue()
        self.closed = False

    def publish(self, **event):
        self.queue.put_nowait(json.dumps(event))

    async def get(self, timeout):
        try:
            return await asyncio.wait_for(self.queue.get(), timeout)
        except TimeoutError:
            return None

    async def close(self):
        self.closed = True


# --- Relay ----------------------------------------------------------------


class RelayTests(SimpleTestCase):
    def test_a_delta_the_client_has_is_dropped_and_an_overlap_is_cut(self):
        relay = stream.Relay("Hello wor")
        self.assertEqual(relay.delta(0, "Hello "), [])
        self.assertEqual(relay.delta(6, "world"), [(9, "ld")])
        self.assertEqual(relay.delta(11, "!"), [(11, "!")])
        self.assertEqual(relay.text, "Hello world!")

    def test_a_delta_past_a_gap_waits_for_the_row(self):
        relay = stream.Relay("ab")
        self.assertEqual(relay.delta(4, "ef"), [])
        self.assertEqual(relay.delta(6, "g"), [])
        self.assertTrue(relay.behind)
        # The row's next save covers part of the gap and more.
        self.assertEqual(relay.row("abcde"), [(2, "cde"), (5, "f"), (6, "g")])
        self.assertEqual(relay.text, "abcdefg")
        self.assertFalse(relay.behind)

    def test_a_row_that_does_not_carry_on_from_the_text_is_ignored(self):
        relay = stream.Relay("first run")
        self.assertEqual(relay.row("second"), [])
        self.assertEqual(relay.row("first"), [])
        self.assertEqual(relay.text, "first run")

    def test_reset_forgets_the_text_and_what_was_waiting(self):
        relay = stream.Relay("old")
        relay.delta(10, "x")
        relay.reset()
        self.assertEqual((relay.text, relay.pending), ("", []))
        self.assertEqual(relay.delta(0, "new"), [(0, "new")])

    def test_code_points_not_bytes(self):
        relay = stream.Relay("naïve ")
        self.assertEqual(relay.delta(3, "ve café"), [(6, "café")])


# --- The endpoint, with a fake subscription -------------------------------


@override_settings(**QUIET)
class StreamTestCase(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.ask = AskQuery.objects.create(
            user=self.alice, question="When does my passport expire?", idempotency_key="k"
        )
        self.auth = bearer(self.alice)  # writes a row: not from async code
        self.subscription = FakeSubscription()
        self.subscribed = []

        async def subscribe(ask_id):
            self.subscribed.append(ask_id)
            return self.subscription

        patcher = mock.patch("assistant.stream.subscribe", subscribe)
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_row(self, **fields):
        AskQuery.objects.filter(pk=self.ask.pk).update(**fields)

    async def aset_row(self, **fields):
        await AskQuery.objects.filter(pk=self.ask.pk).aupdate(**fields)

    async def open(self, user=None, **headers):
        response = await self.async_client.get(
            url(self.ask.pk), headers={**(user or self.auth), **headers}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.streaming)
        return response, aiter(response.streaming_content)

    async def next_event(self, body):
        return parse(await asyncio.wait_for(anext(body), READ_TIMEOUT))

    async def assert_closed(self, body):
        with self.assertRaises(StopAsyncIteration):
            await asyncio.wait_for(anext(body), READ_TIMEOUT)

    async def row_as_polled(self):
        ask = await AskQuery.objects.aget(pk=self.ask.pk)
        return await sync_to_async(lambda: dict(AskQuerySerializer(ask).data))()


class CatchUpTests(StreamTestCase):
    async def test_a_done_ask_gets_its_done_event_and_the_stream_ends(self):
        await self.aset_row(status="done", answer="March 2027 [1].", completed_at=timezone.now())
        response, body = await self.open()
        self.assertEqual(response["Content-Type"], "text/event-stream; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "no-cache")
        self.assertEqual(response["X-Accel-Buffering"], "no")

        event = await self.next_event(body)
        self.assertEqual(event["type"], "done")
        polled = await self.async_client.get(f"/api/v1/ask/{self.ask.pk}/", headers=self.auth)
        self.assertEqual(event["ask"], polled.json())
        await self.assert_closed(body)
        self.assertTrue(self.subscription.closed)

    async def test_a_failed_ask_gets_its_failed_event(self):
        await self.aset_row(status="failed", error="Something went wrong.")
        _, body = await self.open()
        event = await self.next_event(body)
        self.assertEqual(
            (event["type"], event["ask"]["error"]), ("failed", "Something went wrong.")
        )
        await self.assert_closed(body)

    async def test_subscribes_before_reading_the_row(self):
        order = []
        real_read = stream.read_row

        async def subscribe(ask_id):
            order.append("subscribe")
            return self.subscription

        async def read_row(ask_id, user_id):
            order.append("read")
            return await real_read(ask_id, user_id)

        with (
            mock.patch("assistant.stream.subscribe", subscribe),
            mock.patch("assistant.stream.read_row", read_row),
        ):
            _, body = await self.open()
            await self.next_event(body)
        self.assertEqual(order, ["subscribe", "read"])


class LiveTests(StreamTestCase):
    async def test_snapshot_then_live_deltas_then_done(self):
        # A reconnect mid-answer: the row has the text saved so far.
        await self.aset_row(status="running", partial_answer="Your passport ")
        _, body = await self.open()
        self.assertEqual(
            await self.next_event(body),
            {"type": "snapshot", "text": "Your passport ", "offset": 14},
        )

        # Published before the row was read: the client has it already.
        self.subscription.publish(seq=3, type="delta", offset=5, text="passport ")
        self.subscription.publish(seq=4, type="delta", offset=14, text="expires ")
        self.assertEqual(
            await self.next_event(body), {"type": "delta", "offset": 14, "text": "expires "}
        )
        self.subscription.publish(seq=5, type="delta", offset=22, text="in March.")
        self.assertEqual(
            await self.next_event(body), {"type": "delta", "offset": 22, "text": "in March."}
        )

        await self.aset_row(
            status="done", answer="Your passport expires in March.", partial_answer=""
        )
        row = await self.row_as_polled()
        self.subscription.publish(seq=6, type="done", ask=row)
        self.assertEqual(await self.next_event(body), {"type": "done", "ask": row})
        await self.assert_closed(body)
        self.assertTrue(self.subscription.closed)
        self.assertEqual(self.subscribed, [self.ask.pk])

    async def test_an_overlapping_delta_sends_only_what_is_new(self):
        await self.aset_row(status="running", partial_answer="Hello wor")
        _, body = await self.open()
        await self.next_event(body)
        self.subscription.publish(seq=2, type="delta", offset=6, text="world")
        self.assertEqual(await self.next_event(body), {"type": "delta", "offset": 9, "text": "ld"})

    async def test_reset_clears_and_the_new_run_streams_from_zero(self):
        await self.aset_row(status="running", partial_answer="A first try")
        _, body = await self.open()
        await self.next_event(body)
        self.subscription.publish(seq=7, type="reset")
        self.subscription.publish(seq=1, type="delta", offset=0, text="Second")
        self.assertEqual(await self.next_event(body), {"type": "reset"})
        self.assertEqual(
            await self.next_event(body), {"type": "delta", "offset": 0, "text": "Second"}
        )

    async def test_a_gap_is_filled_from_the_row(self):
        # Deltas the row's last save missed were published before the
        # subscription: the next one starts past what the client has.
        await self.aset_row(status="running", partial_answer="ab")
        _, body = await self.open()
        await self.next_event(body)
        await self.aset_row(partial_answer="abcd")  # the worker's next save
        self.subscription.publish(seq=9, type="delta", offset=4, text="ef")
        self.assertEqual(await self.next_event(body), {"type": "delta", "offset": 2, "text": "cd"})
        self.assertEqual(await self.next_event(body), {"type": "delta", "offset": 4, "text": "ef"})

    async def test_rubbish_on_the_channel_is_ignored(self):
        await self.aset_row(status="running")
        _, body = await self.open()
        await self.next_event(body)
        self.subscription.queue.put_nowait(b"not json")
        self.subscription.publish(seq=1, type="delta", offset="0", text="x")
        self.subscription.publish(seq=2, type="mystery")
        self.subscription.publish(seq=3, type="delta", offset=0, text="ok")
        self.assertEqual(await self.next_event(body), {"type": "delta", "offset": 0, "text": "ok"})

    async def test_a_pending_ask_starts_from_an_empty_snapshot(self):
        _, body = await self.open()
        self.assertEqual(await self.next_event(body), {"type": "snapshot", "text": "", "offset": 0})


class TimeTests(StreamTestCase):
    async def test_the_duration_cap_ends_with_timeout(self):
        await self.aset_row(status="running")
        with self.settings(ASK_STREAM_MAX_SECONDS=0.2):
            _, body = await self.open()
            await self.next_event(body)
            self.assertEqual(await self.next_event(body), {"type": "timeout"})
            await self.assert_closed(body)
        self.assertTrue(self.subscription.closed)

    async def test_a_heartbeat_comment_while_idle(self):
        await self.aset_row(status="running")
        with self.settings(ASK_STREAM_HEARTBEAT_SECONDS=0.1):
            _, body = await self.open()
            await self.next_event(body)
            chunk = await asyncio.wait_for(anext(body), READ_TIMEOUT)
        self.assertEqual(chunk, b": keep-alive\n\n")

    async def test_an_ask_finished_without_an_event_ends_the_stream(self):
        # The sweeper failed it, or the worker could not reach Redis.
        await self.aset_row(status="running", partial_answer="Half")
        with self.settings(ASK_STREAM_RECHECK_SECONDS=0.1):
            _, body = await self.open()
            await self.next_event(body)
            await self.aset_row(status="failed", error="Stuck.", partial_answer="")
            event = await self.next_event(body)
        self.assertEqual((event["type"], event["ask"]["error"]), ("failed", "Stuck."))
        await self.assert_closed(body)

    async def test_the_recheck_also_sends_text_the_events_never_brought(self):
        await self.aset_row(status="running", partial_answer="Half")
        with self.settings(ASK_STREAM_RECHECK_SECONDS=0.1):
            _, body = await self.open()
            await self.next_event(body)
            await self.aset_row(partial_answer="Half way")
            event = await self.next_event(body)
        self.assertEqual(event, {"type": "delta", "offset": 4, "text": " way"})

    async def test_closing_the_stream_closes_the_subscription(self):
        await self.aset_row(status="running")
        _, body = await self.open()
        await self.next_event(body)
        self.assertFalse(self.subscription.closed)
        # What the ASGI server does when the client goes away: cancel the
        # task waiting on the body.
        waiting = asyncio.ensure_future(anext(body))
        await asyncio.sleep(0.05)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertTrue(self.subscription.closed)


class UnavailableTests(StreamTestCase):
    async def test_without_live_events_the_catch_up_then_unavailable(self):
        async def down(ask_id):
            raise stream.Unavailable("no Redis")

        await self.aset_row(status="running", partial_answer="So far")
        with mock.patch("assistant.stream.subscribe", down):
            _, body = await self.open()
            self.assertEqual((await self.next_event(body))["type"], "snapshot")
            self.assertEqual(await self.next_event(body), {"type": "unavailable"})
            await self.assert_closed(body)

    async def test_a_finished_ask_needs_no_live_events(self):
        async def down(ask_id):
            raise stream.Unavailable("no Redis")

        await self.aset_row(status="done", answer="Done.")
        with mock.patch("assistant.stream.subscribe", down):
            _, body = await self.open()
            self.assertEqual((await self.next_event(body))["type"], "done")

    async def test_redis_lost_mid_stream_ends_with_unavailable(self):
        await self.aset_row(status="running")
        _, body = await self.open()
        await self.next_event(body)

        async def broken(timeout):
            raise redis.ConnectionError("gone")

        self.subscription.get = broken
        self.assertEqual(await self.next_event(body), {"type": "unavailable"})
        await self.assert_closed(body)
        self.assertTrue(self.subscription.closed)

    @override_settings(ASK_EVENTS_REDIS_URL="")
    async def test_events_off_is_unavailable(self):
        with self.assertRaises(stream.Unavailable):
            await REAL_SUBSCRIBE(self.ask.pk)

    async def test_an_unreachable_redis_is_unavailable(self):
        # Nothing listens on port 1.
        with (
            override_settings(ASK_EVENTS_REDIS_URL="redis://127.0.0.1:1/15"),
            self.assertRaises(stream.Unavailable),
        ):
            await REAL_SUBSCRIBE(self.ask.pk)

    def test_under_wsgi_a_running_ask_gets_the_catch_up_only(self):
        # runserver would buffer the whole stream, so it is not live there.
        self.set_row(status="running", partial_answer="So far")
        client = APIClient()
        client.force_authenticate(self.alice)
        response = client.get(url(self.ask.pk))
        self.assertEqual(response.status_code, 200)
        received = [parse(chunk) for chunk in response.streaming_content]
        self.assertEqual(
            received,
            [{"type": "snapshot", "text": "So far", "offset": 6}, {"type": "unavailable"}],
        )
        self.assertEqual(self.subscribed, [])


class AccessTests(StreamTestCase):
    async def assert_problem(self, response, status, code):
        self.assertEqual(response.status_code, status)
        self.assertFalse(response.streaming)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["code"], code)
        self.assertEqual(self.subscribed, [])

    async def test_another_users_ask_is_a_404_and_no_stream(self):
        bob = await sync_to_async(lambda: bearer(make_user("bob")))()
        response = await self.async_client.get(url(self.ask.pk), headers=bob)
        await self.assert_problem(response, 404, "not_found")

    async def test_a_missing_ask_is_a_404(self):
        response = await self.async_client.get(url(self.ask.pk + 1000), headers=self.auth)
        await self.assert_problem(response, 404, "not_found")

    async def test_a_deleted_conversations_turn_is_a_404(self):
        conversation = await Conversation.objects.acreate(
            user=self.alice, deleted_at=timezone.now()
        )
        await self.aset_row(conversation=conversation, position=1)
        response = await self.async_client.get(url(self.ask.pk), headers=self.auth)
        await self.assert_problem(response, 404, "not_found")

    async def test_errors_are_json_even_when_the_client_accepts_only_events(self):
        response = await self.async_client.get(
            url(self.ask.pk + 1000), headers={**self.auth, "Accept": "text/event-stream"}
        )
        await self.assert_problem(response, 404, "not_found")

    async def test_unauthenticated_is_a_401(self):
        response = await self.async_client.get(url(self.ask.pk))
        await self.assert_problem(response, 401, "not_authenticated")

    async def test_a_bad_token_is_a_401(self):
        response = await self.async_client.get(
            url(self.ask.pk), headers={"Authorization": "Bearer not-a-token"}
        )
        await self.assert_problem(response, 401, "token_not_valid")

    @override_settings(CORS_ALLOWED_ORIGINS=["http://localhost:5173"])
    async def test_cors_headers_as_on_the_rest_of_the_api(self):
        await self.aset_row(status="done", answer="Done.")
        response, body = await self.open(Origin="http://localhost:5173")
        self.assertEqual(response["Access-Control-Allow-Origin"], "http://localhost:5173")
        await self.next_event(body)

    @override_settings(
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "ask-stream-tests",
            }
        }
    )
    async def test_the_general_user_rate_applies(self):
        self.assertIsNone(getattr(AskStreamView, "throttle_scope", None))
        await self.aset_row(status="done", answer="Done.")
        with mock.patch.dict(UserRateThrottle.THROTTLE_RATES, {"user": "1/hour"}):
            response, body = await self.open()
            await self.next_event(body)
            self.subscribed.clear()
            response = await self.async_client.get(url(self.ask.pk), headers=self.auth)
        await self.assert_problem(response, 429, "throttled")


# --- Through Django's ASGI handler, as uvicorn runs it --------------------


async def call_asgi(path, headers, receive_after_start):
    """Run one GET through ASGIHandler; returns the messages it sent.

    ``receive_after_start`` is awaited for the receive() call that follows
    the request body, which is how the handler learns of a disconnect.
    """
    sent = []
    started = asyncio.Event()
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return await receive_after_start(started, sent)

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            started.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    await ASGIHandler()(scope, receive, send)
    return sent


def body_events(sent):
    return [parse(m["body"]) for m in sent if m["type"] == "http.response.body" and m.get("body")]


@override_settings(**QUIET, ALLOWED_HOSTS=["testserver"])
class ASGIDisconnectTests(TransactionTestCase):
    """A client that goes away: the server cancels the body, which closes the subscription.

    TransactionTestCase: the handler runs the view in a thread of its own,
    with its own database connection, which must see committed rows.
    """

    def test_a_disconnect_closes_the_subscription(self):
        alice = make_user("alice")
        ask = AskQuery.objects.create(
            user=alice, question="q", idempotency_key="k", status="running"
        )
        auth = bearer(alice)
        subscription = FakeSubscription()

        async def subscribe(ask_id):
            return subscription

        async def disconnect_once_streaming(started, sent):
            await asyncio.wait_for(started.wait(), READ_TIMEOUT)
            return {"type": "http.disconnect"}

        async def scenario():
            with mock.patch("assistant.stream.subscribe", subscribe):
                return await asyncio.wait_for(
                    call_asgi(url(ask.pk), auth, disconnect_once_streaming), 5
                )

        sent = asyncio.run(scenario())
        self.assertEqual(sent[0]["status"], 200)
        self.assertEqual(body_events(sent), [{"type": "snapshot", "text": "", "offset": 0}])
        self.assertTrue(subscription.closed)


@override_settings(**QUIET, ALLOWED_HOSTS=["testserver"])
class RealRedisTests(TransactionTestCase):
    """The worker's publisher and the stream through the local Redis, database 15."""

    URL = "redis://localhost:6379/15"

    @classmethod
    def setUpClass(cls):
        try:
            redis.Redis.from_url(cls.URL, socket_connect_timeout=0.2).ping()
        except redis.RedisError:
            raise unittest.SkipTest("no Redis at localhost:6379") from None
        super().setUpClass()

    def test_deltas_and_done_from_the_publisher_reach_the_stream(self):
        alice = make_user("alice")
        # Pub/sub channels are shared by every database number and process:
        # an id no other test run is likely to be publishing on.
        ask = AskQuery.objects.create(
            pk=random.randint(10**9, 2 * 10**9),
            user=alice,
            question="q",
            idempotency_key="k",
            status="running",
            partial_answer="Your ",
        )
        auth = bearer(alice)
        publisher = events.Publisher(ask.pk, client=redis.Redis.from_url(self.URL))

        async def publish_while_streaming(started, sent):
            await asyncio.wait_for(started.wait(), READ_TIMEOUT)  # snapshot sent: subscribed
            await sync_to_async(publisher.delta)("Your passport ", offset=0)
            await sync_to_async(publisher.delta)("expires.", offset=14)
            await AskQuery.objects.filter(pk=ask.pk).aupdate(
                status="done", answer="Your passport expires.", partial_answer=""
            )
            await sync_to_async(publisher.outcome)()
            await asyncio.Event().wait()  # never disconnects

        async def scenario():
            with override_settings(ASK_EVENTS_REDIS_URL=self.URL):
                return await asyncio.wait_for(
                    call_asgi(url(ask.pk), auth, publish_while_streaming), 5
                )

        received = body_events(asyncio.run(scenario()))
        self.assertEqual(
            received[:3],
            [
                {"type": "snapshot", "text": "Your ", "offset": 5},
                {"type": "delta", "offset": 5, "text": "passport "},
                {"type": "delta", "offset": 14, "text": "expires."},
            ],
        )
        self.assertEqual(received[3]["type"], "done")
        self.assertEqual(received[3]["ask"]["answer"], "Your passport expires.")
        self.assertEqual(len(received), 4)
