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
"""

import logging
import re
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction

from limits import service as limits

from . import chat
from .citations import MARKER
from .models import QUESTION_MAX_CHARS, AskQuery
from .prompt import Excerpt, excerpts_block, load_prompt, neutralise

logger = logging.getLogger(__name__)

# The system-only limit key a condense call consumes (DECISIONS D91).
CONDENSE_KEY = "condense"


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
    rows = AskQuery.objects.filter(
        conversation_id=ask.conversation_id,
        position__lt=ask.position,
        position__gt=ask.conversation.summary_through,
        status=AskQuery.Status.DONE,
    ).order_by("position")
    return [
        HistoryTurn(position=position, question=question, answer=strip_markers(answer))
        for position, question, answer in rows.values_list("position", "question", "answer")
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


def condense(ask: AskQuery, history: list[HistoryTurn]) -> str | None:
    """The follow-up rewritten to stand alone, or None to search it as asked.

    The ``condense`` use is recorded in its own short transaction before the
    call -- not held open across it -- linked to the turn, with user None
    (it costs the user nothing, DECISIONS D91). A provider failure refunds
    it (nothing was billed); a success records provider, model and tokens
    on it (DECISIONS D222).
    """
    turns = fit_history(history, settings.CHAT_CONDENSE_HISTORY_MAX_CHARS)
    system, user = build_condense_messages(ask.question, turns)
    try:
        with transaction.atomic():
            event = limits.consume(None, CONDENSE_KEY, ask=ask)
    except limits.SystemLimitExceeded:
        logger.warning("Ask %s: the condense limit is reached; searching as asked", ask.pk)
        return None

    try:
        result = chat.complete(
            system, user, max_output_tokens=settings.CHAT_CONDENSE_MAX_OUTPUT_TOKENS
        )
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
    rewritten = clean_condensed(result.text)
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


def history_block(turns: list[HistoryTurn]) -> str:
    """``<history>`` with one ``<turn>`` per earlier turn, oldest first."""
    blocks = [
        f"<turn>\n{_block('question', turn.question)}\n{_block('answer', turn.answer)}\n</turn>"
        for turn in turns
    ]
    return "<history>\n" + "\n\n".join(blocks) + "\n</history>"


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
