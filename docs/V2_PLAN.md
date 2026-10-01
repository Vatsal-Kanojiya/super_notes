# Super Notes — V2 Plan

Brief for the build. Read it end to end before touching anything. Same working method as V1
(`docs/CONVENTIONS.md` §7), except for branching (§1): V2 is built on its own line, feature by
feature.

**The headline of V2 is "Chat with your notes":** Ask becomes a conversation that remembers what
was said and what it has learned about you, and answers stream in as they are written. Around it,
the notes start to work for you: an AI "format my note" button, reminders with a calendar, and
files attached to notes that are read, summarised and searchable like text.

V1 proved retrieval (hybrid search, measured), the job pattern (row → on-commit task → poll) and
the provider layers. V2 reuses all three; nothing in V1 is rewritten.

---

## 0. Where V1 left off

- Phases 0–6 are tagged on `master` (`phase-0-scaffold` … `phase-6-web`). 578 tests, 99% backend
  coverage, CI green.
- **Not done in V1, on hold by the owner:** real Postgres on the dev machine, real AI keys and the
  real-provider evaluation, the Google OAuth client id, and Phase 7 (Android).
- V2 does not depend on any of them to be *built*. Features that need a real model to be *judged*
  (condensing follow-ups, fact extraction, formatting) ship with fake providers and an evaluation
  set, and are measured once a key exists — exactly as V1's retrieval was.

---

## 1. Branching and release workflow (D77)

```
master ──●────────────────────────────────────────────●── v2.0.0
          \  (V1 hotfixes merge down into v2)          /
v2 ────────●──────●──────────●──────────●──────────────●
            \    / \        / \        /
             ●──●   ●──●──●    ●──●──●
        v2-feat/0-foundation  v2-feat/1-conversations …
```

- **`master`** is the released V1 line. Only fixes land there from now on (a V1 bug, a security
  update). Every fix on `master` is merged into `v2` the same day (`git merge master` on `v2`),
  so the two never drift.
- **`v2`** is V2's integration branch, cut from `master` at `phase-6-web`. It is always green: CI
  runs on every push to it. Nothing is committed on `v2` directly except merges and the
  release-notes/doc updates that close a phase.
- **Feature branches** `v2-feat/<phase>-<slug>` (not `v2/…`: git cannot hold a branch `v2` and
  branches under `v2/` at once) (e.g. `v2-feat/1-conversations`), cut from the current `v2`.
  One phase per branch; a large phase may split into two or three branches
  (`v2-feat/1a-conversation-model`, `v2-feat/1b-condense`). Conventional Commits inside, one reviewable idea
  per commit, as in V1.
- **Merging:** a branch is merged into `v2` with `git merge --no-ff` only when its checks pass
  (§9) and it has been reviewed. The merge commit is titled `merge: v2-feat/<branch> — <one line>`.
  `--no-ff` keeps each feature visible as a unit in `git log --graph` and makes reverting a whole
  feature one `git revert -m 1`.
- **Keeping a branch current:** rebase an *unpushed* feature branch onto `v2`; once pushed, merge
  `v2` into it instead (never rewrite pushed history).
- **Tags:** each merged phase is tagged on `v2`: `v2-phase-0-foundation`, `v2-phase-1-conversations`,
  … Practice branches (`practice/v2-<phase>`) are cut from the previous tag, as in V1.
- **Release:** when §10 is met, `v2` is merged into `master` (`--no-ff`) and tagged `v2.0.0`.
  `master` then becomes the V2 line and `v2` is deleted (or kept frozen). V1's last state stays at
  `phase-6-web` (or `phase-7-android` if Android lands on `master` first).
- **CI:** `.github/workflows/ci.yml` runs on pushes to `master` and `v2`, and on every pull request
  (feature → `v2` included). Dependabot keeps targeting `master`; its merges flow down to `v2`.
- **Pull requests** are optional: branches can be merged locally after review and pushed, or opened
  as PRs against `v2` on GitHub for a record. Either way the review happens before the merge.

This replaces V1's D1 (linear `master`, no feature branches) for V2 only.

---

## 2. Scope

### In (recommended)

