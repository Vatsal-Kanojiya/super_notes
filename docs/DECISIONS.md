# Decisions taken without asking

> Companion to [BUILD_LOG.md](BUILD_LOG.md) (what happened) and [COMMIT_PLAN.md](COMMIT_PLAN.md)
> (what happens next). Every judgement call made during an autonomous session is here, so a
> decision you did not personally make is never invisible.
>
> Format: **decided** · **alternatives** · **why** · **reverse it if**.

---

## Phase 0 — scaffold

### D1. Work stays on `master`; one annotated tag per phase

**Decided:** as in the reference (its D1). Phases land as commits on `master`, tagged
`phase-0-scaffold`, `phase-1-auth`, …

**Alternatives:** a branch per phase, merged `--no-ff`.

**Why:** `git diff phase-1-auth phase-2-notes` is the reason the tags exist, and `practice/*`
branches are cut from tags.

**Reverse it if:** you want a phase reviewed before it lands.

### D2. PostgreSQL with pgvector is required; SQLite is not supported

**Decided:** `DATABASE_URL` has no default, and a system check (`config.E001`) refuses any engine
but PostgreSQL. `CREATE EXTENSION vector` is a migration (`retrieval/0001_vector_extension`).

**Alternatives:** the reference's SQLite default with Postgres optional; a vector store beside
SQLite (sqlite-vec, FAISS files).

**Why:** vector search, the GIN full-text indexes and `select_for_update` (sync, quota) are all
Postgres features. On SQLite they would fail late and quietly: a lock that does nothing is worse
than a crash at startup. One database also keeps the owner filter a SQL `WHERE` (D-retrieval).

**Reverse it if:** never for V1. A second backend would mean a second retrieval implementation.

### D3. Shared API pieces live in `config/api/`

**Decided:** the exception handler, pagination, common response shapes, health view and the
versioned URL mount live in `config/api/`. Each app exposes its own `urlpatterns`.

**Alternatives:** a `core` app; putting them in `notes/api/` as the reference put them in
`expenses/api/`.

**Why:** the reference had one main app to host them. Here no app is central, and `config` is
already where project-wide wiring lives. It needs no models, so not an app of its own, except
for registering system checks (`config.apps.ConfigConfig`).

**Reverse it if:** these grow models or migrations.

### D4. Bearer tokens only; no `SessionAuthentication` in DRF

**Decided:** `DEFAULT_AUTHENTICATION_CLASSES` is JWT alone.

**Alternatives:** keep session auth, as the reference did, for the browsable API.

**Why:** the reference had server-rendered pages sharing the session. Here the only session is the
admin's, and leaving session auth on makes every API call from an admin-logged-in browser subject
to CSRF, for no product benefit. The Swagger UI works with a pasted bearer token.

**Reverse it if:** you want the browsable API while logged into the admin.

### D5. Sync is revision-based, not timestamp-based

**Decided:** the plan's design, recorded as a decision. Every note write locks the owner's row
(`select_for_update`), increments `User.notes_revision`, and stamps the note's `revision` with it.
`GET notes/changes/?after=<n>` returns notes with `revision > n`, tombstones included.

**Alternatives:** `updated_at > since`; a per-note version only.

**Why:** timestamps are assigned before commit. A slow transaction can commit with an earlier
`updated_at` than a change a client has already seen, and the client silently skips it. Serialising
a user's writes on their own row makes revisions strictly ordered *in commit order*. The cost is
that one user's writes are serialised, which is fine at human typing speed.

**Reverse it if:** one user ever needs parallel bulk writes (an import); batch them in one
transaction instead.

### D6. Security scope: the reference's ASVS L2 posture, including the device limit

**Decided (by the owner, 2026-09-30):** carry over the reference's Level 2 posture: the
`SecurityEvent` trail, sign-in rate limits, **the two-device limit**, CSP and the security headers,
sensitive-variable scrubbing, SECURITY.md and Dependabot. Password auth, MFA and email verification
stay out: sign-in is Google-only, and Google's own MFA covers the account.

**Alternatives:** the plan's narrower list, which excluded device limits.

**Why:** the owner asked for the same outer layer as the expense tracker. The device limit counts
API refresh-token chains only; there are no web sessions for users.

**Reverse it if:** two devices proves too few with web + Android + a second browser. Raise
`MAX_SIGNED_IN_DEVICES` (setting); no code change.

