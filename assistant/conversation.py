"""A conversation turn: its history, the condensed question, and its prompt (plan Phase 1).

A turn is an ask (DECISIONS D140), answered by the same task. What a
conversation adds happens here, before and around retrieval:

1. **History.** The conversation's answered turns before this one that the
   summary does not already cover, oldest first. Earlier answers lose their
   ``[n]`` markers: those numbered another turn's excerpts, and this turn's
   citations number only its own (DECISIONS D223).
2. **Condense** (turn 2 onward). A follow-up that points back into the
   conversation ("when is it due?") is rewritten by the chat provider into
   a question that stands alone, and that is what is searched. A cheap
   heuristic skips the call when the follow-up already stands alone
   (DECISIONS D221). The call consumes the system-only ``condense`` limit
   and records its cost on that event (DECISIONS D222). Any failure -- the
   provider refusing or down, the limit reached, an empty reply -- falls
   back to searching the follow-up as asked: condensing is an
   improvement, never a reason to fail a turn.
3. **The prompt** (prompts/chat.md): the summary, the newest turns that fit
   CHAT_HISTORY_MAX_CHARS, this turn's excerpts and the question as asked.
4. **Folding**, after a turn is answered (its own task, ``fold_history``): when
   the turns the summary does not cover outgrow CHAT_HISTORY_MAX_CHARS, the
   oldest are folded into ``Conversation.summary`` by the chat provider and
   ``summary_through`` moves up (DECISIONS D280-D287). The summary is bounded,
   the write is conditional on ``summary_through`` so two folds can never
   fold a turn twice or lose one, and a failed fold changes nothing.
"""

import logging
import re
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction

from limits import service as limits

from . import chat
from .citations import MARKER
from .models import QUESTION_MAX_CHARS, AskQuery, Conversation
from .prompt import Excerpt, excerpts_block, load_prompt, neutralise

logger = logging.getLogger(__name__)

# The system-only limit key a condense call consumes (DECISIONS D91).
CONDENSE_KEY = "condense"
# The same for a folding call (DECISIONS D280): its own budget, so a runaway
# summariser cannot starve condensing, which every follow-up depends on.
SUMMARIZE_KEY = "summarize_history"

# A fold keeps the newest turns that fit this share of the history budget
# unfolded, so the next fold is a few turns away rather than the very next
# one (DECISIONS D281).
FOLD_KEEP_SHARE = 0.5
# What one folding call is sent at most: each folded answer cut to
# FOLD_ANSWER_MAX_CHARS, the turns taken oldest first up to FOLD_INPUT_MAX_CHARS
# (one turn at least). A longer backlog is folded by further runs (D283).
FOLD_ANSWER_MAX_CHARS = 2000
FOLD_INPUT_MAX_CHARS = 12000


@dataclass(frozen=True)
class HistoryTurn:
    """An earlier turn, as a prompt repeats it."""

    position: int
    question: str
    answer: str

    @property
    def size(self) -> int:
        return len(self.question) + len(self.answer)


@dataclass(frozen=True)
class TurnContext:
    """What the task needs to answer a turn: what to search, and what to repeat."""

    search_question: str
    history: list[HistoryTurn]
    summary: str


def prepare(ask: AskQuery) -> TurnContext:
    """History and the question to search for one conversation turn.

    A turn taken up again (a retry, a redelivery) reuses the standalone
    question it already stored instead of condensing -- and paying -- twice.
    """
    history = history_for(ask)
    standalone = ask.standalone_question
    if not standalone and history and needs_condensing(ask.question):
        standalone = condense(ask, history) or ""
        if standalone:
            AskQuery.objects.filter(pk=ask.pk).update(standalone_question=standalone)
    return TurnContext(
        search_question=standalone or ask.question,
        history=fit_history(history, settings.CHAT_HISTORY_MAX_CHARS),
        summary=ask.conversation.summary,
    )


# --- History --------------------------------------------------------------


def history_for(ask: AskQuery) -> list[HistoryTurn]:
    """The answered turns before ``ask`` that the summary does not cover, oldest first.

    A failed turn has no answer to repeat and is left out; so is anything
    still unfinished (there is none: turns are sequential).
    """
    return _answered_turns(
        ask.conversation_id, after=ask.conversation.summary_through, before=ask.position
    )