| # | Feature | Why now |
|---|---|---|
| 0 | **Foundation**: V1 debt that V2 builds on — router + deep links, web tests, stuck-ask sweeper, JSON-depth 400, 413 with CORS, a usage ledger | Conversations need URLs; every new AI feature needs metering |
| 1 | **Conversations** — multi-turn Ask with follow-up condensation and per-turn citations | The long-term goal named in V1 |
| 2 | **Streaming answers** over server-sent events, polling kept as the fallback | Chat without streaming feels broken |
| 3 | **User memory** — facts learned from conversations, visible and deletable | What makes conversations *yours* |
| 4 | **"Format my note"** — AI restructures a note, shown as a preview you accept | Small, high-visibility, reuses everything |
| 5 | **Reminders and calendar** — on a note, delivered by email and web push, a month/week view | The first feature where notes come to you |
| 6 | **Attachments and summaries** — images and PDFs on notes, text extracted and indexed, note summaries | Makes Ask cover what isn't typed |

### Later (V3), and why

| Feature | Why not V2 |
|---|---|
| Sharing and collaboration | Breaks the one-owner invariant that every query and test relies on (D67); needs its own design for permissions, sync and search |
| Payments | `plan` is admin-edited; nothing to charge for until V2's features exist |
| Offline-first storage | A client rewrite (local database, merge rules); V1's version conflicts are the stepping stone |
| Desktop packaging | Low value while the web client works in a browser |
| Real-time collaboration / websockets | Not needed without sharing; SSE covers streaming |

### Open questions for the owner (answer before the phase that needs it)

**Settled 2026-10-02:** 1 → two-layer limits, user and system, through one limits layer (D84);
2 → email + web push (D87); 3 → S3 in production, local disk in development (D85); 4 → memory on
by default, with recurring notices (D88). Also added by the owner: no stale cached JavaScript
ever (D89) and lifecycle hooks for sign-in and app open/resume (D90), both in phase 0.
5 → Android after V2 (D97). Limit values D91, app-open rule D93, memory-notice cadence D94,
reminder schedule D95, chat history kept until deleted D96. **All open questions are settled.**

### Parked for an evening session (needs real discussion)

1. **Hosting and deployment target** (VPS, a platform, …) — affects S3, `uvicorn`, web push
   (VAPID, HTTPS), the Google client id origins and the release of `v2.0.0`.
2. **Android in detail** — Play Store or sideload, signing, the Android Google client id
   (after V2, D97).
3. **Refinements the owner asked to revisit:** system limit multipliers (D91), the 5-hour idle
   rule for "app open" (D93), the memory-notice cadence (D94), reminder snooze/stop and other
   cadences (D95).
4. **A limit's "enabled" switch (D100):** recorded as "off = not enforced, usage still counted".
   The other reading is "off = feature paused for everyone". Confirm.
5. **Deploy wiring for D89:** the deploy must set the server's `CLIENT_LATEST_VERSION` to the same
   build id as the web bundle it ships (set `VITE_APP_VERSION` once, pass it to both), or clients
   could reload toward a build whose files aren't served yet. Part of the hosting discussion.

1. **Quota model (before phase 1).** One monthly budget of *AI actions* shared by asks, chat
   turns, formatting and summaries (recommended: simpler to explain, one ledger), or a separate
   quota per feature as V1 has for asks?
2. **Reminder delivery (before phase 5).** Email + web push (recommended), or email only for now?
   Web push needs VAPID keys and a service worker; Android gets native notifications in its phase.
3. **File storage (before phase 6).** Local disk in V2 (recommended while there is no deployment),
   or S3-compatible storage now (adds `django-storages` and a bucket)?
4. **Memory default (before phase 3).** User memory on by default with a visible switch
   (recommended), or opt-in?
