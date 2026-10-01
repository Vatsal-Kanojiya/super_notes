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

### Phase 1 — auth

**Built:** Google sign-in with a list of audiences (`accounts/google.py`), matched on `sub` then
email; `auth/google/`, `auth/refresh/` (rotate + blacklist), `auth/logout/`, `me/`
(`accounts/api.py`). The reference's security layer, Google-only: the `SecurityEvent` trail with
a read-only admin and a daily retention purge, the per-address cap on failed sign-ins plus the
DRF `auth` throttle, and the two-device limit with `GET auth/devices/` and
`DELETE auth/devices/<id>/`. Decisions D13–D22. `accounts/` at 100% coverage.

**What went wrong / notes:**

- simplejwt lets two requests with the same refresh token both rotate it (both pass the
  blacklist check before either writes). Found while wiring the device limit, where it also let a
  replayed token push a real device out. Closed with a row lock (D20) and a threaded test that
  fails without it.
- simplejwt's refresh looks the user up with a bare `.get()`: a token outliving its account was a
  500. Now a 401.
- Tests verify really signed tokens against a throwaway certificate instead of mocking the
  verifier (D14), so the audience-list and expiry checks are the library's own.

**Left:** nothing in the phase. Ask usage on `me/` is phase 5 (a comment marks the place).

**Unsure:** whether 30/hour on the `auth` scope is enough once web and Android both refresh from
one home address (D21); whether refusing a re-used email on a new `sub` (D13) will bite anyone.

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

### Phase 3a — chunker and embedding providers

**Built:** `retrieval/chunking.py` (structure-aware TipTap chunker: heading paths, atomic list and
checklist items, whole-block overlap, title-prefixed `embed_text`, sha256 `content_hash`), and
`retrieval/embeddings/` mirroring the reference's extraction package: boundary (`embed_texts`,
`embed_query`, `embedding_model_id`), lazy registry, Protocol, a hashed-bag-of-words fake, and
OpenAI and Gemini providers over plain `requests`. Settings `CHUNK_*` and `EMBEDDING_*`,
`.env.example`, `docs/RAG.md` (Chunking, Embedding providers). D33–D40. No models, tasks or
migrations: `NoteChunk` and indexing are Phase 3b.

**What went wrong / notes:**

- A bogus-key probe of each endpoint (through the live tests) showed OpenAI's 401 message quotes
  the key it was sent. Vendor messages now have header values redacted before they reach an
  exception.
- The same probes confirmed both URLs and auth headers exist as written (OpenAI 401, Gemini 400
  "API key not valid", neither a 404). The request bodies and responses are from memory of the API
  references and checked only by mocks.
- The test runner's forced `fake` was verified by running with `EMBEDDING_PROVIDER=openai` in the
  environment; `override_settings` in the live tests beats it.

