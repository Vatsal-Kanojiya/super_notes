"""User memory: facts learned from a finished conversation turn (plan Phase 3, DECISIONS D400-D409).

After a conversation turn ends ``done``, ``extract`` (run by the
``extract_memory`` task, queued on commit by the ask task) asks the chat
provider what the turn taught about the user:

1. **Off means off.** A user with ``memory_enabled`` off gets no call, no
   usage event and no write; it is checked again, under the user's row lock,
   before anything is written.
2. **The limit.** One system-only ``memory_extract`` use per turn, linked to
   the turn, user None (it costs the user nothing, DECISIONS D91): recorded
   in its own short transaction before the call, refunded when nothing was
   billed (the provider refusing or down, the question not embeddable). A
   turn is extracted at most once: a second run finds the first's event.
3. **What the call sees** (prompts/memory.md): the user's question and the
   user's live facts most similar to it, with their ids -- never the
   answer, never the note excerpts (the injection boundary, D400, D514).
   The answer quotes notes and attachments, which may hold text pasted from
   anywhere, so it is not sent at all.
4. **What comes back** is strict JSON, ``{"operations": [...]}`` of ``add``,
   ``update(id)``, ``supersede(id)`` and ``none``, checked by
   ``parse_operations``: an id must be one of the facts shown (so this
   user's, and live), a text must fit FACT_MAX_CHARS and must not look like
   a secret, at most MEMORY_MAX_OPERATIONS. Whatever fails is dropped and
   logged -- reasons only, never a fact's text -- and never retried.
5. **The write**, under the user's row lock: ``add`` makes a fact (unless a
   live one already says exactly that); ``update`` rewrites a fact's text
   and vector in place; ``supersede`` makes the new fact and points the old
   one's ``superseded_by`` at it. A target that stopped being live
   meanwhile (another extraction superseded it) is dropped. A dynamic fact
   gets ``valid_until``, MEMORY_DYNAMIC_FACT_DAYS ahead. A dynamic
   ``update`` or ``supersede`` of a static fact is written as an ``add``
   instead: something true for now never replaces, or expires, what stays
   true (D515).

``purge_expired`` (daily) deletes expired dynamic facts and facts
superseded more than MEMORY_SUPERSEDED_RETENTION_DAYS ago -- never a static
fact because its replacement expired (D516).

**Use** (DECISIONS D420-D421): ``facts_for_prompt`` gives a conversation
turn's chat prompt the user's live facts nearest its (standalone)
question, at most MEMORY_PROMPT_FACTS -- none, and no embedding call, with
memory off; none when the question cannot be embedded.

**Forgetting** (D422-D423): ``forget_all`` deletes every fact of the user
and, like switching memory off (``mark_reset``), moves
``User.memory_reset_at`` under the user's row lock. The write step only
writes facts from a turn created after that moment, so an extraction in
flight when the user said "forget everything" writes nothing.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import ExpressionWrapper, FloatField, Value
from django.utils import timezone
from pgvector.django import CosineDistance

from limits import service as limits
from limits.models import UsageEvent
from retrieval.embeddings import (
    EmbeddingError,
    EmbeddingTransientError,
    embed_query,
    embed_texts,
    embedding_model_id,
)

from . import chat
from .conversation import _block
from .models import FACT_MAX_CHARS, AskQuery, UserFact
from .prompt import load_prompt, neutralise

logger = logging.getLogger(__name__)

# The system-only limit key an extraction call consumes (DECISIONS D91).
MEMORY_KEY = "memory_extract"

ADD, UPDATE, SUPERSEDE, NONE = "add", "update", "supersede", "none"
OPS = frozenset({ADD, UPDATE, SUPERSEDE, NONE})

EMBEDDING_ERRORS = (EmbeddingError, EmbeddingTransientError)

# Never kept as a fact, whoever says it (D403): credentials, and long digit
# runs (card, account, phone and ID numbers). A date or a 6-digit PIN code
# (postal) is not a long enough run.
_SECRET = re.compile(
    r"\b(?:passwords?|passcodes?|passphrases?|pins?(?!\s*codes?\b)|otps?|one[- ]time code|cvv"
    r"|api[ _-]?keys?|secret keys?|private keys?|access tokens?|tokens?)\b"
    r"|(?:\d[ -]?){9,}",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Operation:
    """One checked operation. ``kind`` is "" on an update that keeps the fact's kind."""

    op: str
    text: str = ""
    kind: str = ""
    fact_id: int | None = None


