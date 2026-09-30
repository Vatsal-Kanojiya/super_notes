# Commit plan

> The phase list from `SUPER_NOTES_V1_PLAN.md` §8, kept current. One commit = one reviewable
> idea, Conventional Commits, vertical slices (model → serializer → view → URL → test). Each phase
> ends with an annotated tag, so `git diff phase-1-auth phase-2-notes` shows exactly what a step
> changed.

## Status

| Phase | Tag | Status |
|---|---|---|
| 0 — scaffold | `phase-0-scaffold` | ✅ Built, tagged |
| 1 — auth | `phase-1-auth` | ✅ Built |
| 2 — notes and sync | `phase-2-notes` | ✅ Built |
| 3 — chunking and indexing | `phase-3-indexing` | ✅ Built |
| 4 — retrieval and evaluation | `phase-4-retrieval` | ✅ Built (real-provider numbers pending a key) |
| 5 — ask | `phase-5-ask` | ✅ Built (relevance floor value pending the real-provider eval) |
| 6 — web client | `phase-6-web` | 🟡 6a built against the contract; not yet run against the real API |
| 7 — Android | `phase-7-android` | ⏳ |

## Every phase ends with

1. `python manage.py test` green, coverage at or above `fail_under`.
2. `ruff check .` and `ruff format --check .` clean.
3. `python manage.py makemigrations --check --dry-run` clean.
4. `python manage.py spectacular --file docs/openapi.yml --validate --fail-on-warn` (CI diffs it).
5. `BUILD_LOG.md`, `DECISIONS.md`, this table updated; a built / left / unsure summary.
6. `git tag -a phase-N-name -m "Phase N: …"`.

## Phases

### 0 — scaffold
Settings adapted from the reference, custom User, pgvector extension migration, exception handler,
pagination, schema and docs, `health/`, test runner, ruff, pre-commit, CI.
*Done when:* on a fresh native Postgres, `migrate`, `runserver`, `/api/v1/docs/` and the suite all
work, and CI is green.

### 1 — auth
Google sign-in with multiple audiences, JWT issue / rotate / blacklist, `me/`. Security layer from
the reference (D6): `SecurityEvent` trail, sign-in rate limit, two-device limit, devices list and
sign-out.
*Tests:* a valid token creates a user; a repeat sign-in links to the same user; wrong audience, bad
issuer, expired token and unverified email are refused (verifier mocked); a rotated refresh token
can't be reused; the third device signs the first out.

### 2 — notes and sync
Model, `content_text` derivation, CRUD, version conflicts, revision-based `changes`, keyword search.
*Tests:* ownership isolation; `content_text` for text and checklist; stale version → 409 with the
server copy; delete → tombstone in `changes`; `changes?after=n` exact; concurrent writes get
distinct ordered revisions (`TransactionTestCase` with threads).

### 3 — chunking and indexing
Chunker, embedding registry (fake + one real), `NoteChunk`, debounced idempotent indexing,
de-indexing on delete, `reindex_notes`, `index_status`.

### 4 — retrieval and evaluation
Hybrid search (vector + full-text, RRF), `search/`, eval fixtures, `eval_retrieval`.

### 5 — ask
`AskQuery`, chat registry with a fake provider, prompt, task, citation parsing, relevance-floor
short-circuit, quota, idempotency.

### 6 — web client
Vue 3 + TipTap: sign-in, notes list with search, editor with autosave and conflict prompt, sync,
Ask panel with clickable citations and usage.

### 7 — Android
Capacitor wrap, native Google sign-in, API base URL from build config.
