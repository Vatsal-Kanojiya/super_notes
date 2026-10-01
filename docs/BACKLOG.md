# Backlog

Out of V1's scope, or parked during the build. Nothing here is started without the owner's go.

## V2 candidates (from the plan)

- **Multi-turn chat with conversation memory** — the long-term goal. V1's Ask is single-turn.
  - **User memory, in-stack (supermemory's ideas, not its service).** A `UserFact(user, text,
    kind=static|dynamic, source_ask, valid_until, embedding)` table; a Celery task after each
    answered ask extracts facts through the chat provider layer; the few most relevant facts go
    into the Ask prompt beside the note excerpts; a new fact supersedes one it contradicts. Reuses
    pgvector and the owner filter in SQL, with no new service. Research:
    `docs/research/memory-tools.html`.
- **Streaming answers** (and websockets). V1 polls.
- **AI "format my note" button.**
- **Reminders** and **calendar**.
- **Uploads and summaries.**
- Sharing, payments (the plan is admin-edited in V1), offline-first storage, desktop packaging.

## Parked during the build

Things that came up and were left for a decision. Each names what it is waiting on.

- **Real embedding and chat providers** — waiting on which keys the owner has (OpenAI, Gemini or
  Claude). The fake providers carry the build until then; the registry takes a new one without
  touching callers.
- **Relevance floor value** (D58, D74) — the floor now compares the vector leg's cosine
  similarity, and any keyword match passes it; its value is still 0.0. Waiting on the
  real-provider `eval_retrieval` run: set it from the no-answer vs answerable similarity lines.
- **Localised no-answer text** — `ASK_NO_ANSWER_TEXT` is fixed English while model answers follow
  the question's language. V1 accepts it.
- **Refusal fallbacks for Claude 5.5-class models** — if `CHAT_CLAUDE_MODEL` moves to Sonnet or
  Opus 5.5, the claude-api skill recommends the server-side `fallbacks` parameter; not sent today
  (default is Haiku 4.5).

- **Choose the embedding provider** — OpenAI and Gemini are both implemented (D37) and neither has
  been run against a real key. Waiting on the owner's choice and key: run
  `LIVE_PROVIDER_TESTS=1 python manage.py test retrieval.tests.test_embeddings`, set
  `EMBEDDING_PROVIDER`, then consider deleting the unused provider.
- **Real chat provider** — waiting on which keys the owner has (OpenAI, Gemini or Claude). The fake
  carries the build until then; the registry takes a new one without touching callers.
- **Static files in production** — no WhiteNoise (D10). Needs a deployment decision.
- **Eval: per-kind breakdown and noise** — `eval_retrieval` should print recall/MRR per question
  kind (that is where vector vs keyword differ), and with 28 answerable questions one question is
  ~3.6 points, so differences under ~5 points should not drive decisions. Waiting on search.
- **Eval: relevance-floor check** — the three `no_answer` questions should report the top score
  against the Ask floor once that floor exists (Phase 5).

- **Trash / undo for deleted notes** — tombstones keep their content (D28), so a restore endpoint
  is possible; not in V1's scope.

- **Web: semantic search UI** — `searchApi.search()` and its types exist (`web/src/api/`), but
  the notes list only uses keyword `q`. A "related passages" view is a small addition.
- **Web: back button and deep links** — no router (D46). Needed for Android's back button in
  Phase 7, and for linking to a note.
- **Web: unit tests** — no test runner is installed (only the listed dependencies). Vitest would
  cover `lib/citations.ts`, the refresh single-flight and the notes sync loop; ask before adding.
- **Web: offline edits** — edits made offline are retried every 5 s while the editor is open, but
  are lost if the note is closed first (offline-first is out of V1, D49).
- **Web: dark-mode toggle** — the theme follows the system only.
- **Web: API contract reconciliation** — `web/src/api/types.ts` was written from the plan, not the
  generated schema. Reconcile it with `docs/openapi.yml` once the backend phases merge.

- **Immediate sign-out of a device** — a signed-out device's access token works until it
  expires (up to 30 minutes, D19). The `device` claim (D18) makes a per-request "does this device
  still exist" check a small change, at one indexed query per request. Waiting on whether 30
  minutes is acceptable.
- **Account deletion** — not in V1's scope. `SecurityEvent.user` is `SET_NULL` so the trail
  survives it, but nothing blanks the `email` snapshot yet (the reference does).

## Parked during V2

- **Summary chunks after an embedding-model change** (D555) — `reindex_notes` re-embeds note
  chunks but not the summary chunk, and an embedding error while storing a summary leaves it
  without a chunk (logged, not retried). Until fixed, re-run "Summarize" after a model change.
- **Chunk search post-filtering** (D68) — the HNSW owner post-filter can starve a small user in a
  large table; D517 fixed it for facts only. Revisit with pgvector ≥ 0.8 iterative scans.