def memory_prompt_version() -> str:
    """The version line of prompts/memory.md, e.g. "memory-v1"."""
    return load_prompt("memory")[0]


# --- The user's facts ----------------------------------------------------------


def known_facts(user, question: str) -> list[UserFact]:
    """The live facts an extraction call is shown, oldest first.

    All of them while there are at most MEMORY_SIMILAR_FACTS (no embedding
    call); beyond that, the ones nearest the question (D402). Raises
    EmbeddingError or EmbeddingTransientError when the question cannot be
    embedded.
    """
    k = settings.MEMORY_SIMILAR_FACTS
    facts = list(UserFact.objects.live(user).order_by("-pk")[: k + 1])
    if len(facts) <= k:
        return sorted(facts, key=lambda fact: fact.pk)
    return sorted(similar_facts(user, embed_query(question), k), key=lambda fact: fact.pk)


def similar_facts(user, vector: list[float], k: int) -> list[UserFact]:
    """The user's ``k`` live facts nearest ``vector``, by an exact scan of them (D517)."""
    return list(similar_facts_queryset(user, vector)[:k])


def similar_facts_queryset(user, vector: list[float]):
    """The similar-facts query, uncut; public so a test can read its SQL.

    Owner-scoped in SQL (``live``), and only facts embedded by the current
    model: another model's vectors live in another space.

    Ordered by ``distance + 0``, which the HNSW index cannot serve (it
    serves only ``ORDER BY embedding <=> ...`` itself), so the planner
    reads the user's facts by their owner index and sorts them exactly.
    The index would return its ``ef_search`` nearest facts of *every* user
    and filter after: a user with a few facts among many users' close ones
    would get none of them (D517). A user's live facts are few, so the
    exact sort is cheap.
    """
    distance = CosineDistance("embedding", vector)
    return (
        UserFact.objects.live(user)
        .filter(embedding_model=embedding_model_id())
        .annotate(distance=distance)
        .order_by(ExpressionWrapper(distance + Value(0.0), output_field=FloatField()))
    )


def facts_for_prompt(user, question: str) -> list[UserFact]:
    """The live facts a conversation turn's chat prompt carries (DECISIONS D420-D421).

    None -- and no query, no embedding call -- when the user has memory
    off. While the user has at most MEMORY_PROMPT_FACTS live facts, all of
    them, oldest first, with no embedding call; beyond that the nearest to
    ``question`` of the current embedding model, nearest first. A question
    that cannot be embedded gets none: memory can improve an answer, never
    fail one.
    """
    k = settings.MEMORY_PROMPT_FACTS
    if not user.memory_enabled or k <= 0:
        return []
    facts = list(UserFact.objects.live(user).order_by("-pk")[: k + 1])
    if len(facts) <= k:
        return sorted(facts, key=lambda fact: fact.pk)
    try:
        vector = embed_query(question)
    except EMBEDDING_ERRORS:
        logger.warning("The question could not be embedded; answering without memory")
        return []
    return similar_facts(user, vector, k)


# --- Forgetting --------------------------------------------------------------------


def mark_reset(user_id: int, now=None) -> None:
    """Move the user's ``memory_reset_at`` to now; the caller holds the user's row lock.

    From now on no extraction writes a fact learned from an earlier turn
    (``apply_operations``), so one in flight cannot undo a "forget
    everything" or outlive memory being switched off (DECISIONS D422).
    """
    get_user_model().objects.filter(pk=user_id).update(memory_reset_at=now or timezone.now())


def forget_all(user) -> int:
    """Delete every fact of the user's, superseded ones too; how many were deleted.

    The user's row is locked *first*: an extraction holding it (mid-write)
    commits before the delete runs, so the delete sees its facts; and the
    reset marker moves in the same commit, so an extraction still waiting
    for the lock writes nothing (DECISIONS D422).
    """
    with transaction.atomic():
        list(get_user_model().objects.select_for_update().filter(pk=user.pk).values_list("pk"))
        mark_reset(user.pk)
        deleted, _ = UserFact.objects.filter(user_id=user.pk).delete()
    return deleted


