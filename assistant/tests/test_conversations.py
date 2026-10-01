"""Conversations: create_turn's rules, and the HTTP around them.

A turn is an ask (DECISIONS D140): the quota, idempotency and the task are
the ones test_services.py and test_api.py cover. This covers what a
conversation adds -- positions, sequential turns, titles, soft delete,
isolation -- and that the shared rules hold for turns too. Turns racing
each other are in test_concurrency.py.
"""

import uuid
from datetime import timedelta
from unittest import mock

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from assistant.api import ConversationListView, TurnCreateView
from assistant.models import AskQuery, Conversation
from assistant.services import (
    ConversationNotFound,
    IdempotencyKeyReused,
    QuotaExceeded,
    TurnInProgress,
    create_ask,
    create_conversation,
    create_turn,
    delete_conversation,
    derive_title,
    rename_conversation,
)
from assistant.tasks import answer_ask
from limits.models import Limit, UsageEvent
from limits.service import SystemLimitExceeded
from notes import services as notes
from notes.tests.helpers import doc, make_user
from retrieval.indexing import index_note

from .helpers import chat_turns

CONVERSATIONS = "/api/v1/conversations/"
ASK = "/api/v1/ask/"


def conversation_url(conversation_or_id):
    return f"{CONVERSATIONS}{getattr(conversation_or_id, 'pk', conversation_or_id)}/"


def turns_url(conversation_or_id):
    return f"{conversation_url(conversation_or_id)}turns/"


def finish(turn, status=AskQuery.Status.DONE):
    AskQuery.objects.filter(pk=turn.pk).update(status=status)


