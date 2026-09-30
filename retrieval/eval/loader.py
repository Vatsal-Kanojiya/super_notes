"""Load and validate the evaluation fixtures.

The fixtures are hand-written JSON, and a typo in them does not fail loudly on
its own: a question pointing at ``n4`` instead of ``n04`` would just score zero
forever and read as "retrieval is bad". So everything is checked on load and a
mistake raises ``FixtureError`` naming the file and the entry.

The notes are TipTap documents, the same JSON the editor saves (StarterKit
plus TaskList/TaskItem), so the eval exercises the real chunker rather than a
flattened string. Only the node types the editor can produce are accepted: a
node the chunker has never seen would make the eval measure a bug, not
retrieval.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
NOTES_FILE = FIXTURES_DIR / "notes.json"
QUESTIONS_FILE = FIXTURES_DIR / "questions.json"
CONVERSATIONS_FILE = FIXTURES_DIR / "conversations.json"

NOTE_TYPES = frozenset({"text", "checklist"})

# What a question tests; docs/RAG.md "Evaluation" describes each one.
QUESTION_KINDS = frozenset(
    {
        "keyword",  # shares rare words with the note: keyword search should find it
        "paraphrase",  # shares no content words: only vector search can
        "section",  # the answer sits in one section of a long, multi-chunk note
        "near_duplicate",  # two notes nearly match; only one is right
        "checklist",  # the answer is in a task list
        "hinglish",  # Hindi in Latin script
        "multi_note",  # two notes are both needed
        "no_answer",  # nothing in the notes answers it (the relevance floor's test)
    }
)

# What a conversation's last turn tests; docs/RAG.md "Multi-turn evaluation (V2)".
CONVERSATION_KINDS = frozenset(
    {
        "pronoun",  # "when is it due?": the subject is only in an earlier turn
        "ellipsis",  # "and the February one?": the sentence is cut short
        "topic_shift",  # the follow-up changes topic: condensing must not drag old context in
        "refinement",  # "only the unchecked ones": narrows the previous answer
        "near_duplicate",  # the follow-up picks one of two near-duplicate notes
        "no_answer",  # the follow-up has no answer in the notes
    }
)

# StarterKit + TaskList/TaskItem. Anything else is a fixture mistake.
_NODE_TYPES = frozenset(
    {
        "doc",
        "paragraph",
        "text",
        "heading",
        "bulletList",
        "orderedList",
        "listItem",
        "taskList",
        "taskItem",
        "blockquote",
        "codeBlock",
        "hardBreak",
        "horizontalRule",
    }
)
_MARK_TYPES = frozenset({"bold", "italic", "strike", "code"})


class FixtureError(ValueError):
    """The fixture files are malformed. The message names the file and entry."""


@dataclass(frozen=True)
class EvalNote:
    """One fixture note. ``key`` ("n01") is stable; database ids are not."""

    key: str
    type: str
    title: str
    content: dict[str, Any]


@dataclass(frozen=True)
class EvalQuestion:
    """One question and the notes a good retriever should surface for it.

    ``relevant`` is empty for a ``no_answer`` question: the metrics skip
    those for recall and MRR and count them separately.
    """

    id: str
    question: str
    relevant: tuple[str, ...]
    kind: str

    @property
    def has_answer(self) -> bool:
        return bool(self.relevant)


@dataclass(frozen=True)
class EvalSet:
    notes: tuple[EvalNote, ...]
    questions: tuple[EvalQuestion, ...]

    def note(self, key: str) -> EvalNote:
        for note in self.notes:
            if note.key == key:
                return note
        raise KeyError(key)


@dataclass(frozen=True)
class EvalTurn:
    """One turn. ``relevant`` is None when an earlier turn is not labelled."""

    question: str
    relevant: tuple[str, ...] | None = None


@dataclass(frozen=True)
class EvalConversation:
    """A conversation whose last turn is the one scored.

    ``standalone`` is a hand-written, context-free rewrite of the last turn:
    the target a condenser should approach, and an upper bound for retrieval.
    """

    id: str
    kind: str
    turns: tuple[EvalTurn, ...]
    standalone: str

    @property
    def last(self) -> EvalTurn:
        return self.turns[-1]

    @property
    def relevant(self) -> tuple[str, ...]:
        return self.last.relevant or ()

    @property
    def has_answer(self) -> bool:
        return bool(self.relevant)


def load(notes_path: Path = NOTES_FILE, questions_path: Path = QUESTIONS_FILE) -> EvalSet:
    """Read, validate and return both fixture files."""
    notes = parse_notes(_read_json(notes_path), source=Path(notes_path).name)
    questions = parse_questions(
        _read_json(questions_path),
        note_keys={note.key for note in notes},
        source=Path(questions_path).name,
    )
    return EvalSet(notes=notes, questions=questions)


def load_conversations(
    path: Path = CONVERSATIONS_FILE, notes_path: Path = NOTES_FILE
) -> tuple[EvalConversation, ...]:
    """Read and validate conversations.json against the notes' keys."""
    notes = parse_notes(_read_json(notes_path), source=Path(notes_path).name)
    return parse_conversations(
        _read_json(path), note_keys={note.key for note in notes}, source=Path(path).name
    )