def delete_fact(user, fact_id: int) -> bool:
    """Delete one of the user's facts (any state); False when the user has no such fact.

    Owner-scoped in SQL, so another user's id deletes nothing. The facts it
    superseded go with it (D405).
    """
    deleted, _ = UserFact.objects.filter(user_id=user.pk, pk=fact_id).delete()
    return bool(deleted)


# --- The prompt ------------------------------------------------------------------


def facts_block(facts: list[UserFact]) -> str:
    """``<facts>`` with one ``<fact id kind>`` line per fact; empty when there are none."""
    lines = "".join(
        f'<fact id="{fact.pk}" kind="{fact.kind}">'
        f"{neutralise(' '.join(fact.text.split()), conversation=True)}</fact>\n"
        for fact in facts
    )
    return f"<facts>\n{lines}</facts>"


def build_memory_messages(question: str, facts: list[UserFact]) -> tuple[str, str]:
    """(system, user) for one extraction: the facts and the question -- nothing else.

    Never the answer, never the excerpts (D514): the answer quotes notes
    and attachments, so text pasted into one could otherwise be learned as
    the user's own words. Every part is neutralised, so nothing in a
    question or a fact can close its block early.
    """
    user = "\n\n".join([facts_block(facts), _block("question", question)])
    return load_prompt("memory")[1], user


# --- The reply ---------------------------------------------------------------------


def looks_secret(text: str) -> bool:
    return bool(_SECRET.search(text))


def _unfenced(text: str) -> str:
    """The reply without one surrounding Markdown code fence, which models add unasked (D404)."""
    text = text.strip()
    if text.startswith("```") and text.endswith("```") and len(text) >= 6:
        text = text[3:-3]
        first, _, rest = text.partition("\n")
        text = rest if first.strip().lower() in ("", "json") else text
    return text.strip()


def parse_operations(text: str, known_ids) -> tuple[list[Operation], list[str]]:
    """(the valid operations, why the others were dropped) from an extraction reply.

    The reply must be ``{"operations": [...]}`` with at most
    MEMORY_MAX_OPERATIONS entries; otherwise everything is dropped. Each
    entry is then checked on its own and dropped alone if it fails: an
    ``id`` must be an integer among ``known_ids`` (the facts the call was
    shown) and be targeted once; a text must be non-empty, at most
    FACT_MAX_CHARS once its whitespace is collapsed, and not look like a
    secret; a kind must be "static" or "dynamic" (absent: static, or the
    fact's own on an update). Keys beyond these are ignored. The reasons
    never quote the reply.
    """
    try:
        payload = json.loads(_unfenced(text))
    except ValueError:
        return [], ["the reply is not JSON"]
    entries = payload.get("operations") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return [], ['the reply has no "operations" list']
    if len(entries) > settings.MEMORY_MAX_OPERATIONS:
        return [], [f"{len(entries)} operations, more than {settings.MEMORY_MAX_OPERATIONS}"]

    known = set(known_ids)
    operations, problems, targeted = [], [], set()
    for index, entry in enumerate(entries, start=1):
        operation, problem = _operation(entry, known)
        if operation is not None and operation.fact_id is not None:
            if operation.fact_id in targeted:
                operation, problem = None, "the id is targeted twice"
            else:
                targeted.add(operation.fact_id)
        if operation is None:
            problems.append(f"operation {index}: {problem}")
        else:
            operations.append(operation)
    return operations, problems


def _operation(entry, known: set[int]) -> tuple[Operation | None, str]:
    if not isinstance(entry, dict):
        return None, "not an object"
    op = entry.get("op")
    if not isinstance(op, str) or op not in OPS:
        return None, "unknown op"
    if op == NONE:
        return Operation(NONE), ""

    fact_id = None
    if op in (UPDATE, SUPERSEDE):
        fact_id = entry.get("id")
        # type(), not isinstance(): True is an int to Python, not an id.
        if type(fact_id) is not int:
            return None, "the id is not an integer"
        if fact_id not in known:
            return None, "the id is not one of the facts shown"

    text = entry.get("text")
    if not isinstance(text, str) or not text.strip():
        return None, "no text"
    text = " ".join(text.split())
    if len(text) > FACT_MAX_CHARS:
        return None, f"the text is longer than {FACT_MAX_CHARS} characters"
    if looks_secret(text):
        return None, "the text looks like a secret"

    kind = entry.get("kind")
    if kind is None:
        kind = "" if op == UPDATE else UserFact.Kind.STATIC
    elif kind not in UserFact.Kind.values:
        return None, "unknown kind"
    return Operation(op, text, str(kind), fact_id), ""