def _answered_turns(
    conversation_id: int, after: int, before: int | None = None
) -> list[HistoryTurn]:
    """The conversation's done turns with ``after`` < position < ``before``, oldest first."""
    rows = AskQuery.objects.filter(
        conversation_id=conversation_id, position__gt=after, status=AskQuery.Status.DONE
    )
    if before is not None:
        rows = rows.filter(position__lt=before)
    return [
        HistoryTurn(position=position, question=question, answer=strip_markers(answer))
        for position, question, answer in rows.order_by("position").values_list(
            "position", "question", "answer"
        )
    ]


def strip_markers(answer: str) -> str:
    """An earlier answer without its ``[n]`` markers (DECISIONS D223)."""
    text = MARKER.sub("", answer)
    text = re.sub(r"[ \t]+([.,;:!?])", r"\1", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def fit_history(turns: list[HistoryTurn], max_chars: int) -> list[HistoryTurn]:
    """The newest turns whose questions and answers fit in ``max_chars``, oldest first.

    Whole turns, newest first, until the next would overflow; older ones are
    dropped even if a short one would still fit, so the history never has a
    gap. The newest turn is always kept -- it is what a follow-up most
    likely points at -- with its answer cut to the room left if need be
    (DECISIONS D224).
    """
    kept: list[HistoryTurn] = []
    budget = max_chars
    for turn in reversed(turns):
        if turn.size <= budget:
            kept.append(turn)
            budget -= turn.size
            continue
        if not kept:
            room = max(budget - len(turn.question), 0)
            kept.append(HistoryTurn(turn.position, turn.question, _cut(turn.answer, room)))
        break
    kept.reverse()
    return kept


def _cut(text: str, chars: int) -> str:
    if len(text) <= chars:
        return text
    cut = text[:chars]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return (cut.rstrip() + " …").lstrip()


# --- Condensing -------------------------------------------------------------

_WORD = re.compile(r"[a-z]+(?:'[a-z]+)?")

# Words that point back into the conversation. Over-inclusive on purpose:
# a false positive costs one cheap call (and the prompt returns a question
# that stands alone unchanged); a false negative searches "when is it due?"
# as it is.
REFERRING = frozenset(
    "it its itself they them their theirs these those this that he him his she her hers "
    "there one ones same former latter else".split()
)
# A follow-up opening with one of these continues the previous turn.
CONTINUING = frozenset("and but or also only so then too".split())
CONTINUING_PAIRS = frozenset({"what about", "how about"})
# Shorter than this ("Why?", "And Sneha's?") is read as leaning on context.
MIN_STANDALONE_WORDS = 4


def needs_condensing(question: str) -> bool:
    """Whether a follow-up may lean on the conversation (DECISIONS D221).

    English only: a question with letters outside ASCII cannot be judged by
    these word lists, so it is always condensed.
    """
    text = question.lower().replace("’", "'")
    if any(char.isalpha() and not char.isascii() for char in text):
        return True
    words = _WORD.findall(text)
    if len(words) < MIN_STANDALONE_WORDS:
        return True
    if words[0] in CONTINUING or " ".join(words[:2]) in CONTINUING_PAIRS:
        return True
    return any(word.split("'")[0] in REFERRING for word in words)


def run_condenser(question: str, history: list[HistoryTurn]) -> tuple[str, chat.ChatResult]:
    """(the follow-up rewritten to stand alone, the provider's result): no bookkeeping.

    The part of condensing that needs no ask: it consumes no limit and
    records nothing, so the evaluation (eval_retrieval --conversations) can
    condense fixtures that have no AskQuery. The rewrite is "" when the
    reply holds nothing usable. Raises chat.ChatError or
    chat.TransientChatError.
    """
    turns = fit_history(history, settings.CHAT_CONDENSE_HISTORY_MAX_CHARS)
    system, user = build_condense_messages(question, turns)
    result = chat.complete(system, user, max_output_tokens=settings.CHAT_CONDENSE_MAX_OUTPUT_TOKENS)
    return clean_condensed(result.text), result


def condense(ask: AskQuery, history: list[HistoryTurn]) -> str | None:
    """The follow-up rewritten to stand alone, or None to search it as asked.

    The ``condense`` use is recorded in its own short transaction before the
    call -- not held open across it -- linked to the turn, with user None
    (it costs the user nothing, DECISIONS D91). A provider failure refunds
    it (nothing was billed); a success records provider, model and tokens
    on it (DECISIONS D222).
    """
    try:
        with transaction.atomic():
            event = limits.consume(None, CONDENSE_KEY, ask=ask)
    except limits.SystemLimitExceeded:
        logger.warning("Ask %s: the condense limit is reached; searching as asked", ask.pk)
        return None

    try:
        rewritten, result = run_condenser(ask.question, history)
    except (chat.ChatError, chat.TransientChatError):
        logger.warning("Ask %s: condensing failed; searching as asked", ask.pk, exc_info=True)
        limits.refund(event)
        return None

    limits.describe_where(
        {"pk": event.pk},
        provider=result.provider,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )
    if not rewritten:
        logger.warning("Ask %s: the condenser returned nothing usable; searching as asked", ask.pk)
    return rewritten or None


_LABEL = re.compile(r"^(?:standalone|rewritten)?\s*question\s*:\s*", re.IGNORECASE)
_QUOTES = "\"'“”‘’`"


def clean_condensed(text: str) -> str:
    """The condenser's reply as a question: its first line, without a label or quotes."""
    line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    line = _LABEL.sub("", line).strip().strip(_QUOTES).strip()
    return line[:QUESTION_MAX_CHARS]


# --- Prompts ----------------------------------------------------------------


def chat_prompt_version() -> str:
    """The version line of prompts/chat.md, e.g. "chat-v1"."""
    return load_prompt("chat")[0]


def condense_prompt_version() -> str:
    """The version line of prompts/condense.md, e.g. "condense-v1"."""
    return load_prompt("condense")[0]


def _block(tag: str, text: str) -> str:
    return f"<{tag}>\n{neutralise(text.strip(), conversation=True)}\n</{tag}>"


def _turn_block(turn: HistoryTurn) -> str:
    return f"<turn>\n{_block('question', turn.question)}\n{_block('answer', turn.answer)}\n</turn>"


def history_block(turns: list[HistoryTurn]) -> str:
    """``<history>`` with one ``<turn>`` per earlier turn, oldest first."""
    return "<history>\n" + "\n\n".join(_turn_block(turn) for turn in turns) + "\n</history>"


def summarize_prompt_version() -> str:
    """The version line of prompts/summarize.md, e.g. "summarize-v1"."""
    return load_prompt("summarize")[0]


def build_condense_messages(question: str, turns: list[HistoryTurn]) -> tuple[str, str]:
    """(system, user) for condensing one follow-up."""
    user = history_block(turns) + "\n\n" + _block("follow_up", question)
    return load_prompt("condense")[1], user


def build_chat_messages(
    question: str, excerpts: list[Excerpt], turns: list[HistoryTurn], summary: str = ""
) -> tuple[str, str]:
    """(system, user) for answering one turn.

    The summary and the history first, as context; then this turn's
    excerpts, numbered as in a plain ask; the question as asked last.
    """
    parts = []
    if summary.strip():
        parts.append(_block("summary", summary))
    if turns:
        parts.append(history_block(turns))
    parts.append(excerpts_block(excerpts, conversation=True))
    parts.append(_block("question", question))
    return load_prompt("chat")[1], "\n\n".join(parts)


def build_summarize_messages(summary: str, turns: list[HistoryTurn]) -> tuple[str, str]:
    """(system, user) for folding ``turns`` into ``summary``.

    The summary so far (if any), then the turns to fold in ``<fold>``, each
    answer cut to FOLD_ANSWER_MAX_CHARS.
    """
    parts = []
    if summary.strip():
        parts.append(_block("summary", summary))
    folded = [
        HistoryTurn(t.position, t.question, _cut(t.answer, FOLD_ANSWER_MAX_CHARS)) for t in turns
    ]
    parts.append("<fold>\n" + "\n\n".join(_turn_block(turn) for turn in folded) + "\n</fold>")
    return load_prompt("summarize")[1], "\n\n".join(parts)


# --- Folding ----------------------------------------------------------------


def should_fold(conversation_id: int) -> bool:
    """Whether the turns the summary does not cover outgrow the history budget.

    The same turns, and the same sizes, that the next prompt would repeat
    (history_for): folding starts when fit_history would begin dropping.
    """
    through = (
        Conversation.objects.filter(pk=conversation_id, deleted_at__isnull=True)
        .values_list("summary_through", flat=True)
        .first()
    )
    if through is None:
        return False
    turns = _answered_turns(conversation_id, after=through)
    return sum(turn.size for turn in turns) > settings.CHAT_HISTORY_MAX_CHARS


def plan_fold(turns: list[HistoryTurn], max_chars: int) -> list[HistoryTurn]:
    """The oldest of ``turns`` to fold into the summary; [] while they fit ``max_chars``.

    Over budget, the newest turns that fit FOLD_KEEP_SHARE of it stay (the
    newest always does), and the older ones are folded -- at most
    FOLD_INPUT_MAX_CHARS of them in one call, oldest first, one turn at
    least. Pure, so the arithmetic is tested without a database.
    """
    if sum(turn.size for turn in turns) <= max_chars:
        return []
    keep_budget = int(max_chars * FOLD_KEEP_SHARE)
    kept, split = 0, len(turns)
    for index in range(len(turns) - 1, -1, -1):
        if index < len(turns) - 1 and kept + turns[index].size > keep_budget:
            break
        kept += turns[index].size
        split = index
    batch, total = [], 0
    for turn in turns[:split]:
        size = len(turn.question) + min(len(turn.answer), FOLD_ANSWER_MAX_CHARS)
        if batch and total + size > FOLD_INPUT_MAX_CHARS:
            break
        batch.append(turn)
        total += size
    return batch


def fold(conversation_id: int) -> bool:
    """Fold the conversation's oldest unsummarised turns into its summary; True if it did.

    Reads the summary and ``summary_through``, calls the provider *without*
    holding a transaction or a row lock (it can take a minute, and a turn's
    ``updated_at`` write would wait behind it), then writes the result only
    if ``summary_through`` is still what was read: a conditional UPDATE, so
    of two folds racing on one conversation exactly one writes. The loser's
    result is dropped -- its turns are already folded by the winner, and
    folding them again would duplicate them -- and a turn is never skipped,
    because ``summary_through`` only ever moves up to the last turn this
    fold actually read (DECISIONS D282).

    Anything else leaves the conversation unchanged: the ``summarize_history``
    limit reached, the provider refusing or down (the use is refunded:
    nothing was billed), an unusable reply (the call was made: it stays
    counted). The next finished turn tries again, since the history is still
    over budget (DECISIONS D284). The conversation's ``updated_at`` is not
    touched: folding is not activity.
    """
    row = (
        Conversation.objects.filter(pk=conversation_id, deleted_at__isnull=True)
        .values("summary", "summary_through")
        .first()
    )
    if row is None:
        return False
    through = row["summary_through"]
    batch = plan_fold(
        _answered_turns(conversation_id, after=through), settings.CHAT_HISTORY_MAX_CHARS
    )
    if not batch:
        return False
    new_through = batch[-1].position
    last_turn = AskQuery.objects.filter(conversation_id=conversation_id, position=new_through)

    try:
        with transaction.atomic():
            event = limits.consume(None, SUMMARIZE_KEY, ask=last_turn.first())
    except limits.SystemLimitExceeded:
        logger.warning("Conversation %s: the summarize_history limit is reached", conversation_id)
        return False

    system, user = build_summarize_messages(row["summary"], batch)
    try:
        result = chat.complete(
            system, user, max_output_tokens=settings.CHAT_SUMMARY_MAX_OUTPUT_TOKENS
        )
    except (chat.ChatError, chat.TransientChatError):
        logger.warning("Conversation %s: folding failed", conversation_id, exc_info=True)
        limits.refund(event)
        return False

    limits.describe_where(
        {"pk": event.pk},
        provider=result.provider,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )
    summary = clean_summary(result.text)
    if not summary:
        logger.warning("Conversation %s: the summariser returned nothing usable", conversation_id)
        return False

    written = Conversation.objects.filter(pk=conversation_id, summary_through=through).update(
        summary=summary, summary_through=new_through
    )
    if not written:
        logger.info("Conversation %s: another fold got there first", conversation_id)
    return bool(written)


_SUMMARY_LABEL = re.compile(r"^(?:updated\s+)?summary\s*:\s*", re.IGNORECASE)


def clean_summary(text: str) -> str:
    """The summariser's reply as a summary: no label, no quotes, at most CHAT_SUMMARY_MAX_CHARS.

    The prompt asks for 200 words; this is the guarantee, since the summary
    rides in every later prompt and must not grow without limit (D285).
    Cut at a word, with an ellipsis, the way an overlong answer is.
    """
    summary = _SUMMARY_LABEL.sub("", text.strip()).strip().strip(_QUOTES).strip()
    limit = settings.CHAT_SUMMARY_MAX_CHARS
    # _cut adds " …" to what it cuts: leave room, so the cap is a cap.
    return _cut(summary, limit - 2) if len(summary) > limit else summary
