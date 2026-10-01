"""The default provider. No network, no keys, no cost.

Every environment that has not set CHAT_PROVIDER runs on this, and the test
runner forces it. Unlike the reference's fixed fake bill, its answer depends
on the prompt: it quotes the first sentence of excerpt [1] (and of [2], when
there is one) with their citation markers, the way a grounded answer would.
That keeps an end-to-end test meaningful -- the citations it parses point at
real excerpts, so a test can check they map back to the right notes.

A condense call (prompts/condense.md: the user message ends in
``<follow_up>``) gets a fixed rule instead (DECISIONS D225): the first word
of the follow-up that points back -- "it", "them", "that", "one"... -- is
replaced by the content words of the previous turn's question; a follow-up
with no such word comes back unchanged, as a topic shift should. So "When
is it due next?" after "When did I last service the Honda City?" becomes
"When is last service Honda City due next?", which retrieval can match.

A fold call (prompts/summarize.md: the user message holds ``<fold>``) gets
another fixed rule (DECISIONS D286): the summary so far, one line
``- <question> -> <first sentence of the answer>`` per folded turn, and
only the newest FOLD_LINES lines kept -- a bounded summary that forgets the
oldest, as a real one is told to.

A memory extraction (prompts/memory.md: the user message holds ``<facts>``)
gets a rule of its own (DECISIONS D409), read from the ``<question>``, the
only text the call gets (D514). Each sentence of the question that
is not itself a question and says "I'm X" / "I am X" (or "I'm not X", "I'm
no longer X") or "my X is Y" is a statement about a subject (X; the X of
"my X"). A statement about a subject no known fact covers is an ``add``
("User is X.", "User's X is Y."); one that differs from the known fact
about the same subject is a ``supersede`` of it; one already known is
nothing. A sentence with "today", "this week", "currently"... is dynamic.
No statement: ``{"op": "none"}``.

An image (``read_image``, DECISIONS D343) is "read" as FAKE_IMAGE_TEXT,
whatever it shows: fixed, so a test can search for its words.

Streamed, the fake yields its answer a word at a time (each word with the
whitespace after it), then the result (DECISIONS D362). The answer is the
boundary's ``assistant.chat.complete``, looked up at call time: when the fake
is the configured provider that is this class's own ``complete``, and a test
that scripts ``chat.complete`` (an error, a retry, a token count) scripts
the streamed answer too.
"""

import json
import re

from django.conf import settings

from ..types import ChatResult

# The delimiters assistant/prompt.py writes. Excerpt text cannot contain a
# closing tag (prompt.neutralise), so a lazy match finds each excerpt whole.
_EXCERPT = re.compile(r'<excerpt n="(\d+)"[^>]*>\n(.*?)\n</excerpt>', re.DOTALL)
# The note notes/format_prompt.py wraps its document in. Greedy: the document is
# JSON, so the last closing tag is the real one.
_NOTE = re.compile(r"<note>\n(.*)\n</note>", re.DOTALL)
_SENTENCE = re.compile(r"(.+?[.!?])(?=\s|$)")
# Between a whitespace and the next word: where the fake stream cuts.
_WORD_START = re.compile(r"(?<=\s)(?=\S)")

_FOLLOW_UP = re.compile(r"<follow_up>\n(.*?)\n</follow_up>", re.DOTALL)
_HISTORY_QUESTION = re.compile(r"<question>\n(.*?)\n</question>", re.DOTALL)
_TOKEN = re.compile(r"[\w'’.-]+")

_FOLD = re.compile(r"<fold>\n(.*?)\n</fold>", re.DOTALL)
_SUMMARY = re.compile(r"<summary>\n(.*?)\n</summary>", re.DOTALL)
_FOLD_TURN = re.compile(
    r"<turn>\n<question>\n(.*?)\n</question>\n<answer>\n(.*?)\n</answer>\n</turn>", re.DOTALL
)
# The fake summary keeps this many lines.
FOLD_LINES = 6