class NoTaskMixin:
    """Creating turns, not answering them: the task is another test's job."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(answer_ask, "delay")
        self.delay = patcher.start()
        self.addCleanup(patcher.stop)


# --- Services -------------------------------------------------------------


@override_settings(LIMIT_DEFAULTS=chat_turns(10, 10))
class CreateTurnTests(NoTaskMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.alice = make_user("alice")
        self.conversation = Conversation.objects.create(user=self.alice)

    def test_turns_are_numbered_from_one_and_enqueued_on_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            first, created = create_turn(self.alice, self.conversation, "Where is Goa?", "k1")
        finish(first)
        with self.captureOnCommitCallbacks(execute=True):
            second, _ = create_turn(self.alice, self.conversation.pk, "And Kochi?", "k2")

        self.assertTrue(created)
        self.assertEqual((first.position, second.position), (1, 2))
        self.assertEqual(first.conversation, self.conversation)
        self.assertEqual(self.delay.call_args_list, [mock.call(first.pk), mock.call(second.pk)])

    def test_a_turn_consumes_one_chat_turn_linked_to_it(self):
        turn, _ = create_turn(self.alice, self.conversation, "Where is Goa?", "k1")

        event = UsageEvent.objects.get()
        self.assertEqual((event.user, event.key, event.ask), (self.alice, "chat_turns", turn))

    def test_a_turn_is_refused_while_the_previous_one_is_unfinished(self):
        first, _ = create_turn(self.alice, self.conversation, "Where is Goa?", "k1")

        for status in (AskQuery.Status.PENDING, AskQuery.Status.RUNNING):
            with self.subTest(status=status):
                finish(first, status)
                with self.assertRaises(TurnInProgress) as caught:
                    create_turn(self.alice, self.conversation, "And Kochi?", "k2")
                self.assertEqual(caught.exception.turn, first)

        self.assertEqual(AskQuery.objects.count(), 1)
        self.assertEqual(UsageEvent.objects.count(), 1)

    def age(self, turn, seconds):
        AskQuery.objects.filter(pk=turn.pk).update(
            created_at=timezone.now() - timedelta(seconds=seconds)
        )

    @override_settings(TURN_PENDING_STALE_SECONDS=120)
    def test_a_lost_pending_turn_is_failed_refunded_and_the_next_proceeds(self):
        # D505: its message never reached a worker; the user is not blocked for an hour.
        first, _ = create_turn(self.alice, self.conversation, "Where is Goa?", "k1")
        self.age(first, 121)

        second, created = create_turn(self.alice, self.conversation, "And Kochi?", "k2")

        self.assertTrue(created)
        self.assertEqual(second.position, 2)
        first.refresh_from_db()
        self.assertEqual(first.status, AskQuery.Status.FAILED)
        self.assertTrue(first.error)
        self.assertIsNotNone(first.completed_at)
        self.assertTrue(UsageEvent.objects.get(ask=first).refunded)
        self.assertFalse(UsageEvent.objects.get(ask=second).refunded)

    @override_settings(TURN_PENDING_STALE_SECONDS=120)
    def test_a_recent_pending_turn_and_any_running_turn_still_block(self):
        first, _ = create_turn(self.alice, self.conversation, "Where is Goa?", "k1")
        for status, seconds in ((AskQuery.Status.PENDING, 60), (AskQuery.Status.RUNNING, 3000)):
            with self.subTest(status=status, seconds=seconds):
                finish(first, status)
                self.age(first, seconds)
                with self.assertRaises(TurnInProgress):
                    create_turn(self.alice, self.conversation, "And Kochi?", "k2")
                first.refresh_from_db()
                self.assertEqual(first.status, status)
        self.assertFalse(UsageEvent.objects.get(ask=first).refunded)

    @override_settings(TURN_PENDING_STALE_SECONDS=120)
    def test_a_worker_that_comes_late_leaves_the_failed_turn_alone(self):
        first, _ = create_turn(self.alice, self.conversation, "Where is Goa?", "k1")
        self.age(first, 300)
        create_turn(self.alice, self.conversation, "And Kochi?", "k2")

        with mock.patch("assistant.tasks.search") as search:
            answer_ask.run(first.pk)

        search.assert_not_called()
        first.refresh_from_db()
        self.assertEqual(first.status, AskQuery.Status.FAILED)

    def test_a_failed_turn_lets_the_next_one_through(self):
        first, _ = create_turn(self.alice, self.conversation, "Where is Goa?", "k1")
        finish(first, AskQuery.Status.FAILED)

        second, created = create_turn(self.alice, self.conversation, "Where is Goa, again?", "k2")

        self.assertTrue(created)
        self.assertEqual(second.position, 2)

    def test_other_conversations_and_plain_asks_do_not_block(self):
        create_turn(self.alice, self.conversation, "Where is Goa?", "k1")
        create_ask(self.alice, "A plain ask?", "k2")
        other = Conversation.objects.create(user=self.alice)

        turn, created = create_turn(self.alice, other, "Somewhere else?", "k3")

        self.assertTrue(created)
        self.assertEqual(turn.position, 1)

    def test_a_replay_returns_the_turn_even_while_it_runs(self):
        """A retry of the running turn is that turn, not a 409, and counts once."""
        first, _ = create_turn(self.alice, self.conversation, "Where is Goa?", "k1")
        finish(first, AskQuery.Status.RUNNING)

        again, created = create_turn(self.alice, self.conversation, "  Where is Goa?  ", "k1")

        self.assertEqual((again, created), (first, False))
        self.assertEqual(UsageEvent.objects.count(), 1)

    def test_a_key_is_one_request_in_one_place(self):
        """Replays match the question and where it was asked (DECISIONS D142)."""
        turn, _ = create_turn(self.alice, self.conversation, "Where is Goa?", "turn-key")
        finish(turn)
        create_ask(self.alice, "Where is Goa?", "ask-key")
        other = Conversation.objects.create(user=self.alice)

        goa = "Where is Goa?"
        cases = [
            ("another question", create_turn, (self.conversation, "No?", "turn-key")),
            ("another conversation", create_turn, (other, goa, "turn-key")),
            ("a turn's key as an ask", create_ask, (goa, "turn-key")),
            ("an ask's key as a turn", create_turn, (other, goa, "ask-key")),
        ]
        for name, create, args in cases:
            with self.subTest(name), self.assertRaises(IdempotencyKeyReused):
                create(self.alice, *args)
        self.assertEqual(AskQuery.objects.count(), 2)

    def test_another_users_or_a_deleted_conversation_is_not_found(self):
        bob = make_user("bob")
        deleted = Conversation.objects.create(user=self.alice)
        delete_conversation(self.alice, deleted.pk)

        for conversation in (self.conversation, deleted):
            with self.subTest(conversation=conversation.pk):
                user = bob if conversation == self.conversation else self.alice
                with self.assertRaises(ConversationNotFound):
                    create_turn(user, conversation, "Mine?", f"k{conversation.pk}")
        self.assertFalse(AskQuery.objects.exists())

    def test_the_first_turn_names_the_conversation_and_every_turn_moves_it_up(self):
        before = self.conversation.updated_at
        first, _ = create_turn(self.alice, self.conversation, "Where is Goa?", "k1")
        finish(first)
        create_turn(self.alice, self.conversation, "And Kochi?", "k2")

        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.title, "Where is Goa?")
        self.assertGreater(self.conversation.updated_at, before)

    def test_a_title_given_before_the_first_turn_is_kept(self):
        rename_conversation(self.alice, self.conversation.pk, "Trips")
        create_turn(self.alice, self.conversation, "Where is Goa?", "k1")

        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.title, "Trips")

    def test_a_rename_does_not_move_the_conversation(self):
        before = self.conversation.updated_at
        renamed = rename_conversation(self.alice, self.conversation.pk, "  Trips ")

        self.conversation.refresh_from_db()
        self.assertEqual((renamed.title, self.conversation.title), ("Trips", "Trips"))
        self.assertEqual(self.conversation.updated_at, before)


@override_settings(LIMIT_DEFAULTS=chat_turns(2, 10))
class TurnQuotaTests(NoTaskMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.alice = make_user("alice")
        self.conversation = Conversation.objects.create(user=self.alice)

    def test_turns_and_asks_share_the_months_quota(self):
        create_ask(self.alice, "One?", "a")
        create_turn(self.alice, self.conversation, "Two?", "b")
        finish(AskQuery.objects.get(idempotency_key="b"))

        with self.assertRaises(QuotaExceeded) as caught:
            create_turn(self.alice, self.conversation, "Three?", "c")

        self.assertEqual((caught.exception.used, caught.exception.limit), (2, 2))
        self.assertEqual(AskQuery.objects.count(), 2)

    def test_a_refused_turn_leaves_the_conversation_as_it_was(self):
        create_ask(self.alice, "One?", "a")
        create_ask(self.alice, "Two?", "b")
        before = Conversation.objects.get().updated_at

        with self.assertRaises(QuotaExceeded):
            create_turn(self.alice, self.conversation, "Three?", "c")

        self.conversation.refresh_from_db()
        self.assertEqual((self.conversation.title, self.conversation.updated_at), ("", before))

    def test_the_system_limit_passes_through_and_leaves_nothing(self):
        Limit.objects.create(
            key="chat_turns", user_free=5, user_premium=5, system=1, period="month"
        )
        create_ask(make_user("bob"), "Bob's?", "a")

        with self.assertLogs("limits.service", "WARNING"), self.assertRaises(SystemLimitExceeded):
            create_turn(self.alice, self.conversation, "Mine?", "b")
        self.assertFalse(AskQuery.objects.filter(user=self.alice).exists())

    def test_deleting_a_conversation_keeps_its_turns_counted(self):
        create_turn(self.alice, self.conversation, "One?", "a")
        delete_conversation(self.alice, self.conversation.pk)

        self.assertEqual(UsageEvent.objects.filter(refunded=False).count(), 1)
        create_ask(self.alice, "Two?", "b")
        with self.assertRaises(QuotaExceeded):
            create_ask(self.alice, "Three?", "c")


@override_settings(LIMIT_DEFAULTS=chat_turns(10, 10))
class CreateConversationTests(NoTaskMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.alice = make_user("alice")

    def test_empty_conversation_costs_nothing(self):
        conversation, created = create_conversation(self.alice)

        self.assertTrue(created)
        self.assertEqual(conversation.title, "")
        self.assertFalse(AskQuery.objects.exists())
        self.assertFalse(UsageEvent.objects.exists())

    def test_with_a_question_it_asks_turn_one(self):
        conversation, created = create_conversation(self.alice, " Where is Goa? ", "k1")

        turn = AskQuery.objects.get()
        self.assertTrue(created)
        self.assertEqual((turn.conversation, turn.position), (conversation, 1))
        self.assertEqual((turn.question, conversation.title), ("Where is Goa?", "Where is Goa?"))

    def test_a_replay_returns_the_same_conversation_and_counts_once(self):
        first, _ = create_conversation(self.alice, "Where is Goa?", "k1")
        again, created = create_conversation(self.alice, "Where is Goa?", "k1")

        self.assertEqual((again, created), (first, False))
        self.assertEqual((Conversation.objects.count(), UsageEvent.objects.count()), (1, 1))

    def test_a_key_that_made_something_else_is_refused(self):
        create_ask(self.alice, "Where is Goa?", "ask-key")
        conversation, _ = create_conversation(self.alice, "Where is Goa?", "first")
        finish(AskQuery.objects.get(idempotency_key="first"))
        create_turn(self.alice, conversation, "And Kochi?", "second")
        gone, _ = create_conversation(self.alice, "Gone?", "gone")
        delete_conversation(self.alice, gone.pk)

        cases = [
            ("a plain ask", "Where is Goa?", "ask-key"),
            ("another question", "Somewhere else?", "first"),
            ("a later turn", "And Kochi?", "second"),
            ("a deleted conversation", "Gone?", "gone"),
        ]
        for name, question, key in cases:
            with self.subTest(name), self.assertRaises(IdempotencyKeyReused):
                create_conversation(self.alice, question, key)
        self.assertEqual(Conversation.objects.count(), 2)

    def test_a_refused_first_turn_leaves_no_conversation(self):
        with override_settings(LIMIT_DEFAULTS=chat_turns(0, 0)), self.assertRaises(QuotaExceeded):
            create_conversation(self.alice, "Where is Goa?", "k1")
        self.assertFalse(Conversation.objects.exists())


class DeriveTitleTests(TestCase):
    def test_short_questions_are_kept_on_one_line(self):
        self.assertEqual(derive_title("Where\n is  Goa?"), "Where is Goa?")

    def test_long_questions_are_cut_at_a_word(self):
        title = derive_title("word " * 40)
        self.assertTrue(title.endswith("word…"), title)
        self.assertLessEqual(len(title), 81)

    def test_one_long_word_is_cut_anyway(self):
        self.assertEqual(len(derive_title("x" * 300)), 81)


class ConstraintTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.conversation = Conversation.objects.create(user=self.alice)

    def make(self, key, **fields):
        return AskQuery.objects.create(user=self.alice, question="q", idempotency_key=key, **fields)

    def test_one_turn_per_position(self):
        self.make("a", conversation=self.conversation, position=1)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.make("b", conversation=self.conversation, position=1)

    def test_a_turn_has_both_a_conversation_and_a_position_or_neither(self):
        for name, fields in (
            ("position only", {"position": 1}),
            ("conversation only", {"conversation": self.conversation}),
        ):
            with self.subTest(name), transaction.atomic(), self.assertRaises(IntegrityError):
                self.make(name, **fields)
        self.make("plain")  # Neither: a plain ask.


# --- API ------------------------------------------------------------------


class ConversationAPITestCase(NoTaskMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.alice = make_user("alice")
        self.client = APIClient()
        self.client.force_authenticate(self.alice)

    def post_turn(self, conversation, question="Where is Goa?", key=None):
        key = str(uuid.uuid4()) if key is None else key
        headers = {"Idempotency-Key": key} if key != "" else {}
        return self.client.post(
            turns_url(conversation), {"question": question}, format="json", headers=headers
        )

    def start(self, question=None, key=None):
        body = {} if question is None else {"question": question}
        headers = {"Idempotency-Key": key} if key else {}
        return self.client.post(CONVERSATIONS, body, format="json", headers=headers)


@override_settings(LIMIT_DEFAULTS=chat_turns(10, 10))
class ConversationEndpointTests(ConversationAPITestCase):
    def test_every_endpoint_needs_a_user(self):
        anonymous = APIClient()
        conversation = Conversation.objects.create(user=self.alice)
        headers = {"Idempotency-Key": "k"}
        for response in (
            anonymous.get(CONVERSATIONS),
            anonymous.post(CONVERSATIONS, {}, format="json"),
            anonymous.get(conversation_url(conversation)),
            anonymous.patch(conversation_url(conversation), {"title": "x"}, format="json"),
            anonymous.delete(conversation_url(conversation)),
            anonymous.post(turns_url(conversation), {"question": "x"}, headers=headers),
        ):
            self.assertEqual(response.status_code, 401)
        self.assertFalse(AskQuery.objects.exists())

    def test_another_users_conversation_is_a_404_everywhere(self):
        bob = make_user("bob")
        theirs = Conversation.objects.create(user=bob, title="Bob's")
        turn, _ = create_turn(bob, theirs, "Bob's question?", "k")

        for response in (
            self.client.get(conversation_url(theirs)),
            self.client.patch(conversation_url(theirs), {"title": "Mine"}, format="json"),
            self.client.delete(conversation_url(theirs)),
            self.post_turn(theirs),
            self.client.get(f"{ASK}{turn.pk}/"),
        ):
            self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get(CONVERSATIONS).json()["results"], [])
        theirs.refresh_from_db()
        self.assertEqual((theirs.title, theirs.deleted_at), ("Bob's", None))
        self.assertEqual(AskQuery.objects.count(), 1)

    def test_start_empty_then_ask_then_read_the_thread(self):
        started = self.start()
        self.assertEqual(started.status_code, 201)
        conversation = started.json()
        self.assertEqual((conversation["title"], conversation["turns"]), ("", []))

        first = self.post_turn(conversation["id"], "Where is Goa?")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()["position"], 1)
        self.assertEqual(first.json()["conversation"], conversation["id"])
        self.assertEqual(first.json()["status"], "pending")
        finish(AskQuery.objects.get(pk=first.json()["id"]))
        second = self.post_turn(conversation["id"], "And Kochi?")
        self.assertEqual(second.status_code, 202)

        thread = self.client.get(conversation_url(conversation["id"])).json()
        self.assertEqual(thread["title"], "Where is Goa?")
        self.assertEqual(
            [(t["position"], t["question"]) for t in thread["turns"]],
            [(1, "Where is Goa?"), (2, "And Kochi?")],
        )
        # A turn is polled where an ask is.
        polled = self.client.get(f"{ASK}{second.json()['id']}/")
        self.assertEqual(polled.json()["position"], 2)

    def test_start_with_a_question_asks_turn_one_and_replays(self):
        key = str(uuid.uuid4())
        started = self.start("Where is Goa?", key)
        again = self.start("Where is Goa?", key)

        self.assertEqual((started.status_code, again.status_code), (201, 200))
        self.assertEqual(again.json()["id"], started.json()["id"])
        self.assertEqual(
            [(t["position"], t["question"]) for t in started.json()["turns"]],
            [(1, "Where is Goa?")],
        )
        self.assertEqual(UsageEvent.objects.count(), 1)

    def test_start_with_a_question_needs_a_valid_key(self):
        for key, code in (("", "idempotency_key_required"), ("bad key", "idempotency_key_invalid")):
            with self.subTest(code=code):
                response = self.start("Where is Goa?", key or None)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], code)
        self.assertFalse(Conversation.objects.exists())

    def test_turn_needs_a_valid_key_and_question(self):
        conversation = Conversation.objects.create(user=self.alice)
        for kwargs, code in (
            ({"key": ""}, "idempotency_key_required"),
            ({"key": "semi;colon"}, "idempotency_key_invalid"),
            ({"question": "   "}, "invalid"),
            ({"question": "x" * 1001}, "invalid"),
        ):
            with self.subTest(code=code, **{k: v[:10] for k, v in kwargs.items()}):
                response = self.post_turn(conversation, **kwargs)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], code)
        self.assertFalse(AskQuery.objects.exists())

    def test_a_turn_while_the_previous_runs_is_a_409_naming_it(self):
        conversation = Conversation.objects.create(user=self.alice)
        key = str(uuid.uuid4())
        first = self.post_turn(conversation, key=key)

        busy = self.post_turn(conversation, "And Kochi?")
        replay = self.post_turn(conversation, key=key)

        self.assertEqual(busy.status_code, 409)
        self.assertEqual(busy.json()["code"], "turn_in_progress")
        self.assertEqual(busy.json()["turn"], first.json()["id"])
        self.assertEqual((replay.status_code, replay.json()["id"]), (200, first.json()["id"]))
        self.assertEqual(AskQuery.objects.count(), 1)

    def test_a_key_reused_elsewhere_is_a_422(self):
        conversation = Conversation.objects.create(user=self.alice)
        key = str(uuid.uuid4())
        self.client.post(ASK, {"question": "Where is Goa?"}, headers={"Idempotency-Key": key})

        response = self.post_turn(conversation, "Where is Goa?", key=key)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "idempotency_key_reused")

    def test_rename(self):
        conversation = Conversation.objects.create(user=self.alice, title="Old")

        response = self.client.patch(
            conversation_url(conversation), {"title": " Trips "}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "Trips")
        for title in ("", "   ", "x" * 201):
            with self.subTest(length=len(title)):
                response = self.client.patch(
                    conversation_url(conversation), {"title": title}, format="json"
                )
                self.assertEqual(response.status_code, 400)
        conversation.refresh_from_db()
        self.assertEqual(conversation.title, "Trips")

    def test_put_is_not_allowed(self):
        conversation = Conversation.objects.create(user=self.alice)
        response = self.client.put(conversation_url(conversation), {"title": "x"}, format="json")
        self.assertEqual(response.status_code, 405)

    def test_delete_hides_the_conversation_and_its_turns(self):
        conversation = Conversation.objects.create(user=self.alice)
        turn = self.post_turn(conversation).json()
        plain = self.client.post(
            ASK, {"question": "Plain?"}, headers={"Idempotency-Key": "plain"}
        ).json()

        self.assertEqual(self.client.delete(conversation_url(conversation)).status_code, 204)

        self.assertEqual(self.client.get(conversation_url(conversation)).status_code, 404)
        self.assertEqual(self.client.delete(conversation_url(conversation)).status_code, 404)
        self.assertEqual(self.post_turn(conversation).status_code, 404)
        self.assertEqual(self.client.get(CONVERSATIONS).json()["results"], [])
        self.assertEqual(self.client.get(f"{ASK}{turn['id']}/").status_code, 404)
        self.assertEqual([a["id"] for a in self.client.get(ASK).json()["results"]], [plain["id"]])
        # Soft: the rows and the usage stay.
        self.assertEqual(AskQuery.objects.count(), 2)
        self.assertEqual(UsageEvent.objects.filter(refunded=False).count(), 2)

    def test_live_turns_are_listed_with_plain_asks(self):
        conversation = Conversation.objects.create(user=self.alice)
        turn = self.post_turn(conversation).json()
        plain = self.client.post(
            ASK, {"question": "Plain?"}, headers={"Idempotency-Key": "plain"}
        ).json()

        listed = self.client.get(ASK).json()["results"]
        self.assertEqual([a["id"] for a in listed], [plain["id"], turn["id"]])

    def test_list_is_most_recently_active_first_and_paginated(self):
        older, newer, newest = (Conversation.objects.create(user=self.alice) for _ in range(3))
        self.post_turn(older)  # Now the most recent.

        first = self.client.get(CONVERSATIONS, {"page_size": 2}).json()
        self.assertEqual([c["id"] for c in first["results"]], [older.pk, newest.pk])
        rest = self.client.get(first["next"]).json()
        self.assertEqual([c["id"] for c in rest["results"]], [newer.pk])
        self.assertEqual(set(first["results"][0]), {"id", "title", "created_at", "updated_at"})

    def test_writes_use_the_ask_throttle_scope_and_reads_do_not(self):
        view = ConversationListView()
        view.request = mock.Mock(method="POST")
        self.assertEqual(view.throttle_scope, "ask")
        view.request = mock.Mock(method="GET")
        self.assertIsNone(view.throttle_scope)
        self.assertEqual(TurnCreateView.throttle_scope, "ask")


@override_settings(LIMIT_DEFAULTS=chat_turns(1, 1))
class ConversationLimitTests(ConversationAPITestCase):
    def test_over_quota_is_a_429_with_usage(self):
        conversation = Conversation.objects.create(user=self.alice)
        finish(AskQuery.objects.get(pk=self.post_turn(conversation).json()["id"]))

        response = self.post_turn(conversation, "And Kochi?")

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "quota_exceeded")
        self.assertEqual((response.json()["used"], response.json()["limit"]), (1, 1))
        started = self.start("Another?", "k")
        self.assertEqual(started.status_code, 429)
        self.assertEqual(Conversation.objects.count(), 1)

    def test_system_limit_is_a_503(self):
        Limit.objects.create(
            key="chat_turns", user_free=5, user_premium=5, system=0, period="month"
        )
        conversation = Conversation.objects.create(user=self.alice)

        with self.assertLogs("limits.service", "WARNING"):
            response = self.post_turn(conversation)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "system_limit_reached")
        self.assertFalse(AskQuery.objects.exists())


@override_settings(LIMIT_DEFAULTS=chat_turns(10, 10))
class TurnAnsweredTests(TestCase):
    """A turn is answered through the API (condensing and history: test_turns.py)."""

    def setUp(self):
        self.alice = make_user("alice")
        self.client = APIClient()
        self.client.force_authenticate(self.alice)
        passport = notes.create_note(
            self.alice, title="Passport", content=doc("My passport expires in March 2027.")
        )
        index_note(passport.pk, passport.version)
        self.passport = passport

    def test_a_turn_is_answered_and_cited(self):
        with self.captureOnCommitCallbacks(execute=True):
            started = self.client.post(
                CONVERSATIONS,
                {"question": "When does my passport expire?"},
                format="json",
                headers={"Idempotency-Key": "k1"},
            )
        turn_id = started.json()["turns"][0]["id"]

        polled = self.client.get(f"{ASK}{turn_id}/").json()
        self.assertEqual(polled["status"], "done")
        self.assertIn("March 2027", polled["answer"])
        self.assertEqual(polled["citations"][0]["note_id"], self.passport.pk)

        with self.captureOnCommitCallbacks(execute=True):
            follow_up = self.client.post(
                turns_url(started.json()["id"]),
                {"question": "When does my passport expire, exactly?"},
                format="json",
                headers={"Idempotency-Key": "k2"},
            )
        self.assertEqual(follow_up.status_code, 202)
        self.assertEqual(
            self.client.get(f"{ASK}{follow_up.json()['id']}/").json()["status"], "done"
        )
