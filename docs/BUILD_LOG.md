# Super Notes — Build log

> What each phase actually took and what went wrong. Companion to
> [COMMIT_PLAN.md](COMMIT_PLAN.md) and [DECISIONS.md](DECISIONS.md).

---

## Session 1 — 2026-09-30

### Phase 0 — scaffold

**Built:** the Django project from the reference's conventions (`docs/CONVENTIONS.md`): one
settings module, the security block, request ids, CSP, the 413 guard, JSON-only Celery,
JWT-only DRF with cursor pagination and `{detail, code}` errors, drf-spectacular, the custom
`User` (email-keyed, `google_sub`, `plan`, `notes_revision`) in the first migration, and pgvector
enabled by migration. ruff, coverage, pre-commit, CI with a pgvector service, Dependabot and
`SECURITY.md`.

**What went wrong / notes:**

- PostgreSQL was not installed on the machine, and installing it needs sudo. For the session, the
  Ubuntu `postgresql-16` and `postgresql-16-pgvector` packages were unpacked (`apt-get download` +
  `dpkg -x`) into a private directory and run as the normal user on port 5433. The suite ran
  against that. **The README's native setup (apt install) is still the real path** and is untested
  on this machine until the owner runs it.
- Its Unix socket path was too long for Postgres (107-byte limit), so it listens on TCP only.
- The distro's pgvector is 0.6.0, which has HNSW (0.5.0+). Enough for the plan.
- CI has not run yet: the first push happens with this phase's tag.

**Left:** nothing in the phase. **Unsure:** coverage bar (D12).

### Phase 2 — notes and sync

**Built:** the `notes` app. `Note` with `(owner, revision)`, `(owner, -id)` and a GIN full-text
index on `title` + `content_text` (`english`, D24). `notes/content.py` derives `content_text` from
the TipTap document and validates its shape (D23). `notes/services.py` is the only writer: it locks
the owner's row, takes the next `notes_revision`, bumps `version`, and calls `_after_write(note)`,
a documented no-op where phase 3 enqueues indexing on commit. API: list (cursor, `type`, `q`),
create, retrieve, PATCH with `version` → `409 version_conflict` + server copy, soft DELETE,
`changes?after=&limit=` with tombstones and `has_more` (D25). Read-only admin (D29). Tests cover
ownership on every endpoint, derivation, conflicts, tombstones, exact `changes`, search (title,
body, stems, other users), the content cap, ignored server fields, an EXPLAIN proving the GIN index
is used, and threaded `TransactionTestCase`s for revision order. `notes/` is at 100% coverage.

**What went wrong / notes:**

- The first "commit order" concurrency test recorded each revision *after* `create_note`
  returned, which races (a thread can be descheduled between commit and append). It now records
  from `_after_write`, inside the lock. Removing `select_for_update` makes both ordering tests
  fail, so they do test the lock.
- A JSON body nested a few thousand levels deep makes DRF's `JSONParser` raise `RecursionError`,
  a 500, before any serializer runs. Platform-wide, so parked (BACKLOG), not patched here.

**Left:** indexing (phase 3 fills `_after_write`). **Unsure:** `english` vs `simple` (D24) depends
on what language the owner writes in.

### Phase 4a — evaluation fixtures

**Built:** `retrieval/eval/`: 30 TipTap fixture notes and 31 labelled questions (eight kinds,
including near-duplicate, multi-note and three no-answer questions), a loader that validates
both files into frozen dataclasses, and pure recall@k / MRR functions that score note-level
rankings (D41–D45). Draft "Evaluation" section in `docs/RAG_EVAL_DRAFT.md`, to be merged into
`docs/RAG.md`. No DB, no models: search is being built in parallel.

**What went wrong / notes:** nothing blocking. The fixtures were written with a throwaway
generator (not committed; the JSON is the source of truth, D41).

**Left:** `eval_retrieval` (needs search), a per-kind breakdown in its output, and the real
provider's numbers in the results table. **Unsure:** whether 31 questions are enough to separate
the modes — one question moves recall by ~3.6 points, so small differences are noise.

### Phase 5a — chat providers and prompt

**Built:** the `assistant` app skeleton; `assistant/chat/` mirroring the reference's extraction
package (`complete()` → frozen `ChatResult`, `ChatError` / `TransientChatError`, lazy registry,
Protocol) with a grounded-looking fake and Claude, OpenAI and Gemini providers over plain
`requests`; the versioned prompt `assistant/prompts/ask.md` and `build_messages` with protected
`<excerpt>` delimiters and a character budget; `[n]` citation parsing; the Ask settings block.
Docs in `docs/RAG_ASK_DRAFT.md` (to merge into `RAG.md`). D53–D60.

**What went wrong / notes:**

- The session was cut off by a usage limit after the code commits; docs finished on resume.
- The Claude default uses the alias `claude-haiku-4-5` (the claude-api skill's current ID) rather
  than the dated `claude-haiku-4-5-20251001` from the brief. Both are valid.
- In commit `23c95b8` the registry test imports the real providers, which land one commit later;
  that single commit is not green on its own.
- No live test has run: no vendor key on this machine.

**Left:** the `AskQuery` model, the ask task (retrieval, floor short-circuit, `fit_excerpts` →
`build_messages` → `complete` → `parse_citations`), quota, idempotency and the API — Phase 5b.

**Unsure:** the relevance floor's score scale (D58); whether live tests should need an extra
opt-in flag besides the key (BACKLOG).