_FACTS = re.compile(r"<facts>\n(.*?)</facts>", re.DOTALL)
_KNOWN_FACT = re.compile(r'<fact id="(\d+)" kind="\w+">(.*?)</fact>')
_MEMORY_QUESTION = re.compile(r"<question>\n(.*?)\n</question>", re.DOTALL)
_STATEMENT_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")
_I_AM = re.compile(r"\bI(?:['’]m| am)\s+(not\s+|no longer\s+)?(.+)", re.IGNORECASE)
_MY = re.compile(r"\bmy\s+((?:[\w'’-]+\s+){0,3}?[\w'’-]+)\s+is\s+(.+)", re.IGNORECASE)
_KNOWN_IS = re.compile(r"^User is (?:not )?(.+?)\.?$")
_KNOWN_MY = re.compile(r"^User's (.+?) is (.+?)\.?$")
_DYNAMIC = re.compile(
    r"\b(?:today|tomorrow|tonight|this (?:week|month)|next (?:week|month)|currently"
    r"|right now|at the moment|for now)\b",
    re.IGNORECASE,
)
# A statement longer than this ("I'm looking for the note about...") is not a fact.
STATEMENT_MAX_WORDS = 6

# What the fake condenser resolves: the commonest words that point back.
POINTING = frozenset("it its them they their this that these those one ones".split())
# Left out of the previous question when it stands in for a pointing word.
STOPWORDS = frozenset(
    "a an the and or but of to in on at for from by with about into over under "
    "what when where which who whom whose why how is are was were be been being "
    "do does did done have has had will would shall should can could may might must "
    "i me my mine we us our you your he him his she her it its they them their "
    "this that these those there here any some all much many next".split()
)

# What every image "says" to the fake.
FAKE_IMAGE_TEXT = (
    "Whiteboard notes from the planning meeting.\n\n"
    "Renew the scooter insurance before the fifteenth."
)

# The answer quotes at most this many excerpts.
CITED = 2
# A "sentence" with no full stop in sight is cut here.
MAX_SENTENCE_CHARS = 200