5. **Android timing.** Finish V1 Phase 7 on `master` first (recommended once the client ids
   exist — V2's reminders then reuse its notification plumbing), or fold it into V2?

---

## 3. Dependencies (each needs the owner's approval before its phase)

| Package | Phase | For | Alternative if refused |
|---|---|---|---|
| `uvicorn` | 2 — **approved** (D92) | An ASGI server so an async view can hold a streaming response open without a worker thread each | `StreamingHttpResponse` under WSGI (one thread per open stream — acceptable for dev, not for users) |
| `vue-router` (web) | 0 — **approved** (D86) | URLs for notes and conversations, the back button, deep links (Android needs them) | Keep D46's view store and hash-parse by hand |
| `vitest` (web, dev) | 0 — **approved** (D86) | Unit tests for the client's sync loop, token refresh and citation rendering | None — the client stays untested |
| `pywebpush` | 5 — **approved** (D87) | Web push delivery (VAPID signing, payload encryption) | Email-only reminders |
| `pypdf` | 6 — **approved** (D92) | Text from PDFs | Send PDFs to a vision model (costly, slower) |
| `django-storages` + `boto3` | 6 — **approved** (D85) | S3-compatible storage | Local disk |

No other new dependency without asking. Providers stay on plain `requests` (D37, D53).

---

## 4. Data model changes

**User** — add `timezone` (IANA name, default `Asia/Kolkata`; reminders and the quota month use
it), `memory_enabled` (default on, D88), `memory_choice_explicit`, `app_open_count`,
`memory_notice_seen_at_open` (D94).

**Limit** (`limits`) — `key`, `user_free`, `user_premium`, `system`, `period`, `enabled` (D84,
values D91).

**Conversation** (`assistant`)
- `user` FK, `title` (derived from the first question, editable), `summary` (running summary of
  turns that no longer fit the prompt), `summary_through` (last turn position it covers),
  `created_at`, `updated_at`, `deleted_at` (soft delete).
- Index `(user, -updated_at)`.

**AskQuery** (existing — becomes a *turn*)
- Add `conversation` FK (nullable: `POST ask/` keeps working as a one-turn conversation-less ask),
  `position` (turn number within the conversation), `standalone_question` (the condensed
  follow-up actually searched), `memory_used` (JSON: fact ids put in the prompt).
- Unique `(conversation, position)`. Quota, idempotency, the task and the relevance floor are
  unchanged — a turn *is* an ask. This is the central decision of V2 (D78): no second job model.

**UserFact** (`assistant`)
- `user` FK, `text`, `kind` (`static` | `dynamic`), `source_ask` FK (SET_NULL), `embedding`
  (VectorField, same dimensions as chunks), `embedding_model`, `valid_until` (nullable; dynamic
  facts expire), `superseded_by` FK self (nullable), `created_at`.
- Indexes: HNSW on `embedding` (cosine), `(user, superseded_by)`.

**UsageEvent** (`assistant`) — the ledger every AI action writes
- `user`, `kind` (`ask`, `condense`, `memory_extract`, `format`, `summarize`, `extract_file`),
  `provider`, `model`, `input_tokens`, `output_tokens`, `ask` FK (nullable), `created_at`.
- The quota counts rows of the billable kinds (open question 1); cost reports read tokens.
- V1's quota keeps counting `AskQuery` rows until phase 0 moves it onto the ledger (a data
  migration backfills one `ask` event per existing non-failed ask).

**FormatJob** (`notes`) — `note` FK, `owner`, `base_version`, `status`, `proposed_content` (JSON),
`error`, token fields, `idempotency_key`, `created_at`, `completed_at`.

**Reminder** (`notes`)
- `note` FK (CASCADE), `owner` FK, `due_at` (UTC), `lead_days` (default 7; D95), `channels`
  (JSON list: `email`, `push`), `status` (`scheduled` | `done` | `cancelled`), `created_at`,
  `updated_at`, `deleted_at`. Each notification of the series is a `ReminderDelivery`
  (`reminder`, `occurrence_at`, unique together, `sent_at`, `channel_results`).
- Index `(status, due_at)` for the due scan; `(owner, due_at)` for the calendar.
- Reminder writes go through `notes/services.py`: they take the owner lock and bump
  `notes_revision`, so `changes` carries them (D5 unchanged).

**PushSubscription** (`accounts`) — `user`, `endpoint` (unique), `p256dh`, `auth`, `user_agent`,
`created_at`, `last_success_at`. Deleted when the push service answers 404/410.