# --- Extraction --------------------------------------------------------------------


def extract(ask_id: int) -> int:
    """Learn from one finished conversation turn; how many operations were applied.

    Does nothing for a turn that is not done, not a conversation turn, in a
    deleted conversation, already extracted, or whose user has memory off.
    Never raises for an expected failure: the limit reached, the provider
    or the embeddings failing, an unusable reply -- each is logged and the
    turn simply teaches nothing (D407).
    """
    ask = (
        AskQuery.objects.select_related("user", "conversation")
        .filter(pk=ask_id, status=AskQuery.Status.DONE, conversation__isnull=False)
        .first()
    )
    if ask is None or ask.conversation.deleted_at is not None:
        return 0
    if not ask.user.memory_enabled or _predates_reset(ask, ask.user):
        return 0

    event = _consume(ask)
    if event is None:
        return 0

    try:
        facts = known_facts(ask.user, ask.question)
    except EMBEDDING_ERRORS:
        logger.warning("Ask %s: the question could not be embedded; no memory", ask_id)
        limits.refund(event)
        return 0

    system, user = build_memory_messages(ask.question, facts)
    try:
        result = chat.complete(
            system, user, max_output_tokens=settings.MEMORY_EXTRACT_MAX_OUTPUT_TOKENS
        )
    except chat.BilledChatError as exc:
        # The vendor generated and bills for an unusable reply: the use stays
        # counted and records what it cost (D500, D550).
        logger.warning("Ask %s: memory extraction failed", ask_id, exc_info=True)
        limits.describe_where({"pk": event.pk}, **exc.cost())
        return 0
    except (chat.ChatError, chat.TransientChatError):
        logger.warning("Ask %s: memory extraction failed", ask_id, exc_info=True)
        limits.refund(event)
        return 0
    limits.describe_where(
        {"pk": event.pk},
        provider=result.provider,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )

    operations, problems = parse_operations(result.text, [fact.pk for fact in facts])
    if problems:
        logger.warning("Ask %s: memory reply: dropped %s", ask_id, "; ".join(problems))
    writes = [operation for operation in operations if operation.op != NONE]
    if not writes:
        return 0

    try:
        vectors = embed_texts([operation.text for operation in writes])
    except EMBEDDING_ERRORS:
        # The call was made and stays counted; the facts are lost, and the
        # next time the user says so they are learned (D407).
        logger.warning("Ask %s: the facts could not be embedded; dropped", ask_id)
        return 0
    return apply_operations(ask, writes, vectors)


def _consume(ask: AskQuery) -> UsageEvent | None:
    """The turn's ``memory_extract`` use, or None: already extracted, or the limit reached.

    The turn's row is locked while its events are checked, so two runs of
    one extraction (a redelivery) cannot both consume: the second finds the
    first's event -- refunded or not, since a failed extraction is not
    retried either (D407).
    """
    try:
        with transaction.atomic():
            list(AskQuery.objects.select_for_update().filter(pk=ask.pk).values_list("pk"))
            if UsageEvent.objects.filter(ask_id=ask.pk, key=MEMORY_KEY).exists():
                return None
            return limits.consume(None, MEMORY_KEY, ask=ask)
    except limits.SystemLimitExceeded:
        logger.warning("Ask %s: the memory_extract limit is reached; no memory", ask.pk)
        return None


def _predates_reset(ask: AskQuery, user) -> bool:
    """Whether the turn was asked before the user last forgot everything or switched off (D422)."""
    return user.memory_reset_at is not None and ask.created_at <= user.memory_reset_at


def _demoted(operation: Operation, target: UserFact | None) -> Operation:
    """The operation as written: a dynamic update or supersede of a static fact is an add (D515).

    Something true for now ("is in Goa this week") must not turn a lasting
    fact ("lives in Pune") into one that expires, nor replace it: when it
    expired, the purge would take the lasting fact with it.
    """
    if (
        target is not None
        and operation.op in (UPDATE, SUPERSEDE)
        and operation.kind == UserFact.Kind.DYNAMIC
        and target.kind == UserFact.Kind.STATIC
    ):
        return Operation(ADD, operation.text, operation.kind)
    return operation