def approximate_tokens(text: str) -> int:
    """About four characters a token -- the usual rule of thumb for English."""
    return max(1, (len(text) + 3) // 4)


def first_sentence(text: str) -> str:
    text = " ".join(text.split())
    match = _SENTENCE.match(text)
    sentence = match.group(1) if match else text
    return sentence[:MAX_SENTENCE_CHARS]


def format_document(doc: dict) -> dict:
    """What the fake "formats": a lone first line becomes the heading.

    The one visible, deterministic restructuring that keeps every word, so
    an end-to-end format job passes its guardrail. A document that already
    has a heading, or has nothing after its first paragraph, is returned as it is.
    """
    blocks = doc.get("content") if isinstance(doc, dict) else None
    if not isinstance(blocks, list) or len(blocks) < 2:
        return doc
    if any(isinstance(b, dict) and b.get("type") == "heading" for b in blocks):
        return doc
    first = blocks[0]
    if isinstance(first, dict) and first.get("type") == "paragraph":
        heading = {**first, "type": "heading", "attrs": {"level": 2}}
        return {**doc, "content": [heading, *blocks[1:]]}
    return doc


class FakeProvider:
    name = "fake"

    def complete(
        self, system: str, user: str, model: str = "", max_output_tokens: int = 0
    ) -> ChatResult:
        # Deterministic on purpose -- no clock, no randomness -- so a test
        # asserting on this result cannot flake.
        note = _NOTE.match(user)
        follow_up = _FOLLOW_UP.search(user)
        excerpts = _EXCERPT.findall(user)[:CITED]
        fold = _FOLD.search(user)
        facts = _FACTS.match(user)
        if note:
            # A "format my note" request (notes/format_prompt.py): answer with the
            # document, restructured, as the prompt demands.
            try:
                text = json.dumps(format_document(json.loads(note.group(1))), ensure_ascii=False)
            except ValueError:
                text = "not json"
        elif facts:
            question = _MEMORY_QUESTION.search(user, facts.end())
            text = extract_facts(
                question.group(1) if question else "", _KNOWN_FACT.findall(facts.group(1))
            )
        elif fold:
            previous = _SUMMARY.search(user[: fold.start()])
            text = summarise(
                previous.group(1) if previous else "", _FOLD_TURN.findall(fold.group(1))
            )
        elif follow_up:
            previous = _HISTORY_QUESTION.findall(user[: follow_up.start()])
            text = condense(follow_up.group(1), previous[-1] if previous else "")
        elif excerpts:
            text = " ".join(f"{first_sentence(body)} [{n}]" for n, body in excerpts)
        else:
            text = settings.ASK_NO_ANSWER_TEXT

        return ChatResult(
            text=text,
            provider=self.name,
            model="fake",
            input_tokens=approximate_tokens(system) + approximate_tokens(user),
            output_tokens=approximate_tokens(text),
        )

    def read_image(
        self,
        system: str,
        user: str,
        image: bytes,
        mime_type: str,
        model: str = "",
        max_output_tokens: int = 0,
    ) -> ChatResult:
        # An image is billed by its pixels, not its bytes; a constant stands in.
        return ChatResult(
            text=FAKE_IMAGE_TEXT,
            provider=self.name,
            model="fake",
            input_tokens=approximate_tokens(system) + approximate_tokens(user) + 1000,
            output_tokens=approximate_tokens(FAKE_IMAGE_TEXT),
        )

    def stream(self, system: str, user: str, model: str = "", max_output_tokens: int = 0):
        if settings.CHAT_PROVIDER == self.name:
            # Through the boundary, so a test that patches it is obeyed;
            # keyword, so a spy sees the same (system, user) args as before.
            from assistant import chat

            result = chat.complete(system, user, max_output_tokens=max_output_tokens or None)
        else:
            result = self.complete(system, user, model, max_output_tokens)
        yield from words(result.text)
        yield result


def words(text: str) -> list[str]:
    """`text` cut before each word that follows whitespace; joined, they are `text` again."""
    return [piece for piece in _WORD_START.split(text) if piece]


def keywords(question: str) -> str:
    """The content words of a question, in order: "Honda City service"."""
    words = (word.strip(".?!'’") for word in _TOKEN.findall(question))
    return " ".join(word for word in words if word and word.lower() not in STOPWORDS)


def condense(follow_up: str, previous_question: str) -> str:
    """The fake condenser: the first pointing word becomes the previous question's subject."""
    subject = keywords(previous_question)
    if not subject:
        return follow_up
    for match in _TOKEN.finditer(follow_up):
        if match.group().lower().strip(".?!'’") in POINTING:
            return follow_up[: match.start()] + subject + follow_up[match.end() :]
    return follow_up


def summarise(previous: str, turns: list[tuple[str, str]]) -> str:
    """The fake summariser: the old lines plus one per folded turn, the newest FOLD_LINES kept."""
    lines = [line for line in previous.splitlines() if line.strip()]
    lines += [
        f"- {' '.join(question.split())} -> {first_sentence(answer)}" for question, answer in turns
    ]
    return "\n".join(lines[-FOLD_LINES:])


def _subject_of_known(text: str) -> str | None:
    """The subject a known fact is about, as ``statements`` names it; None if neither shape."""
    my = _KNOWN_MY.match(text)
    if my:
        return "my " + my.group(1).lower()
    is_ = _KNOWN_IS.match(text)
    return "is " + is_.group(1).lower() if is_ else None


def statements(question: str) -> list[tuple[str, str, str]]:
    """(subject, fact text, kind) for each statement the user makes about themselves."""
    found = []
    for sentence in _STATEMENT_SPLIT.split(question.strip()):
        sentence = sentence.strip()
        if not sentence or sentence.endswith("?"):
            continue
        body = sentence.rstrip(".!;").strip()
        kind = "dynamic" if _DYNAMIC.search(body) else "static"
        my = _MY.search(body)
        if my and len(my.group(2).split()) <= STATEMENT_MAX_WORDS:
            subject, value = my.group(1), my.group(2)
            found.append((f"my {subject.lower()}", f"User's {subject} is {value}.", kind))
            continue
        i_am = _I_AM.search(body)
        if i_am and len(i_am.group(2).split()) <= STATEMENT_MAX_WORDS:
            negation = "not " if i_am.group(1) else ""
            value = i_am.group(2)
            found.append((f"is {value.lower()}", f"User is {negation}{value}.", kind))
    return found


def extract_facts(question: str, known: list[tuple[str, str]]) -> str:
    """The fake extraction's reply: JSON operations, from the question alone."""
    by_subject = {}
    for fact_id, text in known:
        subject = _subject_of_known(text)
        if subject is not None:
            by_subject[subject] = (int(fact_id), text)
    operations, seen = [], set()
    for subject, text, kind in statements(question):
        if subject in seen:
            continue  # The first thing said about a subject in one question wins.
        seen.add(subject)
        current = by_subject.get(subject)
        if current is None:
            operations.append({"op": "add", "text": text, "kind": kind})
        elif current[1].lower() != text.lower():
            operations.append({"op": "supersede", "id": current[0], "text": text, "kind": kind})
    return json.dumps({"operations": operations or [{"op": "none"}]})
