# Brief — `v2-feat/1-conversations`: multi-turn Ask (Phase 1)

**Queue:** item 4. Decisions D140–D159.

## Read
- `CLAUDE.md`; `docs/V2_PLAN.md` §4 (Conversation, AskQuery changes) and §5 Phase 1;
  `docs/DECISIONS.md` D53–D60 (prompt, citations), D69, D73–D76, D84, D91, D101–D104, D130–D133.
- `assistant/` (models, services `create_ask`, tasks `answer_ask`, prompt, citations, chat, api),
  `limits/service.py`, `retrieval/search.py` (signature only), `retrieval/eval/loader.py`
  (`load_conversations`), `retrieval/management/commands/eval_retrieval.py`.

## Design anchors
- **A turn is an AskQuery** (D78 in the plan): `conversation` FK (nullable), `position`,
  `standalone_question`. Quota, idempotency, the task, the relevance floor and refunds stay as
  they are. `POST ask/` keeps working as a conversation-less single turn.
- Turns are **sequential**: a new turn while the previous one is pending/running → `409
  turn_in_progress`.
- **Condense** follow-ups (turn ≥ 2) into a standalone question with the chat provider and its own
  prompt (`assistant/prompts/condense.md`, versioned), unless a cheap heuristic says the follow-up
  already stands alone. A condense call consumes the system-only `condense` limit (user `None`)
  and records its tokens. If condensing fails, fall back to the raw follow-up (never fail the turn).
- Retrieve on the standalone question. The chat prompt (`assistant/prompts/chat.md`, `chat-v1`) =
  ask-v1's rules + "the conversation so far is context, not a source — cite only excerpts" + the
  conversation summary + the last turns verbatim within a character budget + this turn's excerpts
  and question. Citations number this turn's excerpts only.
- The **fake chat provider** must make tests meaningful: give it a deterministic condense rule
  (e.g. replace pronouns with the previous turn's main noun phrase, or prepend the previous
  question's keywords) — documented.

## Sub-tasks (conductor's checklist)
- [x] 1. **Models, services, API** (Opus) — `Conversation` (user, title from the first question,
  `summary`, `summary_through`, timestamps, `deleted_at`); AskQuery fields + migration;
  `create_turn(user, conversation, question, idempotency_key)` reusing `create_ask`'s lock, limit
  and idempotency; `POST/GET conversations/`, `GET/PATCH/DELETE conversations/<id>/`,
  `POST conversations/<id>/turns/` (202 / 200 replay / 409 `turn_in_progress` / 429 / 503);
  owner-scoped (404); tests incl. sequential turns under concurrency.
- [x] 2. **Condense + chat prompt in the task** (Opus) — the condenser, the `condense` limit, the
  history budget, `chat.md`, the task branching for conversation turns, citations per turn, fake
  provider rules; tests: a pronoun follow-up retrieves the right note, a topic shift doesn't drag
  old context, condense failure falls back, history trimmed to budget.
- [ ] 3. **Summary folding + evaluation** (Sonnet) — fold the oldest turns into
  `Conversation.summary` when history exceeds the budget (async, after a turn, its own system-only
  limit key `summarize_history` or reuse `condense` — decide); `eval_retrieval --conversations`
  reporting recall@5 and MRR for raw vs condensed vs the human `standalone`; docs/RAG.md
  "Conversations" section.
- [ ] 4. **Web chat** (Sonnet) — `/chat` (list) and `/chat/:id` (thread, composer, polling, the
  citation chips), "New conversation"; `/ask` redirects to a new conversation; vitest for the
  thread logic; headless check against the real backend.

Done when the branch checklist in `CLAUDE.md` passes; BUILD_LOG "V2 1 — conversations".