def _valid_until(kind: str, now):
    if kind == UserFact.Kind.DYNAMIC:
        return now + timedelta(days=settings.MEMORY_DYNAMIC_FACT_DAYS)
    return None


def apply_operations(ask: AskQuery, operations: list[Operation], vectors) -> int:
    """Write checked operations for the turn's user; how many were applied.

    One transaction holding the user's row lock, so ``memory_enabled`` and
    ``memory_reset_at`` are read as they are now -- switched off, or
    everything forgotten, since the turn was asked: nothing is written
    (D401, D422) -- and two extractions of one user write one after the
    other. The targets are
    re-read live and owner-scoped in SQL, and locked: one superseded or
    deleted since the call was made is dropped, so a fact is never
    superseded twice (D408).
    """
    model_id = embedding_model_id()
    now = timezone.now()
    applied = 0
    with transaction.atomic():
        user = get_user_model().objects.select_for_update().get(pk=ask.user_id)
        if not user.memory_enabled:
            logger.info("Ask %s: memory was turned off; nothing written", ask.pk)
            return 0
        if _predates_reset(ask, user):
            logger.info("Ask %s: memory was reset since the turn; nothing written", ask.pk)
            return 0
        live = UserFact.objects.live(user, now)
        ids = [operation.fact_id for operation in operations if operation.fact_id is not None]
        targets = {fact.pk: fact for fact in live.select_for_update().filter(pk__in=ids)}

        for operation, vector in zip(operations, vectors, strict=True):
            if operation.fact_id is not None and operation.fact_id not in targets:
                logger.warning(
                    "Ask %s: memory: fact %s is no longer live", ask.pk, operation.fact_id
                )
                continue
            operation = _demoted(operation, targets.get(operation.fact_id))
            if operation.op == UPDATE:
                fact = targets[operation.fact_id]
                fact.text = operation.text
                fact.kind = operation.kind or fact.kind
                fact.valid_until = _valid_until(fact.kind, now)
                fact.embedding, fact.embedding_model = vector, model_id
                fact.source_ask = ask
                fact.save(
                    update_fields=[
                        "text",
                        "kind",
                        "valid_until",
                        "embedding",
                        "embedding_model",
                        "source_ask",
                    ]
                )
                applied += 1
                continue
            if operation.op == ADD and live.filter(text__iexact=operation.text).exists():
                continue  # Already known, word for word.
            fact = UserFact.objects.create(
                user=user,
                text=operation.text,
                kind=operation.kind,
                source_ask=ask,
                embedding=vector,
                embedding_model=model_id,
                valid_until=_valid_until(operation.kind, now),
            )
            if operation.op == SUPERSEDE:
                old = targets[operation.fact_id]
                old.superseded_by = fact
                old.save(update_fields=["superseded_by"])
            applied += 1
    return applied


# --- Expiry ------------------------------------------------------------------------


def purge_expired(now=None) -> int:
    """Delete expired dynamic facts, and facts superseded over the retention ago (D406).

    How many facts were deleted. Deleting a fact deletes those it
    superseded (``superseded_by`` CASCADE, D405), counted too -- except a
    static fact: one superseded by a dynamic fact (written before D515 made
    that an add) is live again when its replacement expires, and waits for
    that rather than for the retention (D516).
    """
    now = now or timezone.now()
    static, dynamic = UserFact.Kind.STATIC, UserFact.Kind.DYNAMIC
    with transaction.atomic():
        UserFact.objects.filter(kind=static, superseded_by__valid_until__lte=now).update(
            superseded_by=None
        )
        expired, _ = UserFact.objects.filter(valid_until__lte=now).delete()
    cutoff = now - timedelta(days=settings.MEMORY_SUPERSEDED_RETENTION_DAYS)
    superseded, _ = (
        UserFact.objects.filter(superseded_by__created_at__lt=cutoff)
        .exclude(kind=static, superseded_by__kind=dynamic)
        .delete()
    )
    return expired + superseded