### D7. Every DRF error gains a `code`

**Decided:** the exception handler adds `code` to all DRF errors from `exc.get_codes()`, and
`code: "invalid"` to field-error 400s.

**Alternatives:** the reference's handler, which handled only `ProtectedError` and left DRF's own
errors as `{"detail"}`.

**Why:** the plan requires `{"detail", "code"}` everywhere; a client should never branch on English.

**Reverse it if:** a client needs DRF's exact default body.

### D8. The committed schema must match the code

**Decided:** `docs/openapi.yml` is committed, and CI fails if a fresh
`manage.py spectacular --validate --fail-on-warn` differs from it.

**Alternatives:** generate on demand only.

**Why:** the plan asks for the schema to be regenerated each phase. A diff check makes forgetting
impossible and shows API changes in review.

**Reverse it if:** the diff becomes noisy across drf-spectacular upgrades; then regenerate in the
Dependabot PR.

### D9. HSTS defaults to one hour

**Decided:** as the reference's D5. `SECURE_HSTS_SECONDS` defaults to 3600; production sets
31536000 once HTTPS is known stable.

**Why:** a wrong long value is nearly irreversible in browsers.

**Reverse it if:** the deployment's HTTPS is stable; raise the default.

### D10. No WhiteNoise

**Decided:** not installed. The admin's static files are served by `runserver` in development; a
deployment serves `collectstatic` output from its web server.

**Alternatives:** WhiteNoise, as the reference.

**Why:** §3 of the plan forbids new dependencies, and deployment is out of V1's scope. The product
UI is the Vue client, which Vite builds.

**Reverse it if:** you deploy without a web server in front; ask, then add WhiteNoise.

### D11. The test runner forces the fake providers

**Decided:** `FastTestRunner` sets `EMBEDDING_PROVIDER` and `CHAT_PROVIDER` to `fake` whatever
`.env` says. The real providers' opt-in tests override this themselves.

**Why:** a developer with a real key in `.env` must never spend money by running the suite.

### D12. Coverage bar starts at 90%

**Decided:** `fail_under = 90`, raised as phases land. The reference's is 95.

**Why:** `config/settings.py`'s `if not DEBUG:` block is uncovered on a machine running with
`DEBUG=True`, which alone costs several points on a small codebase. CI runs with `DEBUG=False`.

**Reverse it if:** the suite settles; raise it to 95.

---

## Phase 2 — notes and sync

### D23. `content_text` is one line per block, derived in `Note.save()`

**Decided:** `notes/content.py` walks the TipTap tree and writes one line per block: paragraphs and
headings as their text, list items as `- ` / `1. `, checklist items as `- [x] ` / `- [ ] `,
quotes as `> `, nested lists indented two spaces, hard breaks as newlines, empty blocks dropped.
Unknown nodes contribute their text and children; malformed ones contribute nothing; it never
raises. Validation is shallow: the `{"type": "doc", "content": [...]}` envelope, every node an
object with a string `type`, string `text`, list `content`, and at most 100 levels deep (checked
iteratively). It is derived in `Note.save()`, not in the service, so no write path can skip it.

**Alternatives:** validate against TipTap's schema (which node may hold which); derive in the
service only; store no derived text and search the JSON.

**Why:** the server needs the words (search, admin), not a renderer. A full schema check would
make every new editor extension a server release. The checked state is kept because "what is
still open on my list" is a question Ask should be able to answer. The depth cap keeps every walk
far from Python's recursion limit.

**Reverse it if:** the client adds a node whose text lives somewhere other than `text` / `content`
(e.g. a mention's `attrs.label`); teach `_block_lines` about it.

### D24. Full-text search uses the `english` configuration

**Decided:** `notes.search.SEARCH_CONFIG = "english"`, one constant used by both the GIN
expression index and the query (`note_search_vector()`), queried with `websearch_to_tsquery`.
Phase 4's chunk search reuses the constant.

**Alternatives:** `simple` (lowercase only, no stemming, no stop words).

**Why:** stemming is most of what keyword search is for ("flight" finds "flights", "booking"
finds "book"), and the reference used `english`. Words in other languages still match exactly,
because the query goes through the same stemmer as the text. The cost: a query made only of stop
words ("the", "and") finds nothing, and stems are English-shaped. `websearch_to_tsquery` takes
what people type (quotes, `or`, `-word`) and never raises a syntax error. One function builds the
expression for both index and query, so they cannot drift apart and silently stop using the index
(`test_search.SearchIndexTests` proves the plan uses `note_fts`).

**Reverse it if:** the owner's notes are largely non-English; switch to `simple` (a migration
rebuilding `note_fts`, and the chunk index with it).