def _read_json(path: Path) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise FixtureError(f"{Path(path).name}: not valid JSON ({exc})") from exc


def parse_notes(data: Any, source: str = "notes") -> tuple[EvalNote, ...]:
    if not isinstance(data, list):
        raise FixtureError(f"{source}: expected a list of notes")
    notes, seen = [], set()
    for index, entry in enumerate(data):
        where = f"{source}[{index}]"
        _require_keys(entry, ("key", "type", "title", "content"), where)
        key = entry["key"]
        where = f"{source} note {key!r}"
        if not isinstance(key, str) or not key:
            raise FixtureError(f"{where}: key must be a non-empty string")
        if key in seen:
            raise FixtureError(f"{where}: duplicate key")
        seen.add(key)
        if entry["type"] not in NOTE_TYPES:
            raise FixtureError(f"{where}: type must be one of {sorted(NOTE_TYPES)}")
        if not isinstance(entry["title"], str) or not entry["title"].strip():
            raise FixtureError(f"{where}: title must be a non-empty string")
        validate_doc(entry["content"], where)
        if entry["type"] == "checklist" and not _contains(entry["content"], "taskItem"):
            raise FixtureError(f"{where}: a checklist note needs at least one taskItem")
        notes.append(EvalNote(key, entry["type"], entry["title"], entry["content"]))
    return tuple(notes)


def parse_questions(
    data: Any, note_keys: set[str], source: str = "questions"
) -> tuple[EvalQuestion, ...]:
    if not isinstance(data, list):
        raise FixtureError(f"{source}: expected a list of questions")
    questions, seen = [], set()
    for index, entry in enumerate(data):
        where = f"{source}[{index}]"
        _require_keys(entry, ("id", "question", "relevant", "kind"), where)
        qid = entry["id"]
        where = f"{source} question {qid!r}"
        if not isinstance(qid, str) or not qid:
            raise FixtureError(f"{where}: id must be a non-empty string")
        if qid in seen:
            raise FixtureError(f"{where}: duplicate id")
        seen.add(qid)
        if not isinstance(entry["question"], str) or not entry["question"].strip():
            raise FixtureError(f"{where}: question must be a non-empty string")
        kind = entry["kind"]
        if kind not in QUESTION_KINDS:
            raise FixtureError(f"{where}: kind must be one of {sorted(QUESTION_KINDS)}")
        relevant = entry["relevant"]
        if not isinstance(relevant, list) or not all(isinstance(k, str) for k in relevant):
            raise FixtureError(f"{where}: relevant must be a list of note keys")
        if len(set(relevant)) != len(relevant):
            raise FixtureError(f"{where}: relevant lists a note twice")
        unknown = sorted(set(relevant) - note_keys)
        if unknown:
            raise FixtureError(f"{where}: relevant names unknown notes {unknown}")
        # The kind and the label must agree, or the per-kind numbers lie.
        if (kind == "no_answer") != (not relevant):
            raise FixtureError(f"{where}: relevant is empty exactly when kind is no_answer")
        if kind == "multi_note" and len(relevant) < 2:
            raise FixtureError(f"{where}: a multi_note question needs two or more notes")
        questions.append(EvalQuestion(qid, entry["question"], tuple(relevant), kind))
    return tuple(questions)


