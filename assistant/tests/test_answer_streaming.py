"""Streaming an answer: the chat.stream boundary, the fake's stream, and the task's events.

The task tests give the publisher a recording client (events.get_client
patched), so they need no Redis; one test at the end uses the real local
Redis on database 15 and is skipped when there is none. Celery runs
eagerly, so the end events -- published on commit -- are seen through
captureOnCommitCallbacks.
"""

import json
import unittest
from unittest import mock

import redis
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.test import SimpleTestCase, TestCase, override_settings

from assistant import chat, events, quota, tasks
from assistant.api import AskQuerySerializer
from assistant.chat import ChatError, ChatResult, TransientChatError
from assistant.chat.providers.base import ChatProvider, StreamingChatProvider
from assistant.chat.providers.fake import FakeProvider, words
from assistant.chat.registry import PROVIDERS, get_provider
from assistant.models import AskQuery, Conversation
from assistant.prompt import Excerpt, build_messages
from assistant.tasks import answer_ask
from limits.models import UsageEvent
from notes import services
from notes.tests.helpers import doc, make_user
from retrieval.indexing import index_note

from .helpers import record_usage

COMPLETE = "assistant.chat.complete"
STREAM = "assistant.chat.stream"


class PlainProvider:
    """A provider with no ``stream``: the boundary falls back to complete()."""

    name = "plain"

    def complete(self, system, user, model, max_output_tokens):
        return ChatResult(text="The whole answer [1].", provider="plain", model=model)


class StreamBoundaryTests(SimpleTestCase):
    def test_dispatches_to_the_providers_stream_with_its_model_and_ceiling(self):
        result = ChatResult(text="ab", provider="claude", model="m")
        with (
            override_settings(CHAT_PROVIDER="claude", CHAT_MAX_OUTPUT_TOKENS=123),
            mock.patch(
                "assistant.chat.providers.claude.ClaudeProvider.stream",
                return_value=iter(["a", "", "b", result]),
            ) as native,
        ):
            self.assertEqual(list(chat.stream("sys", "user")), ["a", "b", result])
        native.assert_called_once_with("sys", "user", settings.CHAT_MODELS["claude"], 123)

    def test_a_provider_without_stream_yields_its_complete_answer_as_one_delta(self):
        with (
            mock.patch.dict(PROVIDERS, {"plain": f"{__name__}.PlainProvider"}),
            override_settings(CHAT_PROVIDER="plain"),
        ):
            items = list(chat.stream("sys", "user", max_output_tokens=7))
        self.assertEqual(items[0], "The whole answer [1].")
        self.assertEqual(
            items[1], ChatResult(text="The whole answer [1].", provider="plain", model="")
        )
        self.assertEqual(len(items), 2)

    def test_a_stream_that_ends_without_a_result_is_a_chat_error(self):
        with (
            mock.patch.object(FakeProvider, "stream", return_value=iter(["half"])),
            self.assertRaisesMessage(ChatError, "without a result"),
        ):
            list(chat.stream("sys", "user"))

    def test_closing_the_boundary_closes_the_providers_stream(self):
        closed = []

        def native(*args):
            try:
                yield "one "
                yield "two"
            finally:
                closed.append(True)

        with mock.patch.object(FakeProvider, "stream", side_effect=native):
            items = chat.stream("sys", "user")
            self.assertEqual(next(items), "one ")
            items.close()
        self.assertEqual(closed, [True])

    def test_which_providers_stream(self):
        for name in ("fake", "claude", "openai", "gemini"):
            with self.subTest(name=name):
                self.assertIsInstance(get_provider(name), StreamingChatProvider)
        self.assertIsInstance(PlainProvider(), ChatProvider)
        self.assertNotIsInstance(PlainProvider(), StreamingChatProvider)


def excerpt(n, text):
    return Excerpt(n=n, note_id=10 * n, chunk_id=n, title=f"Note {n}", heading_path="", text=text)