### D25. `changes`: ceiling first, `limit` + `has_more`, tombstones without content

**Decided:** refinements to D5.
- The user's `notes_revision` (the ceiling) is read **before** the notes, and the query is
  `after < revision <= ceiling`. Read the other way round, a write committing between the two
  reads would be covered by `latest_revision` without being sent, and skipped for ever.
- `limit` (default 500, max 1000). The response is `{results, latest_revision, has_more}`. With
  `has_more`, `latest_revision` is the last sent note's revision (revisions are unique per user,
  so resuming there is exact); the client calls again straight away. Otherwise it is the ceiling.
- A note written several times since `after` appears once, at its newest revision.
- Tombstones carry `id, type, version, revision, updated_at, deleted_at` and no content; live
  notes carry the full note with `deleted_at: null`. The schema is a `oneOf`.
- A client whose `after` is above the server's revision (a restored database) gets
  `latest_revision` lower than it sent: its cue to resync from 0.

**Alternatives:** no limit (a first sync of thousands of notes in one response); cursor pagination
(a cursor over a set that is still being written); returning tombstone content.

**Why:** the loop is trivial for a client and bounds every response. A deleted note's content has
no business travelling to devices after the delete.

**Reverse it if:** notes get large enough that 500 per batch is too heavy; lower the default.

### D26. Keyword search on `notes/` stays in newest-first order

**Decided:** `GET notes/?q=` filters by full-text match but keeps the `-id` cursor order.

**Alternatives:** order by `ts_rank`.

**Why:** cursor pagination needs a unique, unchanging ordering (`config/api/pagination.py`); a
rank is neither. This endpoint is a filter for the notes list; ranked retrieval is phase 4's
`search/`.

**Reverse it if:** people use the list's search to find rather than to filter; add a ranked,
non-paginated mode.

### D27. The version check runs after the owner lock; a conflict leaves no trace

**Decided:** `update_note` locks the owner, then re-reads the note, then compares versions.
`VersionConflict` rolls back the whole transaction, including the revision increment. The `409`
body is `{detail, code: "version_conflict", current: <full note>}`.

**Alternatives:** check before locking; `select_for_update` on the note row as well.

**Why:** checked before the lock, two saves carrying the same version can both pass. After the
lock every writer for that owner is queued, so the re-read is the truth and no second lock is
needed. Rolling back keeps revisions gap-free, which makes "exactly the writes after n" testable.

### D28. Delete has no version check; the tombstone keeps the content

**Decided:** `DELETE notes/<id>/` deletes whatever version is current. The row keeps its title and
content (visible in the admin); clients only ever receive the tombstone fields. A deleted note is
a 404 everywhere except `changes`.

**Alternatives:** require `version` on delete; blank the content.

**Why:** a person deleting a note means "this note", not "this version of it". An edit from
another device that lost the race is lost with it, which is what delete means. Keeping the
content leaves room for an undo or trash later (BACKLOG) and costs nothing: the indexer removes
its chunks.

**Reverse it if:** deletes-after-edits cause complaints; require `version` and return 409.

### D29. The Note admin is read-only

**Decided:** no add, change or delete in the admin.

**Why:** a save there bypasses `services.py`, so no revision: every device would miss it. A delete
there is a hard delete, which no device ever hears about.

**Reverse it if:** support needs to edit notes; add an admin action that calls the service.

### D30. No PUT; PATCH requires `version`; `type` may change

**Decided:** `http_method_names` has no `put`. PATCH takes `version` (required) plus any of
`type`, `title`, `content`.

**Why:** a full replace would have to invent values for every omitted field, and autosave sends
what changed anyway. Changing a note between text and checklist is a legitimate edit; the content
is the same TipTap document either way.

### D31. The content cap is measured as compact UTF-8 JSON of `content` alone

**Decided:** `len(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode())` against
`NOTE_CONTENT_MAX_BYTES` (1 MB), checked after the shape validation; titles are at most 500
characters.