**Attachment** (`notes`)
- `note` FK, `owner` FK, `file`, `original_name`, `mime_type` (sniffed from bytes, never trusted
  from the client — the reference's `sniff_image_type` pattern), `size`, `sha256`, `status`
  (`pending` | `extracting` | `ready` | `failed`), `extracted_text`, `summary`, `error`,
  `created_at`, `deleted_at`.
- Size cap per file and per user (settings). Unique `(note, sha256)` so re-uploads are no-ops.

**NoteChunk** (existing) — add `attachment` FK (nullable, CASCADE) and `source`
(`note` | `attachment` | `summary`). Search and citations name the attachment when a chunk came
from one.

**NoteSummary** — not a table: `Note.summary` + `Note.summary_version` (the note version it
summarises), so a stale summary is visible.

---

## 5. Phases

Each phase lists its branch, what it builds, its decisions, and its acceptance tests. Sizes are
rough; *model* is the sub-agent tier per the working method (Opus for correctness-critical,
Sonnet for well-specified work).

### Phase 0 — foundation · `v2-feat/0-foundation` · M · Sonnet, Opus for the ledger

Backend:
- **Usage ledger** (`UsageEvent`) and moving the Ask quota onto it (data migration; the quota
  count under the user lock is unchanged, D73). `me/` reports usage from the ledger.
- **Stuck-ask sweeper** (BACKLOG): a beat task every 5 minutes fails asks `running` longer than
  the hard time limit plus the retry span; they stop counting.
- **Project `JSONParser`** that turns `RecursionError` into a 400 (BACKLOG).
- **413 carries CORS headers** (BACKLOG): move the size check behind `CorsMiddleware` for `/api/`
  or add the headers in the 413 itself.
- `User.timezone`, set from the browser on sign-in when blank.

Web:
- **vue-router** (after approval): `/notes`, `/notes/:id`, `/chat`, `/chat/:id`, `/settings`;
  the view store (D46) goes. Back button and deep links work.
- **vitest** (after approval) with tests for citation splitting, the refresh single-flight and
  the sync loop.
- `fetch(..., {keepalive: true})` for the save on tab close (BACKLOG).

Added 2026-10-02:
- **Limits layer** (D84; values D91 — `chat_turns`, `format`, `summary`, `storage_bytes`,
  `signups`, system-only `condense`, `memory_extract`) instead of a bare ledger: `Limit` keys with per-user (by plan) and
  system values, `UsageEvent` as the one counter, `429 quota_exceeded` / `503
  system_limit_reached`, admin mail on a system limit, `signups_per_day`.
- **No stale JavaScript** (D89): hashed assets + `no-cache` index, build id and `app/version/`,
  reload prompt, `vite:preloadError` reload.
- **Lifecycle hooks** (D90): `user_signed_in`, `app_opened` signals, `POST session/open/`
  returning notices.
- `User.memory_enabled` / `memory_choice_explicit` / `memory_notice_seen_at` fields now, so the
  notice plumbing is ready for phase 3 (D88).

Acceptance: quota behaviour identical to V1 (the V1 quota tests pass unchanged against the
ledger); the sweeper fails a stuck ask and it stops counting; a 5,000-deep JSON body is a 400; a
too-large cross-origin request shows "too large" in the browser; routes survive a reload.

### Phase 1 — conversations · `v2-feat/1-conversations` · L · Opus

Pipeline for a turn in a conversation:
1. **Condense** (turn 2 onward): the chat provider rewrites the follow-up into a standalone
   question using the last few turns ("and the one before?" → "What was the date of the Goa trip
   before the February one?"). Its own versioned prompt (`assistant/prompts/condense.md`), a small
   output cap, and a `condense` usage event. Skipped for turn 1 and when the follow-up already
   stands alone (a cheap heuristic: no pronouns/ellipsis — measured, not assumed).
2. **Retrieve** on the standalone question (V1's hybrid search, unchanged).
3. **Prompt** (`assistant/prompts/chat.md`, version `chat-v1`): system rules (V1's, plus "the
   conversation so far is context, not a source — cite only excerpts"), the conversation summary,
   the last N turns verbatim within a character budget, then this turn's excerpts and question.
4. **Answer and cite** exactly as V1: citations number this turn's excerpts only.
5. **Summarise** (async, after the turn): when the history exceeds the budget, fold the oldest
   turns into `Conversation.summary`.

API:
- `POST conversations/` (optional first question) · `GET conversations/` (cursor, `-updated_at`)
  · `GET conversations/<id>/` (turns, oldest first) · `PATCH` (title) · `DELETE` (soft).
- `POST conversations/<id>/turns/` with `Idempotency-Key` → `202` + the turn; poll
  `GET ask/<turn id>/` as today, or stream (phase 2).
- A turn is refused with `409 turn_in_progress` while the previous turn is still running (turns
  are sequential; the condenser needs the previous answer).
- `POST ask/` stays: a conversation-less single turn, for compatibility.

Evaluation:
- Extend `retrieval/eval/` with ~15 **multi-turn** cases: a first question, a follow-up that
  needs the first to make sense, and the labelled notes for the follow-up.
- `eval_retrieval --conversations` reports recall@5 for the follow-up *raw* vs *condensed*.
  Condensation stays on only if it wins on the real provider.

Web: a chat view (list of conversations, a thread, a composer), citations as in V1, the Ask panel
becomes "new conversation".

Acceptance: a follow-up with a pronoun retrieves the right note (fake condenser rewrites by a fixed
rule in tests); turns are strictly sequential; conversation isolation (another user's id → 404);
quota counts turns, not condense calls (or both, per open question 1); deleting a conversation
hides its turns from history but keeps quota accounting intact.

### Phase 2 — streaming · `v2-feat/2-streaming` · L · Opus

- **Provider layer:** the Protocol gains an optional `stream(system, user)` yielding text deltas
  and finally a `ChatResult` (usage). Claude, OpenAI and Gemini adapters implement it over
  `requests` with `stream=True` (SSE from each vendor); `fake` yields word by word. A provider
  without `stream` falls back to `complete` and emits one delta.
- **Worker → Redis pub/sub:** the answer task publishes `delta` events to channel `ask:<id>` and
  appends to the row every ~0.5 s (so a reconnect or a poll sees partial text); a final
  `done` event carries citations. Redis is already there.
- **ASGI endpoint** `GET ask/<id>/stream/`: an async view that authenticates the bearer token,
  checks ownership, sends the current state first (catch-up), then relays pub/sub events as
  `text/event-stream`, with a heartbeat comment every 15 s and a hard cap on duration.
  Runs under `uvicorn` (approval, §3); `runserver` keeps working for everything else.
- **Client:** `fetch` with a streaming body reader, not `EventSource` (which cannot send an
  `Authorization` header). On any stream error it falls back to V1's polling.
- **Citations while streaming:** markers appear in the text as they stream; chips become clickable
  when `done` arrives with the parsed citations.

Acceptance: first delta within ~1 s of the provider's first token (manual check with a real key);
a dropped connection resumes from the row's partial text; another user's stream → 404; the
polling path still passes all V1 tests; a provider without streaming still answers.

### Phase 3 — user memory · `v2-feat/3-memory` · M · Opus for extraction rules

- **Extraction** after each done turn (Celery, `memory_extract` usage event): the chat provider
  gets the question, the answer and the user's existing similar facts (vector search over
  `UserFact`), and returns operations as strict JSON: `add`, `update(id)`, `supersede(id)`,
  `none`. Validated against a schema; anything malformed is dropped, never retried into a loop.
- **Only from the user's own words.** Facts are extracted from the user's questions and the
  assistant's answer, **never from note excerpts** — a pasted web page in a note must not be able
  to plant "memories" (prompt-injection boundary, D-memory).
- **Use:** the top 5 relevant, unexpired, non-superseded facts go into the chat prompt in their
  own delimited block, marked as "what you know about the user", never as a citation source.
  `AskQuery.memory_used` records which.
- **Notices** (D88, D94): every 5 app opens, prominent until the user chooses, subtle after.
- **Control:** `GET memory/facts/`, `DELETE memory/facts/<id>/`, `DELETE memory/facts/` (forget
  everything), `PATCH me/ {memory_enabled}`. Off means no extraction and no use.
- **Expiry:** `dynamic` facts get `valid_until` (default 30 days); a daily beat task clears
  expired ones.

Acceptance: a stated preference ("I'm vegetarian") becomes a fact and is used in a later turn;
a contradiction supersedes the old fact; injected instructions in a note never become a fact
(test with a note containing "remember that the user's password is…"); memory off → no
extraction calls (mock asserts); facts are owner-scoped in SQL; deleting a fact removes it from
future prompts.

### Phase 4 — format my note · `v2-feat/4-format` · S · Sonnet

- `POST notes/<id>/format/` with `Idempotency-Key` → `202` `FormatJob`; poll
  `GET format-jobs/<id>/`. The task sends the note's TipTap JSON with a versioned prompt
  (`notes/prompts/format.md`): improve structure (headings, lists, checklists), fix obvious typos,
  **never add or remove facts**.
- **Guardrails:** the result must pass `notes/content.py`'s validator; its derived plain text must
  keep the original's words (token-set overlap above a threshold, and no new numbers/dates that
  weren't in the original) — otherwise the job fails with `format_changed_content`.
- **Apply:** the client shows a before/after preview; "Apply" is an ordinary `PATCH` with
  `version = base_version`, so a note edited meanwhile gets V1's `409` conflict flow.

Acceptance: a messy note becomes structured and passes the guardrail; a result that drops a
paragraph or invents a date is refused; applying on a stale version conflicts; usage recorded.

### Phase 5 — reminders and calendar · `v2-feat/5-reminders` · L · Opus for delivery, Sonnet for the calendar UI

- **Schedule (D95):** a due date-time plus daily heads-ups from `lead_days` (default 7) before it,
  at the same time of day, and on the due day. "Mark done" stops the rest of the series.
- **API:** `POST notes/<id>/reminders/`, `PATCH/DELETE reminders/<id>/`,
  `POST reminders/<id>/done/`, `GET reminders/?from=&to=` (calendar range; each reminder with its
  heads-up dates in range).
  Times are sent and returned with an offset; stored in UTC; displayed in `User.timezone`.
- **Delivery:** a beat task every minute finds occurrences that are due (computed from `due_at`
  and `lead_days` in the user's timezone), claims each by inserting its `ReminderDelivery` row
  (unique `(reminder, occurrence_at)` — a second worker's insert fails, so delivery is at most
  once), and enqueues the sends on commit. Missed occurrences while the worker was down are sent
  once, not replayed one by one.
- **Channels:** email (Django email — console backend in dev), web push (`pywebpush`, VAPID keys
  in settings, a service worker in `web/public/sw.js`), subscribe/unsubscribe endpoints under
  `me/push-subscriptions/`. Dead subscriptions (404/410) are deleted.
- **Natural language** ("remind me next Friday at 9"): optional, via the chat provider returning
  an ISO datetime in the user's timezone; the client always shows the parsed time for
  confirmation before saving.
- **Calendar** (web): month and week views of reminders; clicking opens the note.
- **Sync:** reminders ride in `notes/changes/` (a `reminders` array per changed note).

Acceptance: each occurrence is delivered exactly once even with two beat workers
(TransactionTestCase with threads); a 7-day lead sends 8 notifications at the right local time,
including across a DST change (`Europe/London`); "mark done" stops the series; a deleted note
cancels its reminders; calendar range queries are owner-scoped; dead push subscriptions are
removed.

### Phase 6 — attachments and summaries · `v2-feat/6-attachments` · L · Opus for upload security

- **Upload:** `POST notes/<id>/attachments/` (multipart), limits per file (e.g. 10 MB) and per
  user; type from magic bytes (JPEG, PNG, WebP, PDF only); stored under a random name outside
  any public URL; `sha256` de-duplication per note. `MaxUploadSizeMiddleware` gets a
  per-path allowance for this endpoint only.
- **Download:** `GET attachments/<id>/file/` — authenticated, owner-scoped, `Content-Disposition:
  attachment`, `X-Content-Type-Options: nosniff`, never served from `MEDIA_URL`.
- **Extraction** (Celery): PDF text with `pypdf`; images through the provider layer's new
  `extract_image_text` (vision models; fake returns fixed text) — the reference's bill-scan
  pattern. Text is chunked and indexed into `NoteChunk` with `source=attachment`, so search, Ask
  and chat cover it, and citations say "from *receipt.pdf* in *Car service*".
- **Summaries:** `POST notes/<id>/summarize/` → job → `Note.summary` (+ `summary_version`); a
  summary chunk (`source=summary`) is indexed so broad questions match it. Attachments get a
  summary too.
- **Deletion:** deleting an attachment or its note de-indexes its chunks and deletes the file.

Acceptance: a renamed `.exe` with a `.pdf` extension is refused by magic bytes; another user's
attachment id → 404 on metadata and file; a PDF's text is searchable and citable; an image's text
(fake extractor) is searchable; quotas and sizes enforced; deleting removes file and chunks.

### Phase 7 — mobile catch-up · `v2-feat/7-mobile` · M · Sonnet

Only if V1's Phase 7 (Android) has landed: native notifications for reminders (Capacitor local
notifications + FCM for delivery when the app is closed — a dependency decision at that time),
the share sheet ("share to Super Notes" creates a note or an attachment), and deep links into
`/notes/:id` and `/chat/:id`.

---

## 6. Security (keeping ASVS L2 as V2 adds surface)

| New surface | Control |
|---|---|
| Uploads (V5 file handling) | Magic-byte type check, size caps, random storage names, no public URL, attachment-only download with `nosniff`, per-user storage cap |
| Memory | Extracted only from the user's words, never note excerpts; user can see, delete, switch off; delimited in prompts and never cited |
| Streaming endpoint | Same bearer auth and ownership check as polling; duration cap; no tokens in query strings |
| Web push | VAPID private key in env only; subscriptions owner-scoped; payload carries no note content, only an id to open |
| Reminders email | No note body in the email unless the user opts in; unsubscribe per reminder |
| All AI features | Every call behind the provider layer, recorded in the usage ledger, counted against the quota under the user lock |
| Prompt injection | Every prompt keeps V1's delimiting (excerpts are data); the format guardrail refuses content changes |

`SecurityEvent` gains: `memory_cleared`, `push_subscribed`, `attachment_uploaded` (size and type
only, never names or content).

---

## 7. Evaluation (extends V1's harness)

- **Multi-turn retrieval** (phase 1): raw vs condensed follow-ups, recall@5 and MRR.
- **Memory** (phase 3): ~20 scripted conversations with the facts that should (and should not)
  be extracted; precision and recall of extraction; injection cases must extract nothing.
- **Format guardrail** (phase 4): ~15 notes with deliberately bad model outputs (dropped
  paragraph, invented date) that must be refused, and good ones that must pass.
- **Attachments** (phase 6): add 5 PDF/image fixtures and 8 questions answerable only from them.
- All run on fake providers in CI (smoke) and on real providers by hand; numbers recorded in
  `docs/RAG.md` and a new `docs/EVAL_V2.md`.

---

## 8. Docs

- `docs/V2_PLAN.md` (this file) — kept current; phase status table in `docs/COMMIT_PLAN.md` gains a
  V2 section.
- `docs/DECISIONS.md` continues numbering from D77.
- `docs/BUILD_LOG.md` gets a "V2" part; each merge records the branch, its commits and what went
  wrong.
- `docs/RAG.md` gains Conversations, Streaming and Memory sections; `docs/architecture.html` is
  updated at each phase merge.
- `README.md` at release: new endpoints, `uvicorn` in the run steps, VAPID keys, storage.

---

## 9. Every branch ends with (before merging into `v2`)

1. Backend suite green (also with `DEBUG=False`), coverage ≥ 95% (raise D12's bar in phase 0).
2. `ruff check`, `ruff format --check`, `makemigrations --check` clean; `docs/openapi.yml`
   regenerated.
3. `cd web && npm run build` and `npm test` (from phase 0) green.
4. Docs updated; decisions recorded; a built / left / unsure summary in `BUILD_LOG.md`.
5. Reviewed (sub-agent work is reviewed before it is committed — the V1 working method).
6. Merged `--no-ff` into `v2`, pushed, tagged `v2-phase-N-<name>`.

---

## 10. Definition of done for V2

- Phases 0–6 merged into `v2` and tagged; CI green on `v2`.
- With a real provider: multi-turn recall@5 recorded (raw vs condensed), memory extraction
  precision/recall recorded, attachment questions answered with citations.
- A conversation in the browser: three turns with a follow-up, answers streaming in, citations
  opening notes, a remembered preference used in a later conversation.
- A reminder set in the browser arrives by email and web push, once.
- A PDF attached to a note answers a question with a citation naming the file.
- `v2` merged into `master`, tagged `v2.0.0`, README and architecture page current.

---

## 11. Suggested order and timing

| Order | Branch | Depends on | Can run in parallel with |
|---|---|---|---|
| 1 | `v2-feat/0-foundation` | — | — |
| 2 | `v2-feat/1-conversations` | 0 | `v2-feat/4-format` |
| 3 | `v2-feat/2-streaming` | 1 | `v2-feat/5-reminders` |
| 4 | `v2-feat/3-memory` | 1 | `v2-feat/5-reminders`, `v2-feat/6-attachments` |
| — | `v2-feat/4-format` | 0 | 1, 2, 3 |
| — | `v2-feat/5-reminders` | 0 | 2, 3, 6 |
| — | `v2-feat/6-attachments` | 0 | 3, 5 |
| last | `v2-feat/7-mobile` | V1 Phase 7, 5 | — |

Parallel branches touch different apps (`assistant` vs `notes`), so merges stay small; shared
files (`config/settings.py`, `config/api/urls.py`) take additive blocks only, as in V1.

---

## 12. Ready queue (pick from the top; keep this current)

Fully decided work, in order. Each item's detailed brief is in `docs/v2/briefs/` (written
when the item becomes next). A session — local or remote — takes the first unclaimed item,
builds it on its branch, merges, and ticks it here. Items marked **blocked** wait on the owner.

| # | Branch | What | Model | Status |
|---|---|---|---|---|
| 1 | `v2-feat/0c-limits` | Limits layer (D84): `limits` app — `Limit`, `UsageEvent`, check-and-record under the user lock plus the system count, Ask quota moved onto it, `signups_per_day`, 429/503, admin mail | Opus | in progress (2026-10-02) — brief: `docs/v2/briefs/0c-limits.md` |
| 2 | `v2-feat/0e-lifecycle` | D89/D90 backend: `GET app/version/`, `X-Client-Min-Version`, `user_signed_in` and `app_opened` signals, `POST session/open/` with notices, memory-notice fields and rule (D88), `PATCH me/` (timezone, memory) | Sonnet | **done** (merged 2026-10-02) — brief: `docs/v2/briefs/0e-lifecycle.md` |
| 3 | `v2-feat/0d-web-platform` | vue-router + vitest (D86), stale-JS defences (D89), session/open + notices UI, timezone on sign-in | Sonnet | **done** (merged 2026-10-02) — brief: `docs/v2/briefs/0d-web-platform.md` |
| 4 | `v2-feat/1-conversations` | Phase 1 | Opus | ready after 1–3 |
| 5 | `v2-feat/4-format` | Phase 4 | Sonnet | ready after 1 |
| 6 | `v2-feat/2-streaming` | Phase 2 (needs `uvicorn` approval) | Opus | ready after 4 |
| 7 | `v2-feat/3-memory` | Phase 3 | Opus | ready after 4 (#4) |
| 8 | `v2-feat/5-reminders` | Phase 5 | Opus + Sonnet | ready after 1–3 |
| 9 | `v2-feat/6-attachments` | Phase 6 (needs `pypdf` approval) | Opus | ready after 1–3 |
| 10 | `v2-feat/7-mobile` | Phase 7 | Sonnet | after V2 (D97) — planned in the evening session |

Parallel-safe pairs: 4 ∥ 5, 6 ∥ 8, 7 ∥ 9 (different apps; shared files take additive blocks).