**Left:** `NoteChunk`, `index_note`, `reindex_notes`, `index_status` (Phase 3b).
**Unsure:** real-key behaviour of both providers (Gemini's 100-per-batch limit, response shape);
chunk sizes until the Phase 4 eval measures them.

### Phase 3 — indexing

**Built:** `retrieval.NoteChunk` (HNSW cosine, `(owner, note)` and a GIN full-text index from
`chunk_search_vector()`), `retrieval/indexing.py` (`index_note`, `deindex_note`), `index_note_task`
(retry on transient, drop on permanent), the on-commit hook in `_after_write` with
`INDEX_DEBOUNCE_SECONDS`, `reindex_notes`, `index_status`, a read-only admin, tests at 100% of
`retrieval/`. D61-D65.

**What went wrong / notes:** the plan reuses chunks "by hash"; repeated paragraphs share a hash, so
matching is first-come in ordinal order and one embedding serves all copies. A duplicate delivery can
change rows between the first read and the write, so rows are re-read under the lock.

**Left:** nothing for phase 3; search over the chunks is phase 4.
**Unsure:** whether 20 s is the right debounce; a real worker and Redis were not run here (tests are
eager), so the countdown is checked only by asserting the `apply_async` call.

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

### Phase 4 — retrieval and evaluation

**Built:** `retrieval/search.py` (vector and keyword legs, both owner-scoped in SQL; pure RRF
fusion and per-note cap; `SearchHit` with the fused score, cosine `similarity` and `ts_rank`),
`GET /api/v1/search/` (`search` throttle scope, snippet), `eval_retrieval` (rolled-back run of the
fixtures in all three modes, `--by-kind`, no-answer similarities). "Retrieval" and "Evaluation" in
`docs/RAG.md` (the draft merged and deleted). D66–D71.

**What went wrong / notes:**

- `ORDER BY distance, id` made the HNSW index unusable; the query now orders by distance alone and
  ties are broken in Python.
- EXPLAIN on a small table shows the owner btree for both legs, legitimately: an index only pays
  off when the owner has far more chunks than the limit. The index tests build 1000 chunks and
  `ANALYZE` first; with a small owner share the planner scans that user's rows exactly (D68).
- Fake-provider smoke run: keyword-only recall@5 is 0.179 because `websearch_to_tsquery` ANDs every
  word of a question. Kept as specified; noted in RAG.md "Known limits" with an OR experiment.

**Left:** real-provider numbers (needs a key), and from them `ASK_RELEVANCE_FLOOR` (Phase 5).
**Unsure:** AND vs OR keyword semantics; whether 200 is the right `ef_search`.

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

### Phase 5 — ask

**Built:** `AskQuery` (unique `(user, idempotency_key)`, read-only admin); `assistant/quota.py`
(Kolkata calendar month, non-failed rows); `create_ask` (user lock → key lookup → count → create →
enqueue on commit); the `answer_ask` task (hybrid search, relevance floor, prompt, provider,
citations, retries, every exit done or failed); `POST/GET ask/`, `GET ask/<id>/`;
`me/` `ask_usage`. The Ask draft merged into `RAG.md` "Asking" and deleted. D73–D76.

**What went wrong / notes:**

- With eager Celery and `task_eager_propagates`, `on_failure` never runs and a retry raises
  instead of re-running. Failure handling moved into the task body (D76); the retry tests call
  `apply()` directly (`throw=False`, or starting at the last retry).
- The quota-edge test forces the race with the lock removed (both threads held after counting) to
  show it would catch a missing lock.
- `me/`'s shape grew `ask_usage`, and so did the sign-in response's `user` (same serializer); two
  accounts tests updated.
- The test runner now sets `celery.app.trace` to WARNING: one INFO line per eager task was noise.

**Left:** `ASK_RELEVANCE_FLOOR`'s value (real-provider eval), the stuck-ask sweeper (BACKLOG).
**Unsure:** 200 rather than 202 for a replayed key (D75); letting any keyword match pass the floor
(D74) until the eval says otherwise.

### Phase 6a — web client first pass

**Built:** `web/`, a Vue 3 + Vite + TypeScript client with Pinia and TipTap: Google sign-in (GIS
button), a notes list with server-side search and a type filter, a TipTap editor (text and
checklists) that autosaves about a second after typing with the note's `version` and prompts
"keep mine / take theirs" on a 409, revision-based sync through `notes/changes/` (on focus, on
becoming visible, every 30 s), an Ask panel with `[n]` citation chips that open the source note
and the monthly usage line, and a devices list in the account menu. All payload types are in
`web/src/api/types.ts`. CI gains a `web` job (Node 24, `npm ci`, `npm run build`). D46–D52.

**Built against the contract, not the real API.** The backend's auth, notes, search and ask
endpoints were being written in parallel, so the client follows plan §7 and the coordinator's
shapes. It has **not been run against the real API yet**. It was smoke-tested in headless Chrome
at phone width against a throwaway mock of the contract (not committed): a stale access token
refreshed and retried; the list loaded from `changes?after=0`; an edit autosaved; an edit from
"another device" produced the conflict prompt and "keep mine" saved; an ask was accepted with an
`Idempotency-Key`, polled to done, rendered two chips (a `<script>` in the answer stayed text),
and a chip opened its note; the next ask got the quota message.

**What went wrong / notes:** the scaffold's `.vscode/` folder is ignored by the root
`.gitignore` and was dropped. The title field's style was overridden by the generic input rule
(fixed).

**Left:** everything in BACKLOG under "Web:". **Unsure:** the API assumptions listed there as
"contract reconciliation" — in particular device sign-out's method, the ask status values, and
whether `changes` tombstones carry `deleted_at`.

### Phase 6 — web client against the real API

**Built:** the first pass (6a) was written against the plan's contract; 6b and 6c reconciled
`web/src/api/types.ts` with `docs/openapi.yml` and ran the client in headless Chrome against the
real backend and a real Celery worker. Checked: notes list, create, autosave with `version`, the
conflict prompt ("keep mine"), checklists, delete, sync through `changes`, keyword search,
devices, and Ask end to end — a cited answer, the chip opening its note, usage, the quota and
throttle messages, and the fixed no-answer text.

**Went wrong:** nothing in the client beyond type drift. Two real fixes came out of it: a 429 or
5xx on token refresh used to sign the user out (now only a 400/401 does), and the sign-in call made
a needless `me/` request once `user` carried `ask_usage`.

**Left:** Google sign-in was never exercised (no client id); tokens were minted in the Django
shell. The no-answer branch only fires for a user whose notes share no word with the question
while `ASK_RELEVANCE_FLOOR` is 0.0 — expected until the real-provider evaluation (D58, D74).

**Unsure:** tokens in localStorage (D47) is the standing trade-off to revisit before a public
launch.

### Integration — merging the parallel slices (2026-10-01)

Phases 1, 2 and the first halves of 3–6 were built by parallel agents in separate worktrees, then
rebased onto `master` one at a time. A usage limit stopped four of them mid-work; each was resumed
and finished before merging. Conflicts were all additive (settings blocks, URL mounts, the docs),
resolved by keeping both sides and putting DECISIONS and this log back in phase order.

**Tags:** because phase 2 merged before phase 1, the history is not phase-ordered.
`phase-1-auth` and `phase-2-notes` both point at the first commit where both are complete, so
`git diff phase-0-scaffold phase-2-notes` shows the two together. From phase 3 on, phases land in
order again.

**Owner decisions (2026-10-01):** keyword search stays English (D24); device limit stays 2 (D6);
push at each phase tag; providers stay modular — boundary function, settings-selected registry,
one adapter per vendor — with fake providers until keys are set.

### V2 0a — hardening

Six small items on `v2-feat/0a-hardening` (D78-D83), each with tests and its BACKLOG entry removed:
a stuck-ask sweeper (every 5 minutes, cutoff 3,600 s from the retry span); deeply nested JSON is a
400 `parse_error`; the 413 carries CORS headers for allowed origins; Google's signing keys are cached
for their `max-age` (capped at 1 h, with a fresh-fetch retry on failure); the chunker no longer
splits after common abbreviations (chunks of long blocks that contain them re-embed on next index);
and the tab-hide save uses `fetch` `keepalive` for bodies up to 60 KiB. The web client has no test
runner, so item 6 is verified by the build only.

### V2 — multi-turn eval fixtures

`retrieval/eval/fixtures/conversations.json`: 17 follow-up cases (pronoun, ellipsis, topic shift, refinement, near-duplicate, no-answer) over the existing 30 notes, each with a hand-written standalone rewrite; loader validation and tests added, the raw-vs-condensed comparison waits for phase 1's command.

### Native Postgres verified (2026-10-02)

The owner installed `postgresql-16` and `postgresql-16-pgvector` from Ubuntu's archive and created a
superuser role for their login. `createdb super_notes`, `migrate` (which created pgvector 0.6.0)
and the full suite (620 tests) then ran with `DATABASE_URL=postgres:///super_notes` over the local
socket; `runserver` answers `health/`. The README's native setup is now verified on a real install,
and the session-only stand-in on port 5433 is gone. One snag: copying the install command from
chat picked up a trailing full stop (`postgresql-16-pgvector.`), which apt reads as part of the
package name.


### V2 0e — lifecycle

`v2-feat/0e-lifecycle` (D88-D90, D93, D94, D105-D111). `User` gained `timezone`, `memory_enabled`,
`memory_choice_explicit`, `app_open_count` and `memory_notice_seen_at_open`; `PATCH me/` sets the
first two (timezone checked against IANA names; a `memory_enabled` write marks the choice as the
user's own). Two signals in `accounts/signals.py`, sent with `send_robust`: `user_signed_in` from
`issue_tokens` on every Google sign-in, `app_opened` from `POST session/open/`. `GET app/version/`
is public and unthrottled; `X-Client-Min-Version` rides on every `/api/` response and CORS exposes
it. `session/open/` counts an open per device (throttled by `SignedInDevice.last_app_open_at`,
default 300 s, race-safe), returns the update notice and the D88 memory notice (prominent / subtle,
on / off), due on the first open and every 5 opens after the user last saw it;
`me/memory-notice/seen/` records that. A device id from another account's token is ignored. New
settings `CLIENT_LATEST_VERSION`, `CLIENT_MIN_VERSION`, `MEMORY_NOTICE_EVERY_OPENS`,
`APP_OPEN_MIN_INTERVAL_SECONDS`.

### V2 0d — web platform

`v2-feat/0d-web-platform` (D86, D89, D90, D93, D94, D112-D123). `vue-router` replaces the view
store: `/notes`, `/notes/:id`, `/ask`, `/settings` and a public `/signin`, with a guard that
sends signed-out users to sign-in (returning them by a same-site `next`), lazy route components,
and back / refresh / deep links working. `vitest` with 69 tests (citations, token-refresh
single-flight, notes sync, `safeNext`, build ids and the update decision, the idle/resume tracker,
notice handling); `npm test` is in the CI web job. The notes sync now resyncs from 0 when the
server's revision goes backwards, as D25 always said. Builds carry an id
`YYYYMMDDHHMM-<shortsha>`; the client checks `app/version/` on load, focus and every 5 minutes,
reads `X-Client-Min-Version`, shows a "New version" bar and reloads when no edit is unsaved, and
reloads once on a failed chunk load; `web/README.md` has the hosting rules (immutable hashed
assets, `no-cache` index, SPA fallback). Lifecycle: `session/open/` on launch and on the first
interaction after 5 idle hours, the memory banner (prominent or subtle, "Review / turn off" to
the new settings page, dismiss marks it seen), the update notice feeding the same update logic,
a memory on/off toggle, and the browser timezone sent for users still on the default.
Checked in headless Chrome against the real backend: guarded redirect, deep link, refresh, back,
unknown path, prominent banner on the first open, settings toggle (`PATCH me/`), timezone set to
Europe/Berlin, and the update bar with `CLIENT_LATEST_VERSION` set to a future build.