def parse_conversations(
    data: Any, note_keys: set[str], source: str = "conversations"
) -> tuple[EvalConversation, ...]:
    if not isinstance(data, list):
        raise FixtureError(f"{source}: expected a list of conversations")
    conversations, seen = [], set()
    for index, entry in enumerate(data):
        where = f"{source}[{index}]"
        _require_keys(entry, ("id", "kind", "turns", "standalone"), where)
        cid = entry["id"]
        where = f"{source} conversation {cid!r}"
        if not isinstance(cid, str) or not cid:
            raise FixtureError(f"{where}: id must be a non-empty string")
        if cid in seen:
            raise FixtureError(f"{where}: duplicate id")
        seen.add(cid)
        kind = entry["kind"]
        if kind not in CONVERSATION_KINDS:
            raise FixtureError(f"{where}: kind must be one of {sorted(CONVERSATION_KINDS)}")
        if not isinstance(entry["standalone"], str) or not entry["standalone"].strip():
            raise FixtureError(f"{where}: standalone must be a non-empty string")
        raw_turns = entry["turns"]
        if not isinstance(raw_turns, list) or len(raw_turns) < 2:
            raise FixtureError(f"{where}: turns must be a list of at least 2 turns")
        turns = []
        for position, raw in enumerate(raw_turns, start=1):
            turn_where = f"{where} turn {position}"
            _require_keys(raw, ("question",), turn_where)
            if not isinstance(raw["question"], str) or not raw["question"].strip():
                raise FixtureError(f"{turn_where}: question must be a non-empty string")
            relevant = raw.get("relevant")
            if position == len(raw_turns) and relevant is None:
                raise FixtureError(f"{turn_where}: the last turn needs relevant")
            if relevant is not None:
                _check_relevant(relevant, note_keys, turn_where)
                relevant = tuple(relevant)
            turns.append(EvalTurn(raw["question"], relevant))
        if (kind == "no_answer") != (not turns[-1].relevant):
            raise FixtureError(
                f"{where}: the last relevant is empty exactly when kind is no_answer"
            )
        conversations.append(EvalConversation(cid, kind, tuple(turns), entry["standalone"]))
    return tuple(conversations)


def _check_relevant(relevant: Any, note_keys: set[str], where: str) -> None:
    if not isinstance(relevant, list) or not all(isinstance(k, str) for k in relevant):
        raise FixtureError(f"{where}: relevant must be a list of note keys")
    if len(set(relevant)) != len(relevant):
        raise FixtureError(f"{where}: relevant lists a note twice")
    unknown = sorted(set(relevant) - note_keys)
    if unknown:
        raise FixtureError(f"{where}: relevant names unknown notes {unknown}")


def validate_doc(doc: Any, where: str = "doc") -> None:
    """Check a TipTap document's shape; raise FixtureError on the first fault.

    The top level must be ``{"type": "doc", "content": [...]}`` with at least
    one block. Below it: every node has a known ``type``, ``content`` is a
    list when present, a text node has non-empty ``text`` and known marks, a
    heading has ``attrs.level`` 1-6, and a task item has a boolean
    ``attrs.checked``.
    """
    if not isinstance(doc, dict) or doc.get("type") != "doc":
        raise FixtureError(f'{where}: content must be a TipTap doc {{"type": "doc", ...}}')
    if not isinstance(doc.get("content"), list) or not doc["content"]:
        raise FixtureError(f"{where}: the doc needs a non-empty content list")
    for child in doc["content"]:
        _validate_node(child, where)


def _validate_node(node: Any, where: str) -> None:
    if not isinstance(node, dict):
        raise FixtureError(f"{where}: a node must be an object, got {type(node).__name__}")
    node_type = node.get("type")
    if node_type not in _NODE_TYPES or node_type == "doc":
        raise FixtureError(f"{where}: unknown node type {node_type!r}")
    path = f"{where} > {node_type}"
    attrs = node.get("attrs", {})
    if not isinstance(attrs, dict):
        raise FixtureError(f"{path}: attrs must be an object")

    if node_type == "text":
        if not isinstance(node.get("text"), str) or not node["text"]:
            raise FixtureError(f"{path}: a text node needs non-empty text")
        if "content" in node:
            raise FixtureError(f"{path}: a text node has no content")
        for mark in node.get("marks", []):
            if not isinstance(mark, dict) or mark.get("type") not in _MARK_TYPES:
                raise FixtureError(f"{path}: unknown mark {mark!r}")
        return
    level = attrs.get("level")
    if node_type == "heading" and (type(level) is not int or not 1 <= level <= 6):
        raise FixtureError(f"{path}: a heading needs attrs.level 1-6")
    if node_type == "taskItem" and not isinstance(attrs.get("checked"), bool):
        raise FixtureError(f"{path}: a taskItem needs a boolean attrs.checked")

    content = node.get("content", [])
    if not isinstance(content, list):
        raise FixtureError(f"{path}: content must be a list")
    for child in content:
        _validate_node(child, path)


def _contains(node: dict, node_type: str) -> bool:
    if node.get("type") == node_type:
        return True
    return any(_contains(child, node_type) for child in node.get("content", []))


def _require_keys(entry: Any, keys: tuple[str, ...], where: str) -> None:
    if not isinstance(entry, dict):
        raise FixtureError(f"{where}: expected an object")
    missing = [key for key in keys if key not in entry]
    if missing:
        raise FixtureError(f"{where}: missing {missing}")