class FakeStreamTests(SimpleTestCase):
    def messages(self):
        return build_messages(
            "When does it expire?",
            [excerpt(1, "My passport expires in March 2027. More."), excerpt(2, "Renew early.")],
        )

    def test_streams_its_complete_answer_word_by_word(self):
        system, user = self.messages()
        expected = FakeProvider().complete(system, user)

        items = list(FakeProvider().stream(system, user))

        *deltas, result = items
        self.assertEqual(result, expected)
        self.assertEqual("".join(deltas), expected.text)
        self.assertEqual(deltas, words(expected.text))
        self.assertEqual(deltas[:3], ["My ", "passport ", "expires "])
        self.assertEqual(list(FakeProvider().stream(system, user)), items)  # deterministic

    def test_words_join_back_exactly(self):
        for text in ("", "one", "  lead and  double  spaces\nand lines ", "end."):
            with self.subTest(text=text):
                self.assertEqual("".join(words(text)), text)

    def test_a_script_for_chat_complete_scripts_the_stream_too(self):
        scripted = ChatResult(text="Scripted answer here.", provider="fake", model="fake")
        with mock.patch(COMPLETE, return_value=scripted) as complete:
            items = list(chat.stream("sys", "user"))
        self.assertEqual(items, ["Scripted ", "answer ", "here.", scripted])
        self.assertEqual(complete.call_args.args, ("sys", "user"))

        with (
            mock.patch(COMPLETE, side_effect=TransientChatError("503")),
            self.assertRaises(TransientChatError),
        ):
            list(chat.stream("sys", "user"))


class Recorder:
    """A Redis stand-in that keeps what was published; or fails every publish."""

    def __init__(self, fail=None):
        self.messages = []
        self.calls = 0
        self.fail = fail

    def publish(self, channel, message):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        self.messages.append((channel, json.loads(message)))
        return 1

    def events(self):
        return [message for _, message in self.messages]

    def types(self):
        return [event["type"] for event in self.events()]


class PublisherTests(SimpleTestCase):
    def test_messages_are_compact_json_numbered_from_one_on_the_asks_channel(self):
        client = mock.Mock()
        publisher = events.Publisher(42, client=client)
        publisher.delta("Hé ", offset=0)
        publisher.reset()

        self.assertEqual(
            client.publish.call_args_list,
            [
                mock.call("ask:42", '{"seq":1,"type":"delta","offset":0,"text":"Hé "}'),
                mock.call("ask:42", '{"seq":2,"type":"reset"}'),
            ],
        )

    def test_no_client_publishes_nothing(self):
        publisher = events.Publisher(1, client=None)
        publisher.delta("x", offset=0)
        publisher.reset()
        publisher.outcome()
        self.assertEqual(publisher.seq, 0)

    def test_after_a_failure_deltas_are_skipped_but_their_numbers_used(self):
        client = Recorder(fail=redis.ConnectionError("down"))
        publisher = events.Publisher(1, client=client)
        with self.assertLogs("assistant.events", "WARNING") as logs:
            publisher.delta("a", offset=0)
            publisher.delta("b", offset=1)
            publisher.reset()  # still tried: Redis may be back
        self.assertEqual(client.calls, 2)
        self.assertEqual(publisher.seq, 3)
        self.assertEqual(len(logs.records), 1)

        client.fail = None
        publisher.delta("c", offset=0)
        self.assertEqual(client.calls, 2)  # the run's deltas stay off

    def test_the_client_comes_from_the_setting(self):
        with override_settings(ASK_EVENTS_REDIS_URL=""):
            self.assertIsNone(events.get_client())
        with override_settings(ASK_EVENTS_REDIS_URL="redis://localhost:6379/15"):
            client = events.get_client()
            self.assertIsInstance(client, redis.Redis)
            self.assertIs(events.get_client(), client)
        with (
            override_settings(ASK_EVENTS_REDIS_URL="not-a-url"),
            self.assertLogs("assistant.events", "ERROR"),
        ):
            self.assertIsNone(events.get_client())
        self.assertIsNone(events.Publisher(1).client)  # the test runner turns them off


def write(owner, title, body):
    note = services.create_note(owner, title=title, content=doc(body))
    index_note(note.pk, note.version)
    return note


