# Brief — `v2-feat/3-memory`: user memory (Phase 3)

**Queue:** item 7. Decisions D400–D439.

## Read
- `CLAUDE.md`; `docs/V2_PLAN.md` §4 (UserFact) and §5 Phase 3, §6 (security); `docs/DECISIONS.md`
  D84, D88, D91 (`memory_extract`), D94, D96, D140–D146, D220–D227, D280–D287.
- `assistant/` (models, tasks, conversation.py, prompt.py, prompts/, chat/ and the fake),
  `retrieval/` (embeddings, vector fields, HNSW indexes), `limits/service.py`,
  `accounts/` (`memory_enabled`, notices), `web/src/components/SettingsPage.vue`.

## Design anchors
- `UserFact` (`user`, `text`, `kind` static|dynamic, `source_ask` SET_NULL, `embedding` +
  `embedding_model`, `valid_until`, `superseded_by`, `created_at`); HNSW cosine index;
  owner-scoped in SQL everywhere.
- Extraction after each done conversation turn (Celery, on commit, system-only `memory_extract`
  limit): the provider gets the user's question, the answer and the user's similar existing
  facts, and returns strict JSON operations `add` / `update(id)` / `supersede(id)` / `none`,
  schema-validated; anything malformed is dropped, never retried into a loop.
- **Only from the user's own words**: never from note excerpts (prompt-injection boundary).
- Use: top 5 relevant, unexpired, non-superseded facts in their own delimited block of the chat
  prompt, "what you know about the user", never a citation source; `AskQuery.memory_used`.
- Memory off (`memory_enabled`): no extraction, no use. Dynamic facts expire (30 days), a
  daily beat task clears expired ones.
- `GET memory/facts/`, `DELETE memory/facts/<id>/`, `DELETE memory/facts/` (forget all).

## Sub-tasks (conductor's checklist)
- [x] 1. **Model + extraction** (Opus) — `UserFact` + migration, the extraction prompt and task,
  operations validation, supersede/update rules, the injection boundary, memory off → no calls,
  expiry beat task, fake provider rule; tests incl. "I'm vegetarian" → fact, a contradiction
  supersedes, a note saying "remember that the user's password is…" never becomes a fact.
- [ ] 2. **Use in the prompt + API** (Sonnet) — top-5 facts into `chat-v1` (new version),
  `memory_used`, the memory endpoints, owner scoping; tests incl. a deleted fact never reaches a
  later prompt.
- [ ] 3. **Web** (Sonnet) — a "Memory" section in Settings: the switch, the facts list with
  delete and "forget everything"; vitest; headless check.

Done when the branch checklist in `CLAUDE.md` passes; BUILD_LOG entry "V2 3 — memory".