**Why:** that is close to what Postgres stores, and independent of how the client happened to
format or escape its request body. Measuring after `validate_doc` means `json.dumps` never meets
anything deeper than 100 levels.

### D32. The owner FK has no index of its own

**Decided:** `owner` is `db_index=False`; `(owner, revision)` and `(owner, -id)` both lead with it.
`(owner, revision)` is a plain index, not a unique constraint.

**Why:** a third index on the same leading column is write cost for no read. Revisions are unique
per owner by construction (the lock), but a unique constraint would make every note created
outside the service with the default revision 0 collide, which would bite fixtures and tests more
than it would ever catch a bug.

**Reverse it if:** anything but `services.py` ever writes notes in production; then make it
unique so the invariant is enforced by the database.

### D41. Eval notes are hand-written TipTap JSON, validated strictly on load

**Decided:** `retrieval/eval/fixtures/notes.json` is the source of truth: 30 TipTap documents
(StarterKit + TaskList/TaskItem), each with a stable key `n01`...`n30`. The loader
(`retrieval/eval/loader.py`, not `fixtures.py`, which would shadow the `fixtures/` directory)
refuses any node type or mark the editor cannot produce, and any other shape fault.

**Alternatives:** plain-text notes; a Python generator committed next to the JSON; Django
fixtures loaded into the database.

**Why:** the eval should exercise the real chunker on real structure (headings, task items).
One source of truth avoids a generator and its output drifting. A node the chunker has never
seen would make the eval measure a bug, not retrieval. Pure JSON keeps the loader DB-free.

**Reverse it if:** the fixture set grows past what is comfortable to edit by hand; then commit a
generator and check its output in CI.

### D42. Rankings are scored per note, at the rank of the note's best chunk

**Decided:** before scoring, a chunk ranking is collapsed to notes in first-seen order
(`dedupe_to_notes`). `recall_at_k` and `reciprocal_rank` dedupe their input themselves.

**Alternatives:** score chunks against chunk labels; score the raw chunk list against note labels.

**Why:** questions are labelled with notes, and people see notes. Scoring the raw list would let
one long note's chunks fill the top k and push every other note out, punishing long notes for
being long. Chunk-level labels would have to be redone whenever the chunker changes.

**Reverse it if:** the eval needs to judge which *section* was found; then add chunk labels.

### D43. No-answer questions are excluded from recall and MRR, and counted

**Decided:** a question with `relevant: []` is skipped by `evaluate` and reported as
`no_answer`. The per-question functions raise `ValueError` on it. With no answerable questions,
the means are `None`, not 0.

**Alternatives:** score a no-answer question 1 when nothing is returned, 0 otherwise.

**Why:** recall is undefined when |R| = 0, and scoring it either way moves the mean for reasons
unrelated to ranking. Search always returns something, so these questions test the Ask
relevance floor, which is judged separately.

**Reverse it if:** search itself gains a floor; then report a "correctly empty" rate beside the
means.

### D44. Multi-relevant recall is a fraction; MRR counts the first hit; means are macro

**Decided:** recall@k = |R ∩ top k| / |R|; reciprocal rank uses the first relevant note only;
every answerable question weighs the same in the means.

**Alternatives:** binary "any relevant in top k" (hit rate); micro-averaging over relevant notes.

**Why:** the fraction shows a multi-note question half-answered as half. First-hit MRR matches
what a reader feels ("how far down is the first useful result"). Micro-averaging would let
multi-note questions count double.

**Reverse it if:** Ask starts depending on getting *all* relevant notes; then add a
"complete@k" (all of R in top k).

### D45. Question kinds are a closed set, and labels are strict

**Decided:** eight kinds (`keyword`, `paraphrase`, `section`, `near_duplicate`, `checklist`,
`hinglish`, `multi_note`, `no_answer`), validated against the labels: `no_answer` exactly when
`relevant` is empty, `multi_note` with two or more notes. A note is relevant only if it
contains the answer, not if it is on the topic (the dal makhani recipe is not relevant to "did I
need paneer that week").

**Alternatives:** free-text kinds; graded relevance (0/1/2).

**Why:** per-kind numbers are what show where vector beats keyword and vice versa, so a
mislabelled kind would misreport exactly that. Binary strict labels are simple and easy to
review; graded ones would need nDCG and more labelling.

**Reverse it if:** the eval moves to nDCG, or real questions show a need for "partially
relevant".