def as_json(data):
    return json.loads(json.dumps(data, cls=DjangoJSONEncoder))


class AnswerEventTests(TestCase):
    QUESTION = "When does my passport expire?"

    def setUp(self):
        self.alice = make_user("alice")
        self.passport = write(
            self.alice, "Passport", "My passport expires in March 2027. Renew it at the office."
        )
        write(self.alice, "Launch plan", "The passport photo booth launch is Friday.")
        self.query = record_usage(
            AskQuery.objects.create(user=self.alice, question=self.QUESTION, idempotency_key="k")
        )
        self.recorder = Recorder()
        patcher = mock.patch.object(events, "get_client", return_value=self.recorder)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_task(self, **apply):
        with self.captureOnCommitCallbacks(execute=True):
            if apply:
                answer_ask.apply((self.query.pk,), **apply)
            else:
                answer_ask.delay(self.query.pk)
        self.query.refresh_from_db()
        return self.recorder.events()

    def assert_refunded(self, refunded):
        self.assertEqual(
            UsageEvent.objects.get(ask=self.query, key="chat_turns").refunded, refunded
        )
        self.assertEqual(quota.used(self.alice), 0 if refunded else 1)

    def test_deltas_are_published_in_order_then_done_with_what_polling_returns(self):
        published = self.run_task()

        self.assertEqual(self.query.status, "done")
        self.assertEqual(
            {channel for channel, _ in self.recorder.messages}, {f"ask:{self.query.pk}"}
        )
        self.assertEqual([event["seq"] for event in published], list(range(1, len(published) + 1)))
        *deltas, done = published
        self.assertGreater(len(deltas), 3)
        self.assertEqual({event["type"] for event in deltas}, {"delta"})
        # The fake streams word by word; joined, the deltas are the answer.
        self.assertEqual([event["text"] for event in deltas], words(self.query.answer))
        offsets = [len("".join(e["text"] for e in deltas[:i])) for i in range(len(deltas))]
        self.assertEqual([event["offset"] for event in deltas], offsets)

        self.assertEqual(done["type"], "done")
        self.assertEqual(done["ask"], as_json(AskQuerySerializer(self.query).data))
        self.assertEqual(done["ask"]["citations"][0]["note_id"], self.passport.pk)
        self.assertEqual(self.query.partial_answer, "")
        self.assert_refunded(False)

    def test_done_is_published_only_after_the_commit(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            answer_ask.delay(self.query.pk)
        self.assertNotIn("done", self.recorder.types())
        for callback in callbacks:
            callback()
        self.assertEqual(self.recorder.types()[-1], "done")

    def test_a_refusal_mid_stream_publishes_failed_and_refunds_as_before(self):
        def refused(system, user, max_output_tokens=None):
            yield "Your passport "
            yield "expires"
            raise ChatError("max_tokens")

        with mock.patch(STREAM, side_effect=refused), self.assertLogs("assistant.tasks"):
            published = self.run_task()

        self.assertEqual((self.query.status, self.query.error), ("failed", tasks.CHAT_FAILED))
        self.assertEqual(self.query.partial_answer, "")
        self.assertEqual([e["type"] for e in published], ["delta", "delta", "failed"])
        self.assertEqual(published[-1]["ask"]["status"], "failed")
        self.assertEqual(published[-1]["ask"]["error"], tasks.CHAT_FAILED)
        self.assert_refunded(True)

    def test_giving_up_after_the_last_retry_publishes_failed(self):
        with (
            mock.patch(COMPLETE, side_effect=TransientChatError("503")),
            self.assertLogs("assistant.tasks", "ERROR"),
        ):
            published = self.run_task(retries=answer_ask.max_retries)
        self.assertEqual((self.query.status, self.query.error), ("failed", tasks.BUSY))
        self.assertEqual([e["type"] for e in published], ["failed"])
        self.assert_refunded(True)

    def test_an_unexpected_error_publishes_failed(self):
        with (
            mock.patch(COMPLETE, side_effect=RuntimeError("bug")),
            self.assertLogs("assistant.tasks", "ERROR"),
            self.assertRaises(RuntimeError),
        ):
            self.run_task()
        self.query.refresh_from_db()
        self.assertEqual((self.query.status, self.query.error), ("failed", tasks.UNEXPECTED))
        self.assertEqual(self.recorder.types(), ["failed"])
        self.assert_refunded(True)

    def test_redis_down_never_fails_the_answer(self):
        self.recorder.fail = redis.ConnectionError("Connection refused")

        with self.assertLogs("assistant.events", "WARNING") as logs:
            self.run_task()

        self.assertEqual(self.query.status, "done")
        self.assertIn("[1]", self.query.answer)
        self.assert_refunded(False)
        # One try for the deltas (the rest are skipped), one for the end.
        self.assertEqual(self.recorder.calls, 2)
        self.assertEqual(len(logs.records), 1)

    def test_events_off_queue_nothing_on_commit(self):
        with (
            mock.patch.object(events, "get_client", return_value=None),
            self.captureOnCommitCallbacks(execute=False) as callbacks,
        ):
            answer_ask.delay(self.query.pk)
        self.assertEqual(callbacks, [])
        self.query.refresh_from_db()
        self.assertEqual(self.query.status, "done")

    @override_settings(ASK_PARTIAL_SAVE_SECONDS=0.5)
    def test_the_partial_answer_is_saved_at_most_every_half_second(self):
        clock = mock.Mock(now=0.0)
        seen = []

        def partial():
            return AskQuery.objects.values_list("partial_answer", flat=True).get(pk=self.query.pk)

        def timed(system, user, max_output_tokens=None):
            for now, piece in (
                (0.1, "One "),
                (0.3, "two "),
                (0.6, "three "),
                (0.9, "four "),
                (1.2, "five."),
            ):
                clock.now = now
                yield piece
                seen.append(partial())  # after the task handled this piece
            yield ChatResult(text="One two three four five.", provider="fake", model="fake")

        with (
            mock.patch(STREAM, side_effect=timed),
            mock.patch.object(tasks, "monotonic", side_effect=lambda: clock.now),
            mock.patch.object(tasks, "_save_partial", wraps=tasks._save_partial) as saves,
        ):
            self.run_task()

        self.assertEqual(
            seen, ["", "", "One two three ", "One two three ", "One two three four five."]
        )
        self.assertEqual(saves.call_count, 2)
        self.assertEqual(self.query.answer, "One two three four five.")
        self.assertEqual(self.query.partial_answer, "")  # done: answer has it

    def test_a_partial_save_never_touches_a_finished_ask(self):
        AskQuery.objects.filter(pk=self.query.pk).update(status="done", answer="final")
        tasks._save_partial(self.query.pk, "late")
        self.query.refresh_from_db()
        self.assertEqual((self.query.answer, self.query.partial_answer), ("final", ""))

    @override_settings(ASK_PARTIAL_SAVE_SECONDS=0)
    def test_a_transient_error_mid_stream_resets_and_the_retry_starts_over(self):
        attempts = []

        def flaky(system, user, max_output_tokens=None):
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                yield "Half an "
                yield "answer "
                raise TransientChatError("503 mid-stream")
            yield "In March "
            yield "2027 [1]."
            yield ChatResult(text="In March 2027 [1].", provider="fake", model="fake")

        with (
            mock.patch(STREAM, side_effect=flaky),
            mock.patch.object(tasks, "_save_partial", wraps=tasks._save_partial) as saves,
        ):
            published = self.run_task(throw=False)

        self.assertEqual(attempts, [1, 2])
        self.assertEqual((self.query.status, self.query.answer), ("done", "In March 2027 [1]."))
        self.assertEqual(self.query.citations[0]["note_id"], self.passport.pk)
        summary = [(e["seq"], e["type"], e.get("offset"), e.get("text")) for e in published]
        self.assertEqual(
            summary,
            [
                # The first attempt, taken back.
                (1, "delta", 0, "Half an "),
                (2, "delta", 8, "answer "),
                (3, "reset", None, None),
                # The retry: a new run, numbered from 1, starting over.
                (1, "reset", None, None),
                (2, "delta", 0, "In March "),
                (3, "delta", 9, "2027 [1]."),
                (4, "done", None, None),
            ],
        )
        self.assertEqual(
            [call.args[1] for call in saves.call_args_list],
            ["Half an ", "Half an answer ", "", "In March ", "In March 2027 [1]."],
        )
        self.assert_refunded(False)

    def test_taking_up_a_running_ask_resets_first(self):
        AskQuery.objects.filter(pk=self.query.pk).update(status="running", partial_answer="stale")
        published = self.run_task()
        self.assertEqual(published[0], {"seq": 1, "type": "reset"})
        self.assertEqual(published[-1]["type"], "done")
        self.assertNotIn("stale", self.query.answer)

    def test_a_floor_answer_is_done_without_deltas(self):
        with mock.patch.object(tasks, "search", return_value=[]):
            published = self.run_task()
        self.assertEqual([e["type"] for e in published], ["done"])
        self.assertEqual(published[0]["ask"]["answer"], settings.ASK_NO_ANSWER_TEXT)

    def test_a_conversation_turn_streams_its_answer_but_not_its_condense_call(self):
        conv = Conversation.objects.create(user=self.alice, title="Passport")
        AskQuery.objects.create(
            user=self.alice,
            conversation=conv,
            position=1,
            question="What do my notes say about my passport?",
            status="done",
            answer="It expires in March 2027.",
            idempotency_key="t1",
        )
        self.query = record_usage(
            AskQuery.objects.create(
                user=self.alice,
                conversation=conv,
                position=2,
                question="When does it expire?",
                idempotency_key="t2",
            )
        )

        with (
            mock.patch(STREAM, wraps=chat.stream) as streamed,
            mock.patch(COMPLETE, wraps=chat.complete) as completed,
        ):
            published = self.run_task()

        self.assertEqual(self.query.status, "done")
        self.assertTrue(self.query.standalone_question)  # condensed, with complete()
        self.assertEqual(streamed.call_count, 1)
        self.assertNotIn("<follow_up>", streamed.call_args.args[1])
        self.assertIn("<follow_up>", completed.call_args_list[0].args[1])
        self.assertEqual(published[-1]["type"], "done")
        self.assertEqual(published[-1]["ask"]["position"], 2)
        self.assertEqual(
            "".join(e["text"] for e in published if e["type"] == "delta"), self.query.answer
        )


class RealRedisTests(TestCase):
    """The same events through the local Redis, database 15. Skipped without one."""

    URL = "redis://localhost:6379/15"

    @classmethod
    def setUpClass(cls):
        try:
            redis.Redis.from_url(cls.URL, socket_connect_timeout=0.2).ping()
        except redis.RedisError:
            raise unittest.SkipTest("no Redis at localhost:6379") from None
        super().setUpClass()

    def test_a_subscriber_receives_the_deltas_and_done(self):
        alice = make_user("alice")
        write(alice, "Passport", "My passport expires in March 2027.")
        query = AskQuery.objects.create(
            user=alice, question="When does my passport expire?", idempotency_key="r"
        )
        subscriber = redis.Redis.from_url(self.URL).pubsub(ignore_subscribe_messages=True)
        self.addCleanup(subscriber.close)
        subscriber.subscribe(events.channel(query.pk))
        subscriber.get_message(timeout=1.0)  # the subscribe confirmation

        with (
            override_settings(ASK_EVENTS_REDIS_URL=self.URL),
            self.captureOnCommitCallbacks(execute=True),
        ):
            answer_ask.delay(query.pk)

        received = []
        while not received or received[-1]["type"] not in ("done", "failed"):
            message = subscriber.get_message(timeout=2.0)
            self.assertIsNotNone(message, f"no end event; got {received}")
            received.append(json.loads(message["data"]))

        query.refresh_from_db()
        self.assertEqual(received[-1]["type"], "done")
        self.assertEqual(received[-1]["ask"]["answer"], query.answer)
        self.assertEqual("".join(e["text"] for e in received[:-1]), query.answer)
