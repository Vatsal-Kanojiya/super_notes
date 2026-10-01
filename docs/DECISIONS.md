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

## Phase 1 — auth

### D13. An email already linked to a different Google `sub` is refused

**Decided:** `find_or_create_user` matches on `google_sub` first, then on email. If the email
matches an account whose `google_sub` is set to a *different* value, the sign-in is refused
(`google_failed`, recorded as `google_login_failed` with `reason: sub_mismatch`).

**Alternatives:** re-link the account to the new `sub`; create a second account (impossible:
email is unique).

**Why:** `sub` is Google's stable account id; an email is not. The same address on a new `sub`
means a different Google account now holds it (a Workspace admin deleted a user and re-created
the address). Re-linking would hand the old holder's notes to the new one.

**Reverse it if:** real people hit it. The fix for one account is to clear its `google_sub` in a
shell (it is read-only in the admin on purpose).

### D14. The audience list goes straight to google-auth; tests verify real signatures offline

**Decided:** `verify_oauth2_token(..., audience=list(GOOGLE_OAUTH_CLIENT_IDS))`. google-auth 2.58
accepts a list and requires `aud` to be one of its entries (`google/auth/jwt.py`, `decode`), so no
second `aud` check is written by hand. `iss` and `email_verified is True` are checked on top. The
fetch of Google's certificates gets a 10-second timeout (the transport's default is 120).
Tests sign tokens with a throwaway RSA key and replace only the certificate transport
(`accounts/tests/fake_google.py`), rather than mocking `verify_oauth2_token` as the reference does.

**Alternatives:** verify with no audience and compare `aud` ourselves; mock the verifier.

**Why:** the list is the library's own contract, and a real signature check in the tests proves
that a wrong audience, an expired token and a tampered signature are refused *by the code that
runs in production*, not by a mock returning what the test told it to.

**Reverse it if:** a google-auth upgrade drops list support; the test for the Android audience
fails first.

### D15. Profile and email follow Google on every sign-in

**Decided:** name and avatar are refreshed from the claims at every sign-in (written only when
changed). When a `sub` match arrives with a different email, the account's email follows it,
unless another account already holds that address (then the old one is kept). Names are cut to
150 characters; an avatar URL longer than 500 is dropped, not cut.

**Alternatives:** set them once at sign-up.

**Why:** Google is the only source of these fields in V1; there is no profile editing.

**Reverse it if:** users get a way to edit their name here; then stop overwriting it.

### D16. What the event trail records, and what it does not

**Decided:** `google_login_succeeded`, `google_login_failed` (with a fixed `reason` word:
`invalid_token`, `bad_issuer`, `email_unverified`, `missing_claims`, `inactive`, `sub_mismatch`,
`conflict`), `signed_up`, `logged_out`, `device_signed_out` (`reason: limit | user`) and
`login_blocked`. **No `tokens_refreshed`.** A failed sign-in stores the email only once Google has
vouched for it. The snapshot column is `email` (the reference's `username`; there is no username).

**Alternatives:** record every refresh.

**Why:** a refresh happens every 30 minutes per device; recording it would bury the events that
matter, and `SignedInDevice.last_seen_at` already says when a device was last used. The reason
word is never shown to the client (which always gets one generic refusal) but tells a reviewer
what happened.

**Reverse it if:** you need a per-refresh history for an investigation.

### D17. A device is a refresh-token chain only; signed out with `DELETE`

**Decided:** `SignedInDevice` has no `kind` or `session_key`: the only session in this project is
the admin's. Signing one out is `DELETE auth/devices/<id>/` (the reference: `POST
…/<id>/sign-out/`).

**Alternatives:** keep the reference's model and verb.

**Why:** unused columns invite code paths nobody tests; `DELETE` on the device resource is the
plain REST reading of "remove this device".

**Reverse it if:** web sessions for users ever come back.

### D18. Tokens carry the device id, so a request knows which device it is

**Decided:** `issue_tokens` puts the `SignedInDevice` id in a `device` claim. simplejwt copies it
into every access token and keeps it through rotation. The devices list marks `current: true`
for the caller's own device.

**Alternatives:** the reference's answer: an API device cannot tell which one it is.

**Why:** a client needs "this device" to show the list sensibly (and to warn before signing
itself out). The claim is an id the caller can already see in the list, so nothing leaks.

**Reverse it if:** never needed; it costs one small claim.

### D19. A signed-out device's access token lives out its 30 minutes

**Decided:** ending a device (limit or `DELETE`) and logging out blacklist the refresh token.
The access token beside it keeps working until it expires (`JWT_ACCESS_MINUTES`, default 30).

**Alternatives:** check on every request that the access token's `device` still exists (one
indexed query per request).

**Why:** as the reference: stateless access tokens are the point of JWT, and 30 minutes bounds
the exposure. The `device` claim (D18) makes the stricter check a small change — parked in
BACKLOG.md.

**Reverse it if:** immediate sign-out becomes a requirement.

### D20. A refresh locks the old token's row

**Decided:** `RefreshView` runs in a transaction and `select_for_update`s the presented token's
`OutstandingToken` row before simplejwt checks the blacklist.

**Alternatives:** trust simplejwt as it is.

**Why:** without it, two requests with the same refresh token both pass the blacklist check
before either writes to it, and both get a live chain — the replay rotation exists to stop, and
it also let the second chain count as a new device and push out a real one. A
`TransactionTestCase` with two threads shows `[200, 200]` without the lock and `[200, 401]` with it.

**Reverse it if:** simplejwt makes rotation atomic itself.

### D21. Rate limits: failed Google sign-ins per address, plus the `auth` throttle

**Decided:** `ratelimit.py` keeps only the per-address cap on *failed* Google sign-ins (50 per 15
minutes, the reference's `login-ip` numbers); a blocked attempt is a 429 `rate_limited` and a
`login_blocked` event; a success does not clear the count. `auth/google/`, `auth/refresh/` and
`auth/logout/` share DRF's `auth` scope (30/hour by default, per address for these
unauthenticated views).

**Alternatives:** the reference's per-username and per-account caps (there is no username or
password); leaving refresh out of the `auth` scope.

**Why:** before verification only the address is known. Refresh is in the scope because it is the
other endpoint that accepts a bearer credential from anyone; at one refresh per device per 30
minutes, 30/hour leaves room for a household behind one address.

**Reverse it if:** a NAT'd office hits 30/hour; raise `API_AUTH_THROTTLE`.

### D22. Expired tokens are flushed daily

**Decided:** a beat task runs simplejwt's `flushexpiredtokens` daily, beside the security-event
purge.

**Alternatives:** none in the reference, where the table just grows.

**Why:** rotation leaves one `OutstandingToken` row per refresh (about 50 a day per active
device). An expired token is refused on its `exp` alone, so the row proves nothing, and
`devices.prune` treats a missing token as dead.

**Reverse it if:** you want token history for investigations; keep them and archive instead.

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

## Phase 3a — chunker and embedding providers

### D33. Chunk sizes in characters: target 1600, max 2000, overlap 200

**Decided:** `CHUNK_TARGET_CHARS=1600`, `CHUNK_MAX_CHARS=2000`, `CHUNK_OVERLAP_CHARS=200`, taking
~4 characters per token: ~400 / ~500 / ~50 tokens, inside the plan's 300–500. No chunk's `text` is
longer than the max.

**Alternatives:** count real tokens with a tokenizer (tiktoken, or the vendor's count endpoint).

**Why:** a tokenizer is a new dependency and differs per provider; the sizes only need to be
roughly right. Both models accept far more (8192 and 2048 tokens), so an underestimate costs
nothing but a slightly larger chunk.

**Reverse it if:** the eval (Phase 4) shows recall moving with chunk size, or notes are mostly in a
script where 4 chars/token is far off (CJK); tune the settings first, add a tokenizer only if that
is not enough.

### D34. Chunks follow structure; items are atomic; overlap is whole blocks

**Decided:** walk the TipTap tree. A heading starts a section; a chunk never spans two, and overlap
never crosses one. A list or checklist item's own words are one block (nested items are their own
blocks); only a single block longer than the max is split, on lines, then sentences, then words,
then raw length. Overlap carries whole blocks, or a paragraph's last whole sentences, never part of
an item. Headings with nothing under them become a chunk of their own words.

**Alternatives:** chunk the flat `content_text` with a sliding character window (simpler; splits
items and ignores headings).

**Why:** the plan requires it (§6.1), and a checklist item cut in half is two wrong facts.

**Reverse it if:** notes turn out to be mostly unstructured walls of text, where structure buys
nothing.

### D35. `embed_text` carries the title and heading path; the hash covers `embed_text`

**Decided:** `embed_text = "<title> > <heading path>\n\n<text>"`; `content_hash = sha256(embed_text)`.
`text` stays unprefixed for display.

**Alternatives:** hash only `text`, so a rename does not re-embed.

**Why:** the prefix is part of what is embedded, so a renamed note's old vectors really are stale.
Reusing them would leave search matching the old title. The cost is re-embedding a whole note on a
rename, which is rare.

**Reverse it if:** renames turn out to be frequent and expensive; then drop the title from the
prefix, not the hash.

### D36. `EMBEDDING_DIMENSIONS = 1536`, fixed per deployment; the model id is stored per vector

**Decided:** 1536 for both `text-embedding-3-small` (native) and `gemini-embedding-001` (via
`outputDimensionality`). The boundary rejects any vector of another size. `embedding_model_id()`
(`provider/model@dims`) is stored beside each vector so a chunk is re-embedded when it differs,
even if its hash does not.

**Alternatives:** 768 (smaller index, lower quality); 3072 (Gemini's native size, above pgvector
HNSW's 2000-dimension limit).

**Why:** the one size both candidate models support at good quality, and under the HNSW limit.
Fixing it in the migration means changing the model or size later is a migration plus
`reindex_notes --all` (Phase 3b) — a deliberate, visible operation.

**Reverse it if:** the chosen model does better at another size in the eval; migrate and re-index.

### D37. Both OpenAI and Gemini, over plain `requests`; `EMBEDDING_MODELS` per provider

**Decided:** implement both real providers against the REST APIs with `requests` (already a
dependency) and fixed timeouts (5 s connect, 30 s read). Settings follow the reference's
`BILL_SCAN_MODELS`: `EMBEDDING_MODELS = {"openai": …, "gemini": …}` instead of the plan's single
`EMBEDDING_MODEL`. The Protocol takes a `task` ("document" / "query") so Gemini can use `taskType`.
`EMBEDDING_BATCH_SIZE = 64`.

**Alternatives:** the openai and google-genai SDKs, as the reference; one provider only (the plan's
"implement one first, ask").

**Why:** the owner has not chosen yet, and the SDKs would be two new dependencies for one POST each.
Both are small and mocked-tested, so the choice is a setting. 64 is under Gemini's 100-per-call
limit.

**Reverse it if:** a provider needs features the REST shape makes awkward (streaming, retries with
vendor-specific backoff), or the owner wants only one kept; delete the other.

### D38. Transient failures raise `EmbeddingTransientError`, which is not an `EmbeddingError`

**Decided:** 429, 5xx, timeouts and connection errors raise `EmbeddingTransientError` for the
indexing task's `autoretry_for`. OpenAI's 429 `insufficient_quota` is an `EmbeddingError`: waiting
does not add credit. The two classes are unrelated, so `except EmbeddingError` never swallows a
retryable failure. Vendor messages are quoted with request header values redacted.

**Alternatives:** let `requests` exceptions through, as the reference lets SDK exceptions through
(but an HTTP 429 is not an exception in `requests`); a subclass of `EmbeddingError`.

**Why:** one retryable class at the boundary, independent of vendor. Redaction because OpenAI's 401
quotes the key it was sent.

**Reverse it if:** a caller needs to treat both alike; catch both explicitly.

### D39. The fake provider is a hashed bag of words

**Decided:** lowercased `\w+` tokens hashed (sha256) to a slot and a ±1 sign, summed, L2-normalised.
A text whose words all cancel falls back to one slot for the whole text, so no vector is zero.

**Alternatives:** a random unit vector seeded by the text's hash (meaningless similarity).

**Why:** deterministic across machines like the alternative, but texts sharing words score higher,
so search and eval smoke tests without a key give sensible orderings — useful in CI and a fresh
clone.

**Reverse it if:** tests start depending on its similarity numbers in ways that break when it is
tuned; keep assertions to orderings.

### D40. Live provider tests need the key *and* `LIVE_PROVIDER_TESTS=1`

**Decided:** each real provider's single live test is skipped unless its key is set and
`LIVE_PROVIDER_TESTS=1`. It uses `override_settings` to beat the runner's forced `fake`.

**Alternatives:** the plan's "skipped unless its key is set".

**Why:** `environ.Env.read_env` copies `.env` into `os.environ`, so a key alone would make every
suite run on a configured machine spend money, which D11 forbids.

**Reverse it if:** keys move out of `.env` (e.g. into the process manager only).

## Phase 3b — indexing

### D61. Indexing is debounced by a countdown, not by coalescing

**Decided:** every write enqueues `index_note_task(note_id, version)` on commit with
`countdown=INDEX_DEBOUNCE_SECONDS` (20). A tombstone is enqueued with no countdown.

**Alternatives:** coalesce in Redis (one pending key per note, reset on each write); index
synchronously in the request.

**Why:** the plan's design, and it needs no state beyond the task itself. Autosave enqueues a task per
write; all but the last find `note.version` moved on and return before embedding. The cost is a few
cheap no-op tasks. Deletes are not debounced because de-indexing is free and should be prompt.

**Reverse it if:** the no-op tasks crowd the queue; then coalesce with a key per note.

### D62. Chunks are reused by `(content_hash, embedding_model)`, matched in ordinal order

**Decided:** a stored chunk is reused when its hash and model id match a new chunk; its row is
updated (ordinal, text, heading path, note version) and keeps its vector. Identical chunks in one
note (repeated paragraphs) are matched to existing rows first-come in ordinal order, and embedded once
when new. Leftover rows are deleted.

**Alternatives:** delete and recreate every chunk, embedding only the new hashes (simpler, churns
the HNSW index); key reuse by ordinal (an insert at the top would re-embed everything).

**Why:** a one-word edit re-embeds one chunk, and reordering sections re-embeds none. The model id is
part of the key so a model change re-embeds everything without a separate flag (D36).

**Reverse it if:** row updates prove slower than delete and insert for large notes.

### D63. Embed outside the transaction, then lock the note and re-check its version

**Decided:** the provider call happens with no lock held. The write then takes `select_for_update`
on the note row and aborts silently if its version is no longer the task's. Existing rows are read
again under that lock; vectors from the earlier read fill any row that vanished meanwhile.

**Alternatives:** embed inside the transaction (holds a lock and a connection for seconds of network);
no re-check (a slow task could overwrite newer chunks with older text).

**Why:** `acks_late` means duplicate and overlapping deliveries are normal. Locking the note row
serialises them, and the version check makes the newest task the only one that writes.

**Reverse it if:** never the lock-free embedding; the re-check could go if tasks were serialised per note.

### D64. The chunk full-text index covers `heading_path` and `text`

**Decided:** `chunk_search_vector()` is `SearchVector("heading_path", "text", config="english")`,
defined once in `retrieval/search_vector.py` and used by both the GIN index and (phase 4) the query.

**Alternatives:** text only; include the note title too.

**Why:** a chunk under "Risks" that never says "risks" should still match it, as it does for the
vector search, whose `embed_text` carries the path. The title is left out: it is the note's, it would
make every chunk of a note match a title word, and note-level search already covers it.

**Reverse it if:** the evaluation shows heading matches hurting keyword precision; drop the column
from the expression and migrate the index.

### D65. A permanent embedding error is logged and dropped; `index_status` shows the gap

**Decided:** `EmbeddingError` in the task is logged with its traceback and not retried or raised.
`EmbeddingTransientError` retries with backoff (max 5). The note keeps its old chunks.

**Alternatives:** raise (Celery records a failure, and with `acks_late` a worker crash loop is
possible); store a per-note "index failed" flag.

**Why:** retrying a bad key or a wrong-sized vector cannot succeed. The note stays searchable at its
previous version, and `index_status` reports it as behind; `reindex_notes` retries after the fix.

**Reverse it if:** silent staleness bites; add the flag and surface it.

---

## Phase 4a — evaluation fixtures

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

## Phase 4 — retrieval

### D66. Hybrid retrieval merged by reciprocal rank fusion, not vector-only

**Decided:** every search runs a vector leg (cosine over the HNSW index) and a keyword leg
(`websearch_to_tsquery` over the GIN index), `SEARCH_CANDIDATES` (50) each, merged by RRF:
score = Σ 1/(`SEARCH_RRF_K` + rank), `SEARCH_RRF_K` = 60, ties to the lower chunk id. Vector-only
and keyword-only exist as `mode=` for the evaluation.

**Alternatives:** vector only; a weighted sum of normalised scores.

**Why:** notes are full of names, codes and numbers ("Honda City", `select_for_update`, a PNR) that
embeddings blur and full-text matches exactly; paraphrases are the reverse. RRF uses ranks only, so
cosine similarity and `ts_rank` never have to be put on one scale, and it has one knob with a
well-known default. Fusion is a pure function, unit-tested on a hand-built example.

**Reverse it if:** the real-provider eval shows hybrid no better than vector on these fixtures and
the keyword leg's cost matters; or a weighted fusion measurably beats RRF.

### D67. The owner filter, live notes and the current model are SQL, in every query

**Decided:** both legs and the final read go through `_live_chunks(user)` =
`NoteChunk.objects.filter(owner=user, note__deleted_at__isnull=True)`. The vector leg also
filters `embedding_model = embedding_model_id()`.

**Alternatives:** fetch candidates, then drop other users' or deleted notes' chunks in Python;
rely on de-indexing for deletes.

**Why:** a Python filter after a top-N cut both leaks by construction (one bug from showing another
user's text) and under-fills (the N best may all be someone else's). De-indexing a deleted note
runs on commit and may lag or fail; the SQL filter hides it at once. A vector from another model is
in another space, so its distance is meaningless (D36); keyword search does not care. Tests
capture the SQL and check the `owner_id` clause, and prove another user's word-for-word copy never
appears in any mode.

**Reverse it if:** never for the owner filter.

### D68. HNSW `ef_search` raised per query; the planner may choose an exact scan

**Decided:** the vector query runs in a transaction with
`set_config('hnsw.ef_search', max(SEARCH_HNSW_EF_SEARCH, SEARCH_CANDIDATES), true)`,
`SEARCH_HNSW_EF_SEARCH` = 200. The query orders by the distance alone (`ORDER BY embedding <=> v
LIMIT 50`); equal distances are put in chunk-id order in Python after the fetch.

**Alternatives:** pgvector's default `ef_search` (40); `ORDER BY distance, id` in SQL; a partial
index per user.

**Why:** an HNSW scan returns at most `ef_search` rows, so 40 could never fill 50 candidates, and
pgvector 0.6 applies the `WHERE` clause *after* the index scan: other users' rows use up the list.
200 leaves room at small cost. A second sort key makes pgvector's index unusable (seq scan plus
sort), hence the Python tie-break. Measured with EXPLAIN: when the owner holds most of the table
the planner uses `chunk_embedding_hnsw` (a test asserts it on 1000 chunks); when the owner is a
small fraction, it prefers the owner btree and sorts that user's rows exactly, which is cheaper and
has no recall loss. Known limit: an owner who is a middling fraction of a large table can still get
fewer than 50 candidates from HNSW; pgvector 0.8's iterative scans fix that.

**Reverse it if:** pgvector is upgraded to 0.8+ (use `hnsw.iterative_scan`), or search latency
shows the 200 is too many.

### D69. Each hit carries the vector leg's `similarity` for the Ask relevance floor

**Decided:** `SearchHit` has `score` (the fused RRF score), `similarity` (1 − cosine distance, or
`None` if only the keyword leg found it) and `keyword_rank` (`ts_rank`, or `None`).

**Alternatives:** return only the fused score; normalise and blend the raw scores.

**Why:** an RRF score is about 1/(60 + rank) whatever the text says, so it cannot express
"irrelevant" (D58). Cosine similarity can, and `eval_retrieval` prints it for the no-answer
questions next to the answerable ones, which is the data `ASK_RELEVANCE_FLOOR` will be set from.
`keyword_rank` is kept for the same kind of tuning and for debugging, at no cost.

**Reverse it if:** Ask ends up flooring on something else (a reranker score).

### D70. A failed query embedding falls back to keyword-only in hybrid mode

**Decided:** in `mode="hybrid"`, `EmbeddingError` and `EmbeddingTransientError` from
`embed_query` are logged as a warning and the search continues with the keyword leg alone (every
`similarity` is `None`). `mode="vector"` raises.

**Alternatives:** raise, and let the endpoint return 503.

**Why:** a search box that fails whenever the provider rate-limits or is down is worse than one that
ranks by keyword for a while; the result is a weaker ranking, not a wrong one. The warning makes an
outage visible in the logs. Ask (Phase 5) must treat all-`None` similarities as "cannot judge
relevance", not as relevance. The eval uses vector mode first, so a provider failure stops it
instead of quietly skewing the hybrid numbers.

**Reverse it if:** silent degradation hides a broken key for long; then add a metric or alert, or
surface a `degraded` flag in the response.

### D71. At most two chunks per note, applied after fusion and before the cut to k

**Decided:** `SEARCH_MAX_CHUNKS_PER_NOTE` = 2. The fused list is walked in order, a note's third
and later chunks are dropped, then the top k (`SEARCH_DEFAULT_K` 8, at most `SEARCH_MAX_K` 20) are
returned.

**Alternatives:** no cap; one chunk per note (a note-level result list).

**Why:** a long note on the topic would otherwise fill all k places, leaving Ask one source and the
search page one note. Two keeps a second section of the best note (answers often span a heading)
while leaving room for other notes. Applying it before the cut means k still means k results.

**Reverse it if:** the eval's section questions show the answer's section pushed out by the cap.

### D72. The retrieval keyword leg matches any word of the query (OR)

**Decided (by me, from the evaluation):** `retrieval/search.py`'s keyword leg ORs the query's
words, each as its own plain `SearchQuery`, and ranks by `ts_rank`. The notes list's `?q=` keeps
`websearch` AND semantics.

**Alternatives:** `websearch_to_tsquery` (every word required), as first built; `plainto` of the
whole query (also AND).

**Why:** search here is fed whole questions. Requiring every word made the keyword leg almost
silent: recall@5 0.179 on the evaluation set, 0.839 with OR (fake provider). Matching more words
still ranks higher, and fusion with the vector leg decides the final order. Building the OR from
plain per-word queries means no user text is ever parsed as tsquery syntax. The notes list is
different: a person types two or three keywords and expects all of them.

**Reverse it if:** real-provider evaluation shows OR's noise costs hybrid precision; then try
requiring at least two words, or weight the legs in RRF.

## Phase 5a — chat providers and prompt

### D53. The chat providers call each vendor's HTTP API with `requests`, not its SDK

**Decided:** `assistant/chat/providers/{claude,openai,gemini}.py` POST JSON through one shared
helper (`_http.py`) that sets a (5 s connect, `CHAT_TIMEOUT_SECONDS` read) timeout, reads the key
from the vendor's usual env var and translates failures.

**Alternatives:** the `anthropic`, `openai` and `google-genai` SDKs, as the reference's bill
scanner uses.

**Why:** plan §3 allows no new dependency without asking, `requests` is already pinned (for
google-auth), and each provider is one POST. The SDKs' value here would be retries and typed
errors, which the helper replaces in about fifty lines; Celery does the retrying.

**Reverse it if:** a provider needs streaming, tool use or file uploads; then add that vendor's
SDK (after asking).

### D54. `ChatError` gives up, `TransientChatError` retries, and neither subclasses the other

**Decided:** 408, 409, 429, every 5xx (Anthropic's 529 included), timeouts and dropped
connections raise `TransientChatError`, for the ask task's `autoretry_for`. Everything else raises
`ChatError`: bad key or missing key, unknown model, rejected request, non-JSON body, a refusal or
safety block, and an answer **cut off by the token ceiling** (not stored half-finished: it may end
mid-claim or mid-citation, and a failed ask does not count against the quota).

**Alternatives:** let the vendor's exceptions propagate, as the reference does with SDK
exceptions (with `requests` there are no vendor exceptions to propagate); make the transient class
a `ChatError` subclass.

**Why:** separate classes mean `except ChatError: mark_failed()` in the task cannot swallow a
failure Celery was meant to retry.

**Reverse it if:** truncation turns out common in practice; then keep the partial answer with a
flag instead of failing.

### D55. Cheap default models: `claude-haiku-4-5`, `gemini-2.5-flash-lite`, `gpt-5-mini`

**Decided:** `CHAT_MODELS` defaults, each overridable (`CHAT_CLAUDE_MODEL`, …). Claude is given
by its alias `claude-haiku-4-5`, which the claude-api skill lists as the current ID (the dated
snapshot is `claude-haiku-4-5-20251001`; the result records whichever the API reports). No
`thinking` is sent to Claude; OpenAI gets `reasoning.effort: "low"` and `store: false`.
`CHAT_MAX_OUTPUT_TOKENS` defaults to 2048 because OpenAI counts hidden reasoning against it.

**Alternatives:** Sonnet 5.5 / Opus 5.5 class models.

**Why:** answering from eight excerpts is reading comprehension; the quota prices every ask the
same, and the cheap tier costs a fraction of the others. Gemini matches the reference's choice.

**Reverse it if:** the Phase 5 evaluation shows the cheap tier ignoring the grounding rules
(uncited claims, guessing). A thinking model then needs a larger `CHAT_MAX_OUTPUT_TOKENS`, and
Sonnet/Opus 5.5 would want the refusal `fallbacks` parameter the claude-api skill recommends.

### D56. Excerpts sit in `<excerpt>` tags; only the prompt's own tag names are neutralised

**Decided:** each excerpt is `<excerpt n="1" title="…" section="…">text</excerpt>` inside
`<excerpts>`, then `<question>`. Inside excerpt text and the question, any opening or closing
`excerpt`/`excerpts`/`question` tag (any case or spacing) has its `<` replaced by `&lt;`; titles and
heading paths are HTML-escaped and put on one line. The system prompt says excerpts are data and
their instructions are not to be followed.

**Alternatives:** HTML-escape all excerpt text; random per-request delimiters; JSON.

**Why:** escaping only our tag names leaves code, maths and HTML in notes exactly as written (a
model that sees `&lt;` everywhere tends to quote it back), while still making it impossible for
note text to close its excerpt. Random delimiters would defeat reproducible prompts and tests.

**Reverse it if:** a red-team prompt gets through; add a random nonce to the tag names.

### D57. The excerpt budget keeps whole excerpts in rank order

**Decided:** `fit_excerpts` adds excerpts in the given (rank) order until `ASK_EXCERPT_MAX_CHARS`
(12,000) would overflow; the overflowing one is cut at a word boundary if at least 200 characters
remain, and the rest are dropped. The best match is always sent, truncated if needed. The ask
task should parse citations against `fit_excerpts`' output, i.e. what the model actually saw.
`build_messages` keeps the `(system, user)` return the brief asked for and calls `fit_excerpts`
itself (it is idempotent).

**Alternatives:** trim every excerpt proportionally; count tokens exactly.

**Why:** the top-ranked chunks matter most, and a 200-character scrap cut from its context
misleads more than it helps. Characters approximate tokens as the chunker does.

**Reverse it if:** answers often miss facts in the lower-ranked excerpts; raise the budget.

### D58. `ASK_RELEVANCE_FLOOR` defaults to 0.0 until the evaluation tunes it

**Decided:** the setting exists (float, env-overridable) with 0.0, meaning only an empty
retrieval short-circuits to `ASK_NO_ANSWER_TEXT`.

**Alternatives:** guess a number now.

**Why:** the floor's meaning depends on which score retrieval returns. A fused RRF score is about
`1/(60 + rank)` whatever the text says, so it cannot express "irrelevant" at all; the vector leg's
cosine similarity can. Picking a value before retrieval (Phase 4) and its eval numbers exist would
be a guess. See BACKLOG.

**Reverse it if:** Phase 4/5 lands; set it from the eval, against the score it actually applies to.

### D59. Citation markers: `[1]`, `[1][2]`, `[1, 2]`, `[1; 2]` and `[1-3]`

**Decided:** all read; ranges (hyphen or en dash, either direction) expand only over existing
excerpt numbers. Not markers: `[^1]`, `[1a]`, `[see above]`, Markdown links, `[1,]`.

**Alternatives:** `[n]` only, as the prompt asks.

**Why:** models drift into the grouped and range forms even when told otherwise; losing a
citation for its punctuation would be worse than accepting it. Bounding ranges by the excerpt set
keeps `[1-999999999]` cheap.

**Reverse it if:** a false positive shows up in real answers.

### D60. Unknown markers are dropped from `citations` but left in the answer text

**Decided:** `parse_citations` keeps only numbers that name a sent excerpt, de-duplicated in
first-appearance order, each keeping its own `n` (no renumbering). The answer is stored exactly as
the model wrote it; the client links only the numbers present in `citations`. Snippets are the
excerpt text on one line, cut at a word boundary to 240 characters (a constant).

**Alternatives:** strip invalid markers from the text; renumber citations 1..m and rewrite the
text to match.

**Why:** the verbatim answer is what debugging and evaluation need (an invented `[9]` is itself a
finding), and stripping "markers" would also eat an innocent `[2024]`. Keeping `n` means every
marker in the text still matches its citation.

**Reverse it if:** users find unlinked numbers confusing; strip them at render time in the client.

## Phase 5 — ask

### D73. The quota is counted from `AskQuery` rows, under the user's row lock

**Decided:** `create_ask` locks the user's row (`select_for_update`), then counts this calendar
month's (Asia/Kolkata) `AskQuery` rows whose status is not `failed`, and creates the new row only if
the count is under `ASK_QUOTAS[plan]` — all in one transaction. The plan is read from the locked
row. `me/` reports the same count as `ask_usage`.

**Alternatives:** a counter column on `User` (or in Redis) incremented per ask; counting without a
lock; a unique "slot number" per user and month.

**Why:** the rows already record every ask, so a separate counter is a second source of truth that
has to be decremented when an ask fails and can drift. The lock is D5's pattern and for the same
reason: a check followed by a write is only safe if nobody can check in between. Without it, two
asks at the edge both count `limit − 1` and both pass; a test forces exactly that interleaving with
the lock removed, and shows the limit breached. The count is one indexed query on
`(user, created_at)`.

**Reverse it if:** asks become frequent enough per user that serialising them on one row hurts
(they are one at a time by nature today), or quotas need other units (tokens, cost).

### D74. The relevance floor passes a hit on cosine similarity or on any keyword match

**Decided:** `is_relevant(hits)` is true if any hit's `similarity` is at least
`ASK_RELEVANCE_FLOOR`, or any hit was matched by the keyword leg (`keyword_rank` is not null).
Otherwise the task stores `ASK_NO_ANSWER_TEXT` as a done answer with no provider call. The fused
RRF `score` is never compared (D69). A floor-answered ask counts against the quota.

**Alternatives:** similarity only (keyword-only hits count as below the floor); a second threshold
on `ts_rank`; requiring both.

**Why:** the two mistakes cost different amounts. Calling the model when the notes are irrelevant
costs one cheap call, and the prompt already makes the model say the notes don't cover it.
Skipping the call when they are relevant tells the user their notes say nothing when they do.
A keyword hit means words of the question appear in the note (stemmed, stopwords dropped), which
is exactly where embeddings are weakest: codes, names, rare terms. `ts_rank` is no better
calibrated than RRF, so a threshold on it would be a guess. The consequence: the floor
short-circuits only when no chunk shares a single content word with the question and none is
close by meaning. The floor value itself stays 0.0 until the real-provider eval sets it (D58).

**Reverse it if:** the eval shows keyword-only hits on no-answer questions are common and costly;
then require a minimum similarity even for keyword hits, or a minimum share of matched words.

### D75. Idempotency: one key, one question, one ask; a replay is 200, a reuse is 422

**Decided:** `POST ask/` requires an `Idempotency-Key` header of 1–100 characters from
`[A-Za-z0-9_-]` (a UUID fits); missing is 400 `idempotency_key_required`, malformed is 400
`idempotency_key_invalid`. Keys are unique per user (a DB constraint). A new key → 202 with the
pending ask. The same key and the same question (after trimming) → 200 with the existing ask,
whatever its status, counted once and not re-enqueued, even if the quota has filled since. The same
key with a different question → 422 `idempotency_key_reused`. The lookup happens under the user
lock, before the quota check.

**Alternatives:** 202 for a replay too; returning the old ask for a different question; keying on
a hash of the question; letting a failed ask's key retry it.

**Why:** a replay is the retry of one request, so it must return what the first returned and must
not cost a second ask — including when the retry arrives after the month filled up. 200 tells the
client "nothing new was started" while the body is the same shape it already handles. A different
question under the same key is a client bug; answering it with the old ask would show an answer to
a question not asked. A failed ask stays failed under its key: retrying is a new request, with a
new key, and costs nothing because failed asks don't count. The narrow character set keeps keys
safe in logs and URLs.

**Reverse it if:** clients need to retry a failed ask under the same key; then reset the row to
pending on replay of a failed one.

### D76. Every way out of the task ends in done or failed, handled in the task body

**Decided:** `answer_ask` handles its expected failures where they happen (`ChatError` and
`EmbeddingError` → failed with a fixed user-facing message; the vendor's message goes only to the
log). Around that, the task body catches a transient error on the last allowed attempt
(`request.retries >= max_retries`) and marks the ask failed ("busy") instead of re-raising, and
catches anything else (the soft time limit, a bug), marks the ask failed and re-raises. The claim
(pending/running → running) and the final write are conditional updates, so a duplicate run
(`acks_late`) never overwrites a finished ask; a `running` ask is taken up again, since that is
what a redelivery after a crash looks like.

**Alternatives:** `Task.on_failure`; a periodic sweeper as the only safety net.

**Why:** an ask left `running` would be polled forever and, never failing, would count against the
quota. `on_failure` would work on a worker, but with eager Celery and `task_eager_propagates` (the
test runner) it is never called and retries raise rather than re-run, so the behaviour could not be
tested. In the body it behaves and is tested the same both ways.

**Reverse it if:** the tests stop running Celery eagerly; `on_failure` is then testable and keeps
the task body shorter. Not covered either way: a worker killed at the hard time limit runs no code
at all (BACKLOG, the stuck-ask sweeper).

## Phase 6a — web client first pass

### D46. No router in the web client: a view store instead

**Decided:** `web/src/stores/view.ts` holds the screen (`list` | `note` | `ask`), the open note's
id, and where "back" from a note goes. There is no vue-router.

**Alternatives:** vue-router with hash history.

**Why:** the plan's web stack (§3) does not list a router, and three screens need one string, not
a dependency. Capacitor wraps a single page anyway.

**Reverse it if:** deep links or the browser/Android back button become a requirement (Phase 7's
back button is the likely trigger); add vue-router then.

### D47. JWTs in localStorage

**Decided:** the access and refresh tokens are stored in `localStorage` and sent as a bearer
header. They are read fresh on each request, so tabs share one session.

**Alternatives:** an httpOnly refresh cookie (needs cookie + CSRF handling in a JWT-only API, and
`CORS_ALLOW_CREDENTIALS`, which is off); memory only (every reload signs out).

**Why:** the API is bearer-only by design (D4), and the same code runs in the Capacitor WebView.
The trade-off is XSS: any script running in the page can read both tokens. It is contained by
rendering untrusted text (model answers, device labels) only as text — never `v-html` (D50) —
by loading no third-party script once signed in (D51), by short-lived access tokens (30 min), and
by rotating, blacklisted refresh tokens plus the device list, from which a stolen session can be
signed out.

**Reverse it if:** the web client gains a same-origin deployment; then move the refresh token to
an httpOnly `SameSite=Strict` cookie and keep only the access token in memory.

### D48. Single-flight refresh, across tabs with Web Locks

**Decided:** on a 401 the client refreshes once and retries once. Concurrent failures share one
refresh promise, and where the browser has `navigator.locks` the refresh runs under a named lock;
inside it, an access token that changed since the failed request means another tab already
refreshed, and that pair is used. A refused refresh signs out; a network failure keeps the tokens.

**Why:** refresh tokens rotate and the old one is blacklisted, so two tabs refreshing at once
would have one of them refused and signed out for no reason.

**Reverse it if:** the backend stops rotating refresh tokens.

### D49. The sync revision lives in memory

**Decided:** `lastRevision` is not persisted; every page load syncs from `after=0`, which fetches
every note. The unfiltered list is the local map; a search or type filter asks `notes/?q=&type=`.

**Why:** offline-first storage is out of V1 (plan §1). A full pull is fine at personal-notes
scale, and it means no stale local cache can survive a sign-out.

**Reverse it if:** accounts grow to thousands of notes or offline mode arrives; persist notes and
the revision in IndexedDB then.

### D50. Model output is rendered as text only

**Decided:** an answer is split on `[n]` / `[n, m]` markers into text segments and citation
buttons (`web/src/lib/citations.ts`); nothing from the model goes through `v-html`. A marker with
no matching citation stays as literal text.

**Alternatives:** render the answer as Markdown/HTML with a sanitiser.

**Why:** answers are built from note text, which can contain anything, and tokens live in
`localStorage` (D47). Text rendering makes injection impossible by construction.

**Reverse it if:** formatted answers are wanted; add a Markdown renderer with a strict allow-list
sanitiser, and ask before adding the dependency.

### D51. Google Identity Services loaded at runtime, on the sign-in screen only

**Decided:** the GIS script is injected from `https://accounts.google.com/gsi/client` when the
sign-in screen mounts. Sign-in is Google-only; there is no development bypass.

**Why:** Google requires the script from its own origin. Loading it only when signed out keeps
third-party code out of signed-in page loads (D47).

**Reverse it if:** Phase 7 swaps it for the native Capacitor plugin on Android.

### D52. Conflict prompt resolves whole notes

**Decided:** on a 409 the editor offers "Keep mine" (re-PATCH on top of the server's `version`,
overwriting it) or "Take theirs" (load the server copy, dropping unsaved local edits). A newer
copy arriving through sync replaces the editor's content only when nothing is unsaved.

**Alternatives:** a three-way merge of the TipTap documents.

**Why:** the plan asks for a simple keep mine / take theirs prompt, and a correct rich-text merge
is a project of its own.

**Reverse it if:** conflicts turn out to be common in use (a phone and a laptop open at once).

## V2

### D77. V2 is built on a `v2` integration branch, one feature branch per phase

**Decided (by the owner, 2026-10-01):** `master` stays the released V1 line and takes fixes only.
`v2`, cut from `master` at `phase-6-web`, is V2's integration branch. Each phase is built on a
`v2-feat/<phase>-<slug>` branch and merged into `v2` with `--no-ff` after review and green checks, then
tagged `v2-phase-N-<name>`. Fixes on `master` are merged into `v2` the same day. At release, `v2`
is merged into `master` and tagged `v2.0.0`. Full workflow: `docs/V2_PLAN.md` §1.

**Alternatives:** V1's linear `master` with tags (D1); trunk-based work on `master` behind feature
flags.

**Why:** V2 is a version update built over weeks while V1 must stay releasable and fixable. An
integration branch keeps V1 untouched, and `--no-ff` merges keep each feature a single, revertible
unit in the history.

**Reverse it if:** V2 work stalls and fixes pile up on both lines; then merge `v2` early and
continue on `master` with flags.

### D78. A periodic sweeper fails asks stuck past a cutoff, in one conditional UPDATE

**Decided:** `assistant.tasks.sweep_stuck_asks`, scheduled every 5 minutes, runs one
`UPDATE ... WHERE status IN (pending, running) AND created_at < now - ASK_STUCK_AFTER_SECONDS`
setting `failed`, `completed_at` and the error "This took too long. Please ask again." The cutoff
defaults to 3,600 s: a live ask can take 5 attempts (`max_retries=4`), each bounded by the 600 s
hard time limit, plus at most 1+2+4+8 s of backoff -- 3,015 s -- and the rest is headroom for
queueing. A failed ask drops out of the quota by itself (`quota.used` excludes failed).

**Alternatives:** cutoff measured from when the ask went `running` (needs a new column and a
migration); a short cutoff just above the time limit (would fail asks still retrying); a lock or
`SELECT ... FOR UPDATE` per row.

**Why:** D76 cannot reach a worker killed at the hard limit, or a lost message: no code runs. The
WHERE clause does the race-proofing -- an ask that finished between the sweeper's clock reading and
its write no longer matches, so it can never be overwritten. `created_at` is what exists; pending
asks are covered by the same rule.

**Reverse it if:** asks are routinely slower than an hour to start (a long backlog): raise the
setting, or move to a `started_at` column and sweep from that.

### D79. Nested JSON is refused by a project JSONParser that turns RecursionError into ParseError

**Decided:** `config.api.parsers.JSONParser` wraps DRF's and converts `RecursionError` to
`ParseError` (400, `code: "parse_error"`). `DEFAULT_PARSER_CLASSES` lists it with DRF's own
`FormParser` and `MultiPartParser`, so nothing else changes. `ValueError` needs no handling: DRF's
parser already turns it into a `ParseError`.

**Alternatives:** a depth check before parsing (a second pass over the body); a middleware; a
lower recursion limit.

**Why:** `json.loads` raises `RecursionError`, which is not a `ValueError`, past roughly 20,000
levels on Python 3.12 (the exact depth depends on the interpreter). That is about 40 KB, well under
the body size limit, so a 500 was one cheap request away. Catching it where the parse happens is the
smallest change and costs nothing on valid input.

**Reverse it if:** bodies nested only a few thousand levels (which parse fine) prove to hurt
elsewhere, say in validation or rendering; then cap depth explicitly instead of relying on the
interpreter's limit.

### D80. The 413 adds CORS headers itself, for allowed origins only

**Decided:** `MaxUploadSizeMiddleware` stays first. On its 413 it sets
`Access-Control-Allow-Origin` (echoing the origin) and `Access-Control-Expose-Headers` only when the
request `Origin` is in `CORS_ALLOWED_ORIGINS` and the path matches `CORS_URLS_REGEX`, and always adds
`Vary: Origin`.

**Alternatives:** move `CorsMiddleware` ahead of the size check.

**Why:** the point of the check is to refuse before anything touches the request, and the
Content-Length check must stay ahead of everything that could read the body. Moving CORS up would
make that an ordering convention between two middlewares instead of a fact of the list, and would
also make the 413 pass through CORS's own processing. Repeating the allow-list rule for one response
is ten lines and leaves the order alone. A disallowed origin gets no CORS header, as with any other
response, so the browser still blocks it.

**Reverse it if:** CORS configuration grows beyond `CORS_ALLOWED_ORIGINS` (regexes, allow-all): the
copied rule would then drift from `django-cors-headers`; move `CorsMiddleware` first instead. Only
the plain list is honoured here.

### D81. Google's signing keys are cached by a wrapping transport, with a retry on failure

**Decided:** `accounts.google.CachedCertsRequest` wraps the google-auth transport passed to
`verify_oauth2_token`. For a GET of the certificates URL it serves the body from Django's cache
(key `google-certs:<url>`), or fetches it and stores it for the response's `Cache-Control: max-age`,
capped at `CERTS_MAX_TTL` (1 hour). No `max-age`, `no-store`/`no-cache`, or a non-200 means nothing
is cached. If verification fails on keys that came from the cache, the entry is dropped and the
token is verified once more against a fresh fetch, so a key rotation (an unknown `kid`) heals
itself; a token that is truly bad fails the second time too.

**Alternatives:** subclassing `google.auth.transport.requests.Request`; caching inside
`_fetch_certs` by patching the library; `cachecontrol` on the `requests` session (a new dependency);
a fixed TTL ignoring the header.

**Why:** `_fetch_certs` makes one `request(certs_url, method="GET")` and reads `.status` and
`.data`, so a callable wrapper is the whole seam and needs no library internals beyond the URL
constant. Wrapping (not subclassing) leaves `_transport()`, and every test that patches it,
unchanged. The cap plus retry keep "fail closed" true without failing forever: the worst case after
a rotation is one extra fetch, and a stale entry can live at most an hour. The retry also fires for
an expired or wrong-audience token that arrives while keys are cached, costing the fetch sign-in
made before caching; the sign-in throttle bounds that. Tests turn a `LocMemCache` on, since the
runner uses `DummyCache`.

**Reverse it if:** Google changes the certificates URL or format (the wrapper matches the library's
private `_GOOGLE_OAUTH2_CERTS_URL`; a library upgrade that renames it will fail the tests), or the
cache backend is per-process and hit rates are poor.

### D82. Sentence splitting skips common abbreviations and initials

**Decided:** `_SENTENCES` in `retrieval/chunking.py` no longer splits after `e.g.`, `i.e.`, `etc.`,
`Dr.`, `Mr.`, `Mrs.`, `Ms.`, `vs.`, `approx.`, `No.` or a single capital letter plus a dot
(`J. R. R.`), via fixed-width negative lookbehinds anchored on `\b`. Output stays deterministic.

**Alternatives:** a tokenizer such as NLTK's punkt (a dependency and a data download); a
sentence-start heuristic (next word capitalised); leaving it.

**Why:** a split inside "e.g. this" or "Dr. Who" produces a chunk that starts mid-sentence and
embeds worse. Not splitting after `etc.` or `No.` when they do end a sentence only makes a chunk
slightly longer, never wrong. Case matters for `No.` and initials (so "casino." and "go." still
end sentences); the others match any case.

**Reverse it if:** the evaluation shows the list costs more than it saves, or notes are
predominantly in a language with other abbreviations; then a real sentence segmenter is warranted.

**Consequence:** splitting changes for blocks long enough to be split by sentence (longer than
`CHUNK_MAX_CHARS`) that contain these abbreviations, so their chunks change and their content
hashes (D35) change. Those chunks re-embed on the note's next index; nothing else does.

### D83. The final PATCH on tab hide uses `keepalive`, through the API client, for small bodies

**Decided:** `RequestOptions` gains `keepalive`, threaded through `notesApi.update` and the notes
store; the editor's save passes it only from the `visibilitychange` (hidden) handler. `send` sets
`fetch`'s `keepalive` only when the serialized body is at most 60 KiB (`KEEPALIVE_MAX_BYTES`);
larger bodies go as a normal fetch. Timer- and unmount-driven saves are unchanged.

**Alternatives:** `navigator.sendBeacon` (POST only, no `Authorization` header); a raw `fetch` in
the component (bypasses the client's auth and error handling); keepalive on every save.

**Why:** browsers cap keepalive bodies at 64 KiB in total across in-flight requests, and reject
larger ones outright, so a big note would lose its save entirely; the fallback keeps today's
behaviour for it. The margin under 64 KiB leaves room for a second save. Keepalive only matters
when the page is going away, so it is not used elsewhere.

**Reverse it if:** notes routinely exceed 60 KiB (autosave's 1 s timer already covers them, so the
window is small) or the gap needs a real fix (a service worker or a sync queue). Limits: a 401
during that save triggers the normal refresh, which is not keepalive; and there is no web test
runner (BACKLOG), so this is verified by the build and by hand, not by a test.

### D84. Limits are two-layered — per user and system-wide — through one modular limits layer

**Decided (by the owner, 2026-10-02):** every capped resource is a *limit key*
(`ai_actions_per_month`, `ai_tokens_per_month`, `storage_bytes`, `attachments`, `signups_per_day`,
…). Each key has a **per-user** value (by plan) and a **system-wide** value, defined in one place:
an admin-editable `Limit` table with defaults in settings. Usage is counted from one ledger
(`UsageEvent`). A user over a limit gets `429 quota_exceeded` for that feature only — notes,
search and sync keep working. The system over a limit pauses that feature for everyone with
`503 system_limit_reached` and mails the admins. `signups_per_day` caps mass account creation.

**Alternatives:** a separate quota per feature in settings (V1's `ASK_QUOTAS`); per-user limits
only.

**Why:** the owner wants any resource cappable per user and globally from one place, and a
system-level backstop against abuse (thousands of scripted accounts each using their own quota).

**Reverse it if:** never in V2; new resources become new limit keys.

### D85. Attachments are stored in S3 in production, local disk in development

**Decided (by the owner, 2026-10-02):** `django-storages` + `boto3` (approved), storage backend
chosen by env; tests and development use local disk. Per-user and system storage are limit keys
(D84).

**Alternatives:** local disk only for V2.

**Why:** the owner wants production-grade storage from the start.

**Reverse it if:** there is no deployment by phase 6; ship on local disk and switch by env later.

### D86. `vue-router` and `vitest` are approved

**Decided (by the owner, 2026-10-02):** routes for every screen (`/notes/:id`, `/chat/:id`, …)
replace D46's view store; `vitest` tests the web client. Both land in V2 phase 0.

### D87. Reminders are delivered by email and browser push

**Decided (by the owner, 2026-10-02):** email plus web push — the browser's own "Allow
notifications" prompt, then notifications that arrive even with the tab closed. `pywebpush` is
approved. Android gets native notifications in its phase.

### D88. Memory is on by default, and the app keeps telling the user so

**Decided (by the owner, 2026-10-02):** `User.memory_enabled` defaults to on. Two more fields:
`memory_choice_explicit` (the user set it themselves) and `memory_notice_seen_at`. At intervals
(`MEMORY_NOTICE_DAYS`, and on a user's first chat) the client shows a notice:

| State | Notice |
|---|---|
| On, never chosen by the user | **Prominent** banner: "We build a lasting memory from your chats to make answers more relevant. Review or turn it off" |
| On, chosen by the user | Subtle inline note with the same link |
| Off (always chosen) | Subtle note: "Memory is off. Turn it on for more relevant answers" |

The notice is served by the app-open hook (D90), so every client gets it the same way.

**Why:** default-on gives better answers; telling users clearly and repeatedly keeps it honest,
and a user's explicit choice earns a quieter reminder.

### D89. The web client can never get stuck on stale cached JavaScript

**Decided (2026-10-02, owner's requirement):**
1. **Hashed asset names** (Vite's default `assets/*.[hash].js`) served `Cache-Control: public,
   max-age=31536000, immutable`; **`index.html` always `no-cache`** (revalidated every load). A
   new deploy is new file names, so a browser cannot mix old and new code.
2. **Build id.** Each build embeds `VITE_APP_VERSION` (the git commit). The API returns the
   deployed client build and a minimum supported build (`GET app/version/`, and an
   `X-Client-Min-Version` header). The client checks on load, on focus and every few minutes: a
   newer build shows "New version — reload" (reloading itself when no edit is unsaved); a build
   below the minimum must reload.
3. **Lazy-loaded route chunks** that 404 after a deploy (`vite:preloadError`) trigger one
   reload instead of a blank screen.
4. **The service worker** (needed for push, D87) never caches `index.html` or app code, is
   itself served `no-cache`, and activates updates immediately (`skipWaiting` + `clients.claim`).
   It handles push only.
5. The Android app (Capacitor) uses the same version check for its bundled code.

**Why:** the owner has been burned by browsers running stale JavaScript after deploys; every
cache layer (HTTP, chunks, service worker) is covered, with a server-side "minimum version" lever.

### D90. Lifecycle hooks: sign-in, and app open / resume

**Decided (2026-10-02, owner's requirement):** three server-side hooks, as Django signals so any
feature can subscribe without touching the sender:
- `user_signed_in(user, request, device, created)` — every Google sign-in; `created` is true for a
  new account, so registration needs no separate hook.
- `app_opened(user, device, platform, app_version, reason)` — sent by the client to
  `POST session/open/` when the app **launches** with a valid session, or **resumes** after being
  hidden longer than `APP_RESUME_AFTER_MINUTES` (web: page load and `visibilitychange`; Android:
  Capacitor's app-state events). Throttled per device; updates `SignedInDevice.last_seen_at`.
- Its response carries **notices** for the client: the memory notice (D88), "new version" (D89),
  and room for announcements or a forced sign-out.

**Why:** a single, reliable place to trigger notifications, validations and usage tracking on
the events the owner relies on in mobile apps, with the same behaviour on web and Android.

### D91. Limit values, per feature (owner, 2026-10-02)

Each AI feature carries its own limit key (D84). Per month:

| Key | Free | Premium (×5) | System |
|---|---|---|---|
| `chat_turns` (asks and chat turns) | 20 | 100 | 2,000 |
| `format` | 5 | 25 | 500 |
| `summary` | 2 | 10 | 200 |

Storage: one file ≤ 10 MB; `storage_bytes` 1 GB per user (both plans), 20 GB system.
`signups` 30 per day (system). Behind-the-scenes model calls (condensing a follow-up, memory
extraction) cost the user nothing but are logged and capped by system keys (`condense`,
`memory_extract`, defaults 10× `chat_turns`' system value).

**Parked for refinement:** the owner said "system ×20". It is recorded as ×20 of the *premium*
value; ×20 of free would let 21 free users exhaust the whole system. All values are rows in the
admin-editable `Limit` table, so changing them needs no deploy.

### D92. `uvicorn` and `pypdf` are approved (owner, 2026-10-02)

Unblocks streaming (phase 2) and PDF text extraction (phase 6).

### D93. "App open" means a real reopen, or coming back after 5 idle hours (owner, 2026-10-02)

An app open (`session/open/`, D90) is sent when:
- the app or browser window is **launched** with a valid session (a page load, a cold app start);
- or it is **resumed**: the first user interaction after **5 hours without any** (no clicks,
  keys, scrolls or touches). This covers a browser window left open for months: a new day of use
  counts as a new open. Tab visibility alone does not count.

The client keeps a `lastInteractionAt` in memory (and `localStorage` across reloads); the
threshold is `APP_RESUME_IDLE_HOURS` (default 5). **Parked for refinement.**

### D94. The memory notice is shown every 5 app opens (owner, 2026-10-02)

Replaces D88's time interval: the server counts `app_opened` events per user
(`User.app_open_count`, `memory_notice_seen_at_open`) and returns the memory notice on the first
open and then every `MEMORY_NOTICE_EVERY_OPENS` (default 5) opens since the user last saw it.
D88's prominent/subtle rule is unchanged. **Parked for refinement.**

### D95. Reminders: a due date with daily heads-ups for the last 7 days (owner, 2026-10-02)

A reminder has a **due date-time** (e.g. an expiry). Notifications go out **daily from
`lead_days` (default 7) days before it**, at the reminder's time of day, and on the due day
itself — 8 notifications for the default. `lead_days` 0 means a single notification at the due
time. Replaces V2_PLAN's none/daily/weekly/monthly repeats. Delivered by email + web push (D87).
**Parked for refinement:** snooze, "mark handled" to stop the series, other cadences.

### D96. Chat history is kept until the user deletes it (owner, 2026-10-02)

### D97. Android comes after V2 (owner, 2026-10-02)

V1 Phase 7 and V2 Phase 7 (mobile) move after `v2.0.0`; planned in a dedicated session.

## V2 0c — limits

### D98. The system limit is serialised by a transaction-scoped advisory lock per key

**Decided:** `limits.consume` checks the user's limit under the caller's user-row lock, then, only
when the key has a system limit, takes `pg_advisory_xact_lock(0x4C494D, hashtext(key))` before
summing everyone's events. The lock is released when the caller's transaction ends. `consume`
refuses to run outside `transaction.atomic()` (the lock would be released at once). The user check
comes first, so a user already over their own limit never queues on the global lock.

**Alternatives:** `select_for_update` on the `Limit` row (there is none while a key uses its
default); a counter row per key and period updated with `F()` (a second source of truth, D73);
`SERIALIZABLE` isolation (retries everywhere).

**Why:** the advisory lock needs no row to exist and leaves usage counted from one ledger. A test
forces two threads past the count with the lock removed and the limit is breached; with it, exactly
the limit passes. The two-key form keeps it out of any other advisory lock's space; a `hashtext`
collision between two keys only makes them wait for each other.

**Reverse it if:** one key's system checks become a measured bottleneck (every chat turn
serialises on it for the length of the caller's transaction); then keep a per-period counter row.
Callers should consume one system-limited key per transaction: two in opposite orders could
deadlock (Postgres detects it and fails one).

### D99. The admins are mailed on the first refusal per key, period and limit, deduplicated in the cache

**Decided:** when a system limit refuses a request, `consume` does `cache.add` on
`limits:system-alerted:<key>:<period start>:<limit>` (expiring at the period's end) and, if it
was new, queues `limits.tasks.mail_system_limit_reached`. A queueing failure deletes the marker so
the next refusal retries. Raising the limit gives a new marker, so filling the raised limit mails
again.

**Alternatives:** a marker row in the database (it would roll back with the refused request's
transaction, so it could never be written on refusal); mailing when the event that fills the limit
is recorded (misses amounts larger than one unit and a limit lowered below current usage); mailing
synchronously (SMTP latency while holding the system lock).

**Why:** the refusal is the moment the feature is paused, and the cache is the store that is not
rolled back with the request. "Once" is best effort: a flushed or evicted cache, or LocMemCache
across several processes (development only; `check --deploy` warns, `config/checks.py`), can mail twice, which
is harmless.

**Reverse it if:** duplicate mails become a nuisance; then record the alert from a separate
connection or a periodic check task.

### D100. Limit rows override every value of a key; `enabled` off means not enforced; deleted accounts keep counting

**Decided:** a `Limit` row replaces all of its key's defaults (not field by field), so a null in the
row means unlimited. `enabled=False` stops enforcing the key at both levels while still recording
usage. `Limit.clean` refuses a key absent from `LIMIT_DEFAULTS`; `consume` raises `KeyError` for
one. `UsageEvent.user` and `.ask` are `SET_NULL`: deleting an account or an ask keeps its usage
counted for the system. `created_at` is the instant `consume` checked against, so an event made
across a period boundary lands in the period it was counted in.

**Alternatives:** per-field override (null meaning "use the default", which then cannot express
"unlimited"); `enabled=False` as a kill switch that refuses everyone; `CASCADE` on the user.

**Why:** what the admin sees in a row is exactly what applies. A kill switch is a different
product decision, left to the owner. With `CASCADE`, deleting and recreating accounts would reset the
system count, the abuse D84 guards against.

**Reverse it if:** the owner wants a per-feature kill switch; add a separate flag rather than
reusing `enabled`. **Needs the owner:** confirm `enabled` off = "not enforced".

### D101. `assistant/quota.py` stays as a thin wrapper over the `chat_turns` limit; `ASK_QUOTAS` goes

**Decided:** `quota.py` keeps its names (`usage`, `used`, `limit_for`, `month_bounds`) but each one
reads `limits` for the key `chat_turns`. `ASK_QUOTAS` is removed from settings, so the values come
from `LIMIT_DEFAULTS["chat_turns"]` (or its admin row). Premium therefore drops from V1's 500 to
D91's 100. V1 tests that overrode `ASK_QUOTAS` now override `LIMIT_DEFAULTS` through
`assistant/tests/helpers.chat_turns(free, premium)`.

**Alternatives:** delete `quota.py` and have `me/` and the tests call `limits` directly; keep
`ASK_QUOTAS` as the source of `chat_turns`' per-user values.

**Why:** `me/`'s `ask_usage` and V1's tests keep working unchanged, and sub-task 3 changes `me/`
anyway. Two settings for one number would drift.

**Reverse it if:** nothing reads `quota` but `me/`; then inline it there and delete the module.

### D102. A failed ask is refunded in the transaction that fails it, and only by the call that fails it

**Decided:** `tasks._fail` is atomic: its conditional update (still pending or running → failed)
and `limits.refund_where(ask_id=…)` commit together, and the refund runs only if that update
changed the row. `sweep_stuck_asks` is atomic too. It locks the stuck rows (`select_for_update`),
fails them with the same status-checked update, and refunds exactly those asks. A done ask is
never refunded; a retry that has not given up refunds nothing.

**Alternatives:** refund every failed ask's unrefunded events on each sweep (self-healing, but a
join over all failed asks every five minutes); refund through a signal on `AskQuery` status.

**Why:** "failed asks don't count" (V1) must stay exact. With the refund in the same commit, an ask
is never failed but still counted, or refunded but not failed. Refunding only from the call that
made the change means a duplicate run (`acks_late`) or the task racing the sweeper hands back one
ask, not two. Tests remove each refund, and the guard, and fail.

**Reverse it if:** asks gain partial costs (tokens) that should stay counted after a failure.

### D103. The backfill copies each non-failed ask's user and time, and lives in `limits`

**Decided:** `limits/migrations/0002_backfill_ask_usage` creates one `chat_turns` event (amount 1)
per `AskQuery` that is not failed, with the ask's `user` and `created_at` and linked to the ask.
It skips asks that already have an event, so a second run adds nothing. Reversing deletes the
`chat_turns` events linked to an ask. Provider, model and tokens are not copied, because new ask
events don't carry them yet either (not in scope).

**Alternatives:** a migration in `assistant`; copying token counts.

**Why:** with the original `created_at`, this month's count is the same number before and after
the deploy, and V1's months stay in the right periods. The ledger owns its data, so the migration
lives in `limits`.

**Reverse it if:** never. Note: during a rolling deploy, asks made by old code after the migration
ran get no event and don't count. Run the migration with the new code, or rerun the backfill
afterwards (it is safe to repeat).

### D104. `create_ask` makes the row, then consumes; a system refusal passes through untouched

**Decided:** under the user lock, after the idempotency lookup, `create_ask` creates the
`AskQuery` and then calls `limits.consume(locked, "chat_turns", ask=ask)`. `UserLimitExceeded`
becomes V1's `QuotaExceeded` (same fields, so the 429 body is unchanged). `SystemLimitExceeded`
passes through as it is. Either refusal rolls back the ask with the transaction. A replay returns
before `consume`, so it uses nothing.

**Alternatives:** consume first, then attach the ask to the event (one more UPDATE on every
successful ask); a nullable link filled later.

**Why:** the event is linked from birth, which is what refunds look up. A refused ask costs one
rolled-back insert and one skipped id.

**Reverse it if:** refusals become common enough that the wasted inserts matter. Until sub-task 3
adds the 503 handler, a `SystemLimitExceeded` surfaces as a 500.

### D130. A system limit is a 503 from the shared exception handler, saying no more than "paused"

**Decided:** `config/api/exceptions.py` turns `limits.SystemLimitExceeded`, raised anywhere in a
DRF view, into `503 {detail, code: "system_limit_reached"}` and marks the transaction for rollback,
as DRF does for its own errors. The detail names neither the key nor the numbers, and the response
has no `Retry-After`. `POST ask/` documents the 503 through `SYSTEM_LIMIT_RESPONSE` in
`config/api/common.py`, which later features reuse.

**Alternatives:** each view catches it (as `QuotaExceeded` is); a 429, like the user's own limit;
a `Retry-After` set to the period's end.

**Why:** every limited feature needs the same answer, and a feature that forgets to catch it would
send a 500. 503 tells the client the service is unavailable, not that the user did something wrong.
The key and the counts are for the admins (D99's mail). A period end is often weeks away, and the
admin may raise the limit sooner, so a `Retry-After` would mislead.

**Reverse it if:** clients need to tell features apart; then add the key to the body.

### D131. Sign-ups consume `signups` inside the create; when full, a 403 `signups_closed` that is not a failed attempt

**Decided:** `accounts.google._create_user` calls `limits.consume(None, "signups")` and inserts the
user in one `transaction.atomic()`. An insert that loses the unique-constraint race rolls its
sign-up back. When the day is full, `SignupsClosed` (a `GoogleSignInError` with reason
`signups_closed`) is raised and nothing is created. There is one exception: if the account appeared
meanwhile (the same person's double tap took the last place), they are signed in as it. The view
answers `403 {detail, code: "signups_closed"}`, records `google_login_failed` with that reason, and
does not count it towards the per-address failed-sign-in limit. Existing accounts never touch the
limit.

**Alternatives:** the generic 400 `google_failed` (D13's "say nothing" rule); 503
`system_limit_reached`; counting it as a failure for `accounts/ratelimit.py`.

**Why:** the token was good, so nothing about it is revealed, and a person refused at the door
should know it is a daily cap and not a broken sign-in. 403, not 503: it is a policy refusal for
this caller (a new account), and the service is otherwise up. Counting it as a failure would lock
out an office's address for a reason that has nothing to do with forged tokens.

**Reverse it if:** the cap starts being probed to learn whether an email has an account (today
it reveals only that the caller has none, which signing in reveals anyway).

### D132. `me/` reports the user-facing keys as a fixed object, counted in two queries

**Decided:** `me/` gains `limits: {chat_turns, format, summary, storage_bytes}`, each
`{used, limit, resets_at}` with `limit` and `resets_at` nullable (unlimited; never resets). The
keys are `limits.serializers.USER_KEYS`, and system-only keys are left out. `limits.usage_many`
reads the rules in one query and sums every key in one conditional aggregate. `ask_usage` keeps
its shape, now nullable in the same two places, and is the same numbers as `limits.chat_turns`
(computed once per serialisation).

**Alternatives:** an open `{key: …}` map typed as a dict; one `usage()` call per key (8 queries
on every `me/` and sign-in).

**Why:** a typed object gives the generated client real field names. A new user-facing key is a
deliberate API change anyway. `me/` is fetched on every app open, so its cost should not grow with
the number of keys; a test pins it at two queries.

**Reverse it if:** keys become per-deployment configuration; then switch to a map.

### D133. A finished ask writes its provider, model and tokens onto its usage event

**Decided:** `tasks._finish` is atomic. When its conditional update marks the ask done and the
result came from a provider, `limits.describe_where({"ask_id": …}, provider, model,
input_tokens, output_tokens)` fills them in. `describe_where` only accepts those fields. A floor
answer (no provider call) leaves them empty and null. The backfill (D103) now copies them too, for
answered asks, replacing D103's "not copied".

**Alternatives:** read the cost from `AskQuery` when reporting (join per event); write the event
only when the ask finishes.

**Why:** the ledger is where cost reports and future token limits will look, for every feature
(chat turns, format, summaries), and not every feature has an `AskQuery`. The event still exists
from the moment of the ask, so the quota is right while it runs.

**Reverse it if:** token use becomes a limit of its own; then consume a token key instead of
annotating.


### D105. Lifecycle signals are sent with `send_robust`; `user_signed_in` is sent from `issue_tokens`

`user_signed_in` needs the `SignedInDevice`, which exists only once `issue_tokens` has registered
it, so it is sent there (`issue_tokens(user, request, created=False)`), not from `accounts/google.py`.
Both signals go through `accounts.signals.send`, which uses `send_robust` and logs a failing
receiver by exception type: a broken listener (notifications, usage tracking) must never turn a
successful sign-in or app open into an error. **Alternative:** plain `send` (fails loudly, but a
bug in an unrelated receiver would lock everyone out). `memory_notice_seen_at_open` is nullable
(null = never seen) so "first open" needs no sentinel; `User.timezone` is validated against
`zoneinfo.available_timezones()` in `PATCH me/`, not on the model.

### D106. `X-Client-Min-Version` is added to `/api/` responses by middleware

`ClientMinVersionMiddleware` sets it on every response under `/api/` when `CLIENT_MIN_VERSION` is
non-empty, and `CORS_EXPOSE_HEADERS` lists it. The early 413 from `MaxUploadSizeMiddleware` does
not carry it (it runs before everything else and a too-big request is not a version question).
**Alternative:** set it in a DRF renderer/response mixin, which would miss errors raised outside DRF.

### D107. The app-open throttle is a column, `SignedInDevice.last_app_open_at`

A counted open claims the slot with one conditional `UPDATE ... WHERE last_app_open_at IS NULL OR
<= now - APP_OPEN_MIN_INTERVAL_SECONDS` and counts only if a row changed, so two simultaneous
opens from one device count once. **Alternative:** the cache (per-process with locmem, lost on
restart, and not testable without clock tricks); `last_seen_at` was not usable because refresh
and the open itself move it. A throttled open still returns notices and still moves `last_seen_at`.

### D108. The device comes from the token's `device` claim, and only the caller's own is honoured

`session/open/` looks the claim up with `user=request.user`; another account's id (forged or
stale) resolves to no device, which is then neither updated nor throttled, and the signal carries
`device=None`. A token with no device row cannot be throttled, so each of its opens counts.
**Alternative:** a 403 on a foreign device; rejected because the open itself is harmless and the
client would have nothing to do about it.

### D109. Build ids that are not `YYYYMMDDHHMM[-sha]` never produce an update notice

`is_older` is false when either side is empty or unparseable (a dev server, a typo, an unset
limit). An `update` notice is sent when the build is older than `latest` **or** older than
`min_supported` (so a minimum set without a latest still forces an update); `required` is
"older than min". Ids with an equal timestamp compare equal whatever their sha.

### D110. The memory notice stays due until `memory-notice/seen/` is posted

Due when `memory_notice_seen_at_open` is null and the user has at least one open (D94: the
first), or `app_open_count - memory_notice_seen_at_open >= MEMORY_NOTICE_EVERY_OPENS` (minimum 1).
The server cannot know the client displayed it, so it keeps returning it until confirmed.
**Alternative:** mark it seen when served; a notice lost to a crashed page would then be skipped.

### D111. Notices are computed after the open is counted, and `session/open/` rejects unknown values

`platform` must be `web` or `android`, `reason` `launch` or `resume`, and `app_version` at most 64
characters (blank allowed); anything else is a 400 and counts nothing. The first open is
therefore open number 1 when its notices are worked out, and a throttled repeat sees the same
count as the open it repeats, so it gets the same notices.

### D112. `/signin` is a route; the guard redirects there with `next` (0d, 2026-10-02)

**Decided:** sign-in is a normal route (`/signin`, public). The guard sends signed-out users to
`/signin?next=<path>`; a signed-in user landing there is sent on to `next`. `next` goes through
`safeNext` (same-site paths only, refusing `//host` and `/\host`), kept in `lib/safeNext.ts`.
**Alternative:** render `SignIn` in `App.vue` when signed out and use the guard only to redirect.

### D113. "Back" from a note follows the app's own history (0d, 2026-10-02)

**Decided:** back is `router.back()` when `history.state.back` exists, else `/notes`; a refreshed
or deep-linked note therefore goes to the list, and a citation opened from an answer goes back to
the answer ("Answer" label). **Alternative:** always go to the list.

### D114. A deleted note's page stays in history (0d, 2026-10-02)

**Decided:** after deleting, the editor calls back; the deleted note's route is simply behind us.
**Alternative:** `router.replace` the note's entry so back never reaches it.

### D115. The notes sync resyncs from 0 when the server's revision goes backwards (0d, 2026-10-02)

**Decided:** D25 says a `latest_revision` below the client's `after` is the cue to resync from 0,
but the client took `max()` and never did. Now it drops every local note and refetches from 0.
**Alternative:** keep local notes and only reset the counter (leaves notes that no longer exist).

### D116. Client tests run in plain Node with stubbed globals, no jsdom (0d, 2026-10-02)

**Decided:** vitest (D86) in its default Node environment; `fetch` and `localStorage` are stubbed
per test, the notes API is mocked, and `safeNext` moved out of `router.ts` so it imports without a
browser. **Alternative:** jsdom for everything (slower, and unneeded for this logic).

### D117. An update never reloads over an unsaved edit, even a required one (0d, 2026-10-02)

**Decided:** the editor tells the notes store when a note has edits the server lacks
(`hasUnsaved`). A newer build, or one below the minimum, reloads at once only when nothing is
unsaved; otherwise the bar shows and the reload follows as soon as the save completes. A note
deleted elsewhere does not count (it can never be saved). After one reload that did not clear the
condition (a deploy not yet live everywhere), only the bar remains, so there is no reload loop.
**Alternative:** force the reload for "below minimum" regardless (loses the edit).

### D118. Chunk-load errors reload once a minute, and not at all without `sessionStorage` (0d, 2026-10-02)

**Decided:** route components are lazy; `vite:preloadError` reloads once, and a second error
within 60 s surfaces normally. If `sessionStorage` is blocked there is no loop guard, so no reload.
**Alternative:** reload anyway when storage is blocked (risks an endless loop).

### D119. The build id is made in `vite.config.ts` from UTC time and `git rev-parse` (0d, 2026-10-02)

**Decided:** `YYYYMMDDHHMM-<shortsha>`, with `nogit` when git is unavailable, overridable by a
`VITE_APP_VERSION` environment variable (so CI can match the server's `CLIENT_LATEST_VERSION`).
Builds compare by timestamp only; an id that does not parse (dev server) never triggers an update.
**Alternative:** a package.json version (needs a manual bump per deploy).

### D120. The client reports `launch` on every transition to signed-in, `resume` after 5 idle hours (0d, 2026-10-02)

**Decided:** `session/open/` is sent with `launch` when a session becomes valid (a page load with a
stored session, or a fresh sign-in) and with `resume` on the first click, key, scroll or touch
after 5 hours with none. `lastInteractionAt` lives in memory and `localStorage`; storage is read
on every interaction, so another tab's activity counts, and written at most every 30 s (and at the
moment of a resume). With nothing stored there is no resume (the launch covers it); with storage
blocked the tab's own memory copy still works. The server's per-device throttle absorbs repeats.
**Alternative:** a `visibilitychange` trigger (rejected by D93).

### D121. "Review / turn off" counts as seeing the memory notice (0d, 2026-10-02)

**Decided:** following the banner's link to `/settings` calls `memory-notice/seen/` as well as
Dismiss does, since the person has looked at it. A failed call still hides the banner; it returns
at a later open. **Alternative:** only Dismiss marks it seen (the banner would reappear on the next
open after someone has already reviewed their settings).

### D122. A server `update` notice is a fact for the same update decision, not a second mechanism (0d, 2026-10-02)

**Decided:** `decideUpdate` takes `serverSaysUpdate` / `serverSaysRequired` beside the version
check, so the unsaved-edit wait and the reload-loop guard apply to it too. **Alternative:** act on
the notice directly in the lifecycle store (would bypass those guards).

### D123. The settings page saves the memory toggle at once; the timezone is sent, not edited (0d, 2026-10-02)

**Decided:** the memory checkbox sends `PATCH me/` on change. The browser's timezone is sent at
launch only when the user is still on the server default `Asia/Kolkata` and the browser's zone
differs; the page shows the zone but has no picker yet. **Alternative:** a Save button; a timezone
picker (not asked for, and a long list to get right).

### D240. A `FormatJob` points at its usage event; `limits` is untouched (4-format, 2026-10-01)

**Decided:** `FormatJob.usage_event` (FK to `limits.UsageEvent`, SET_NULL) is set from what
`limits.consume(user, "format")` returns. The task refunds with `refund_where(pk=<that event>)` and
records provider/model/tokens on it with `describe_where`. Nothing in `limits/` changed, so no
migration there and no clash with the other branches that touch the ledger. The key is `format`
(the ledger has `key`, not the plan's `kind`). **Alternative:** a `format_job` FK on `UsageEvent`
plus `"format_job"` in `META_FIELDS` (symmetrical with `ask`, but a `limits` migration that every
parallel branch adding its own FK would conflict on).

### D241. There is no apply endpoint: apply is `PATCH notes/<id>/` with `version = base_version` (4-format)

**Decided:** as the brief says, the server never writes a note from a job. A stale `base_version`
is the ordinary `409 version_conflict` of V1; a test covers the whole path (POST, poll, PATCH) and
the stale case. **Alternative:** `POST format-jobs/<id>/apply/` (a second write path through the
service, with its own conflict body).

### D242. A failed job has `error_code` and a user-safe `error`; every unusable result is `format_changed_content` (4-format)

**Decided:** `FormatJob.error_code` (for programs) beside `error` (for people). Codes:
`format_changed_content` (the guardrail refused, which includes output that is not JSON or not a
document, since the brief says a result failing the validator fails with it), `format_failed`
(provider refused), `format_busy` (transient errors exhausted), `format_unexpected`,
`format_stuck` (the sweeper), `format_note_changed`, `format_note_gone`. All are refunded.
**Alternative:** `error` holding the code only (the client would need the English map).

### D243. The guardrail: words kept >= 0.9, words from the original >= 0.8, numbers identical, no new month/weekday, same ticked count (4-format)

**Decided:** `notes/format_guard.py::check_format`, pure. Words are lower-cased letter runs of
`content_to_text` with list/quote markers stripped (so a numbered list adds no "1.", "2.").
(1) Numbers (digit runs with `.,:/-`) must be the same multiset: a changed, added, dropped or
re-punctuated amount or date fails. (2) No month or weekday name the original lacks (`may` and the
ambiguous short forms excluded). (3) The number of ticked checklist items is unchanged. (4) Recall
of the original's distinct words >= `FORMAT_MIN_WORDS_KEPT` (0.9, the brief's example). (5)
Precision, the share of the result's words found in the original >= `FORMAT_MIN_WORDS_ORIGINAL`
(0.8), so a heading or a few words may be added but a new paragraph of prose may not. A word
pair that is a near spelling (difflib >= 0.8, 4+ letters) counts as a typo fix for (4) and (5).
**Alternative:** recall only, as the brief literally says (lets a model append invented prose to a
long note); stricter number handling that tolerates `4500` -> `4,500` (refused here: reformatting
is not worth a risk of changing an amount; the prompt forbids it). Both thresholds are settings
and need tuning on real notes.

### D244. The task refuses to run on a note that has moved past `base_version`; refunded (4-format)

**Decided:** if `note.version != job.base_version` when the task starts, the job fails with
`format_note_changed` without a provider call, because an apply at `base_version` would 409 anyway
and the model would be formatting text the person no longer has. A deleted note fails with
`format_note_gone`. **Alternative:** format the current content and move `base_version` forward
(works during typing, but the client's `base_version` changes under it after the 202).

### D245. An empty note or one over `FORMAT_MAX_INPUT_CHARS` (24,000) is a 400 that uses nothing (4-format)

**Decided:** `note_empty` and `note_too_long`, checked under the lock before `consume`. The result
is the whole document as JSON, so a long note would be cut off at the output ceiling and fail
after being paid for. **Alternative:** format long notes in chunks (a different feature: chunk
borders break lists and the guardrail's whole-note comparison).

### D246. `complete()` takes an optional `max_output_tokens`; formats use `FORMAT_MAX_OUTPUT_TOKENS` (16,384) (4-format)

**Decided:** one backward-compatible keyword on `assistant.chat.complete`. `CHAT_MAX_OUTPUT_TOKENS`
(2,048) suits a few cited sentences, not a rewritten document, and a cut-off answer is a `ChatError`
(D54). **Alternative:** raise the global ceiling (every ask would be allowed 8x the output).

### D247. The fake provider "formats" by turning a lone first paragraph into a heading (4-format)

**Decided:** for a user message that starts with `<note>`, the fake returns the document as JSON
with its first paragraph made a level-2 heading when the note has no heading and 2+ blocks;
otherwise the document unchanged. Deterministic, visible in a poll, and keeps every word so the
guardrail passes. Tests that are about what a model said mock `complete` with canned output.
**Alternative:** an identity echo (an end-to-end test could not tell a format happened).

### D248. The note travels as compact JSON in `<note>` tags with `<note` escaped as `<` (4-format)

**Decided:** `notes/format_prompt.py::document_json`. An opening or closing `note` tag inside the
note's text is written as the JSON escape `<`: identical to a JSON reader, inert as a
delimiter. Rules live in `notes/prompts/format.md` (`version: format-v1`), stored per job.
**Alternative:** HTML-escaping the whole JSON (changes every quote and `&` the model must undo).

### D249. Format jobs get their own throttle scope, sweeper and stuck age (4-format)

**Decided:** `POST notes/<id>/format/` uses throttle scope `format` (`API_FORMAT_THROTTLE`,
30/hour; the limit is the real cap, the throttle stops a runaway script). `sweep_stuck_format_jobs`
(beat every 5 minutes) fails jobs unfinished after `FORMAT_STUCK_AFTER_SECONDS` (1 hour, the same
retry budget as asks, D78) and refunds them. Finished jobs and their `proposed_content` are kept
(no retention purge yet). **Alternative:** share the `ask` throttle scope and `ASK_STUCK_AFTER_SECONDS`.

### D140. A conversation turn is an `AskQuery` with `conversation` and `position` (1, 2026-10-01)

**Decided:** `Conversation` (user, title, `summary`, `summary_through`, timestamps, `deleted_at`)
holds no answers; each turn is an `AskQuery` with a nullable `conversation` FK, a `position` from 1
and a blank `standalone_question` (filled by sub-task 2). Unique `(conversation, position)`, and a
check constraint that both are set or neither (a plain `POST ask/`). `AskQuerySerializer` gains
`conversation` and `position` (null for a plain ask); `standalone_question` is not exposed yet.
A turn is polled at `GET ask/<id>/`. **Alternative:** a separate Turn model (plan D78 rejects a
second job model).

### D141. Turns are sequential, checked under the user's row lock (1, 2026-10-01)

**Decided:** `create_turn` takes the same user lock as `create_ask`, then (in order) re-reads the
conversation (deleted → 404), replays the idempotency key, refuses with `TurnInProgress` (409
`turn_in_progress`, body names the unfinished `turn`) if any turn is pending/running, then creates
position `max + 1` and consumes `chat_turns`. A replay comes before the 409, so retrying the turn
that is running returns it (200). A failed turn does not block the next one. The unique
`(conversation, position)` is the second guard: without the lock, a race ends in an
IntegrityError, not a second turn (tested). **Alternative:** a per-conversation row lock (finer,
but every ask and turn already takes the user lock for the quota, so it would add a lock without
adding concurrency).

### D142. An idempotency key replays only the same question in the same place (1, 2026-10-01)

**Decided:** keys stay unique per user across asks and turns. A key replays only when both the
question and the conversation (none, for `POST ask/`) match; a turn's key sent to `POST ask/`, or
to another conversation, is 422 `idempotency_key_reused` (its text is unchanged: "already used
for a different question"). **Alternative:** replay by key alone (would hand back a turn of another conversation as if it
were this request).

### D143. `POST conversations/` takes an optional first question; then it needs a key (1, 2026-10-01)

**Decided:** without `question`, an empty conversation (201, no key, no quota). With one, turn 1
is asked in the same transaction and `Idempotency-Key` is required; a replay returns the same
conversation (200). A key that made a plain ask, a later turn, or a turn of a since-deleted
conversation is 422. A refused first turn (429/503) leaves no conversation. Response is the
conversation with its turns. **Alternative:** always require a key (an empty conversation costs
nothing, and a duplicate empty one is harmless).

### D144. The title comes from the first question; a rename does not reorder the list (1, 2026-10-01)

**Decided:** the first turn sets `title` (the question on one line, cut at a word near 80
characters, with "…") only if the title is still blank, so a rename made before it is kept.
`updated_at` is moved by each new turn (explicitly, in the same transaction), not by a rename or a
delete (both use `update()`, skipping `auto_now`). PATCH accepts only `title` (1-200, stripped,
not blank); PUT is 405. **Alternative:** `auto_now` on every save (a rename would jump to the top).

### D145. Deleting a conversation hides it and its turns everywhere; usage stays (1, 2026-10-01)

**Decided:** soft delete (`deleted_at`) under the user lock, so a turn racing the delete either
commits first and is hidden or gets a 404. A deleted conversation is 404 on every endpoint, and
its turns drop out of `GET ask/` and `GET ask/<id>/` (filter `conversation__deleted_at__isnull`,
a LEFT JOIN that keeps plain asks). Live conversations' turns stay in `GET ask/`. Usage events
are untouched, so the month's count does not change. A turn already running finishes unseen.
**Alternative:** keep a deleted conversation's turns visible in `ask/` (contradicts "hides its
turns from history"); exclude every turn from `ask/` (a breaking change for no gain).

### D146. `GET conversations/` pages by `(-updated_at, -id)` (1, 2026-10-01)

**Decided:** a `CursorPagination` on `-updated_at` with `-id` as tie-break, despite the project
pager's warning about moving fields. `updated_at` only moves up (a new turn), so paging never
shows a conversation twice; one that gets a turn mid-paging is missed on that pass and is at the
top of the next page-one fetch. **Alternative:** `-id` (stable, but not the "recent first" order
the chat list needs).

### D220. A conversation turn uses the chat prompt from turn 1, with the question as asked (2, 2026-10-01)

**Decided:** every turn of a conversation is answered with `prompts/chat.md` (`chat-v1`, stored in
`prompt_version`), turn 1 included (it simply has no history); a plain `POST ask/` keeps
`ask-v1`. The user message is `<summary>` (if any), `<history>`, this turn's `<excerpts>`, then
`<question>` holding the follow-up **as the user wrote it**: the model has the history to read it
by, and the standalone rewrite is only for retrieval. The relevance floor applies to turns
unchanged. **Alternative:** `ask-v1` for turn 1 (two prompts for one thread); the standalone
question in `<question>` (a bad rewrite would then change what is answered, not only what is
searched).

### D221. The "already stands alone" heuristic: no pointing word, no continuation, four words, ASCII (2, 2026-10-01)

**Decided:** `needs_condensing` sends a follow-up to the condenser if it has a word that points
back (`it, its, they, them, their, this, that, these, those, he, him, his, she, her, there, one,
ones, same, former, latter, else`…), opens with a continuation (`and, but, or, also, only, so,
then, too`, "what about", "how about"), has fewer than four words, or has any non-ASCII letter (the
word lists are English; anything else is always condensed). Over-inclusive on purpose: a false
positive costs one cheap call that returns the question unchanged; a false negative searches
"when is it due?" as is. Measured on the 17 multi-turn fixtures: all 12 follow-ups that need
context are condensed, the 3 topic shifts and 2 no-answer follow-ups are not (a test pins this).
**Alternative:** always condense (one extra call per turn, and a chance for a real model to drag
the old topic into a shift).

### D222. A condense call is a `condense` event with user None, linked to the turn; refunds and cost stay per key (2, 2026-10-01)

**Decided:** before the provider call, `limits.consume(None, "condense", ask=turn)` in its own
short transaction (the advisory lock is not held across the HTTP call). A provider error refunds
it (nothing was billed); a reply records provider, model and tokens on it with `describe_where`.
Because the event is linked to the turn, the task's `_finish` (D133), `_fail` (D102) and the
sweeper now act on `key="chat_turns"` only: the answer's cost no longer lands on the condense
event, and a turn that fails after condensing refunds its chat turn but not the condense call
that was made. **Alternative:** the event under the user (attributable, but the brief and D91 say
user None; the turn link gives the user anyway); no `ask` link (loses which turn it served).

### D223. Earlier answers are repeated without their `[n]` markers (2, 2026-10-01)

**Decided:** in the history (chat and condense prompts) an earlier answer has its citation markers
removed (the `citations.MARKER` pattern), with the space before punctuation tidied. Those numbers
referred to that turn's excerpts; left in, they invite the model to cite `[1]` meaning an old
excerpt, while this turn's citations number only this turn's excerpts. **Alternative:** keep them
(verbatim, but a source of wrong citations).

### D224. History is trimmed by characters, newest whole turns, no gaps (2, 2026-10-01)

**Decided:** the history is the conversation's `done` turns before this one with position after
`summary_through` (failed turns have no answer and are skipped). `fit_history` keeps whole turns
(question + answer characters) from the newest back while they fit `CHAT_HISTORY_MAX_CHARS`
(6,000, ≈1,500 tokens) and stops at the first that does not, so there is never a gap. The newest
turn is always kept, its answer cut at a word to the room left. Condensing uses the same function
with `CHAT_CONDENSE_HISTORY_MAX_CHARS` (2,000) and no summary. The summary itself is included
whole (sub-task 3 bounds what it writes). **Alternative:** a fixed number of turns (a long answer
would blow the prompt); skipping an overflowing turn to fit an older one (a history with a hole).

### D225. The fake condenser replaces the first pointing word with the previous question's content words (2, 2026-10-01)

**Decided:** the fake provider recognises a condense call by `<follow_up>` and replaces the
follow-up's first `it, its, them, they, their, this, that, these, those, one, ones` with the
previous turn's question minus stop words ("How often do I have to take it?" after "What did
Dr. Kulkarni say about my vitamin D?" → "How often do I have to take Dr Kulkarni say vitamin D?").
No such word → the follow-up unchanged. Crude, but deterministic, and it moves retrieval the way a
real condenser should, so tests can show a pronoun follow-up finding the right note and a shift
not picking up the old topic. **Alternative:** prepend the previous question's keywords always
(would drag every topic shift).

### D226. Condensing never fails a turn; the rewrite is stored whenever the condenser gave one (2, 2026-10-01)

**Decided:** `ChatError`, `TransientChatError` (not retried: the turn is not worth a minute of
backoff for a better search), `SystemLimitExceeded` on `condense`, and an empty reply all log a
warning and search the follow-up as asked; `standalone_question` stays blank. A usable reply is
cleaned (first line, a "Question:" label and quotes removed, cut to 1,000 characters) and stored,
even when it equals the follow-up, before retrieval; a turn taken up again (retry, redelivery)
reuses it instead of condensing and paying twice. A bug in a provider (any other exception) still
fails the turn, as it would the answer. **Alternative:** retry transient condense errors with the
task (spends the turn's retries on an optional step).

### D227. `chat.complete` takes an optional `max_output_tokens`; condensing gets 512 (2, 2026-10-01)

**Decided:** `complete(system, user, max_output_tokens=None)` defaults to `CHAT_MAX_OUTPUT_TOKENS`;
the condenser passes `CHAT_CONDENSE_MAX_OUTPUT_TOKENS` (512). A question needs a few dozen tokens,
but OpenAI's reasoning tokens count against the ceiling, and a reply cut off by it is a
`ChatError` (D54), which would make every condense fall back. **Alternative:** the shared 2,048
ceiling (no cap on a runaway rewrite); a second entry point (`condense()` in the chat package).

### D124. A reminder write stamps its note's revision; `changes` sends the note with all its reminders (5, 2026-10-02)

**Decided:** create, change, done and delete of a reminder take the owner lock and the next
`notes_revision`, and set it on the note with a queryset update — the note's `version` and
`updated_at` are untouched and nothing is re-indexed (its content did not change, so an open editor
must not conflict). A live note in `changes` carries `reminders`: every non-deleted reminder of the
note, any status, so the client replaces the note's set. Tombstones carry none. Deleting a note
turns its `scheduled` reminders `cancelled` in the same transaction; `done` stays `done`.
**Alternative:** a separate `revision` per reminder and a `reminders` list beside `results` (a
second stream for the client to page and merge, for no gain while reminders belong to one note).

### D125. Skipped and repeated local times in the schedule (5, 2026-10-02)

**Decided:** `occurrences()` builds each heads-up from the due time's wall-clock time with
`fold=0`: a time skipped by a spring change lands just after it (01:30 → 02:30 BST), a repeated
autumn time is its first instance. The due-day occurrence is always `due_at` itself. **Alternative:**
drop a heads-up whose local time does not exist (one fewer notification that week, silently).

### D126. Reminder date-times must carry an offset, and a new `due_at` must be in the future (5, 2026-10-02)

**Decided:** `due_at`, `from` and `to` without an offset are 400, not read in the server's zone.
`due_at` in the past is 400 on create and on change (delivery would otherwise fire a stale
notification at once). `channels` needs at least one of `email`, `push`; duplicates are dropped.
**Alternative:** accept naive times in the user's timezone (a guess about what the client meant);
allow past due dates as calendar records.

### D127. Changing a reminder never changes its status (5, 2026-10-02)

**Decided:** `PATCH reminders/<id>/` changes `due_at`, `lead_days`, `channels` only; a done reminder
moved to a new date stays done. "Done" twice is a no-op that takes no revision. **Alternative:** a
new `due_at` reopens the series (a product call — parked with D95's snooze/stop refinements).

### D128. At most 20 live reminders per note (5, 2026-10-02)

**Decided:** `REMINDERS_PER_NOTE_MAX = 20` in notes/services.py, counted under the owner lock;
deleted reminders don't count; past it, 400 `too_many_reminders`. **Alternative:** no cap (the
calendar query and the delivery sweep would be unbounded per note); a Limit row (the limits layer
is for AI usage).

### D129. The calendar returns `{reminder, note_title, occurrences}` items (5, 2026-10-02)

**Decided:** `GET reminders/?from=&to=` (half-open, ≤ 62 days) returns `{"results": [...]}`, each
item nesting the reminder rather than flattening it, unpaginated. Done reminders are listed;
deleted ones and those of deleted notes are not. Nested so `status` lives in one schema component:
a second component with a reminder `status` makes drf-spectacular's enum naming collide with the
asks' `status` and the schema check fail. The asks' enum is now `AskQueryStatusEnum` (was
`StatusEnum`; nothing referenced the name). **Alternative:** a flat item plus an
`ENUM_NAME_OVERRIDES` entry in config/settings.py (outside this sub-task's files).

### D134. A reminder occurrence is claimed by `INSERT ... ON CONFLICT DO NOTHING RETURNING id` (5, 2026-10-02)

**Decided:** the minute sweep (`notes/delivery.py`, beat entry `deliver-due-reminders`, 60 s,
`expires` 55 s) reads scheduled live reminders with `due_at` in `[now − grace, now + 31 days]`
(the `(status, due_at)` index) and the last delivered occurrence of each, works out the due
occurrence in Python (the schedule depends on each owner's timezone), and inserts its
`ReminderDelivery`. A returned id means this sweep won: it enqueues the send on commit. Each claim is
its own transaction, so a later failure in the sweep never re-opens claims whose sends are queued.
A test holds two sweeps at a barrier after they read their candidates. With the unique constraint,
one claim succeeds. With it dropped, both do. **Alternative:** catching `IntegrityError` in a
savepoint per claim (Postgres logs an error for every lost race); `SELECT ... FOR UPDATE SKIP
LOCKED` on reminders (guards one sweep, not a resend by a later one).

### D135. Which occurrence is due: the latest passed one, after the last delivered and the last change (5, 2026-10-02)

**Decided:** due now = the latest occurrence `≤ now` that is later than the last delivered one and
not before the reminder's `updated_at`. After an outage only the latest missed heads-up goes out. A
reminder created or moved after that day's time has passed does not fire a heads-up at once; the
next one is tomorrow's. **Alternative:** replay every missed occurrence (a burst of stale mail), or
send the passed heads-up on creation (a notification for something the user just did).

### D136. An occurrence missed by more than 24 hours is not sent (5, 2026-10-02)

**Decided:** `REMINDER_MISSED_GRACE_HOURS` (env, default 24). It also bounds the sweep: a reminder
due more than that long ago is no longer read. **Alternative:** no limit (after a long outage, a
"due now" mail days late; and every past reminder rescanned each minute for ever).

### D137. At most once also at send time; a failed send is recorded, not retried (5, 2026-10-02)

**Decided:** the send task first sets `sent_at` with `UPDATE ... WHERE sent_at IS NULL`. If no row
changes, another run already has it (Celery's `acks_late` can hand a task out twice). Then it
re-checks the reminder (scheduled, not deleted, note live: `{"skipped": "inactive"}` if not), sends
each channel and records `channel_results` (`sent`, `failed`, `no_address`). No retries. A worker
dying mid-send, or an SMTP error, loses that one notification; the next day's still comes.
**Alternative:** retry transient mail errors (risks duplicates, which the plan rules out); set
`sent_at` after sending (a redelivered task would send again).

### D138. The reminder email: title, due date, relative day and a link, plain text, never the note body (5, 2026-10-02)

**Decided:** subject `Reminder: <title> (due in N days | due tomorrow | due now)`. The title is put
on one line (a newline in a subject is header injection), cut to 100 characters, and "Untitled
note" when empty. The body has the title, `Due Tue 27 Oct 2026, 09:00 GMT.` in the owner's
timezone, `<WEB_APP_URL>/notes/<id>` and one line on how to stop it. It is sent from
`DEFAULT_FROM_EMAIL` (defaults to `SERVER_EMAIL`). **Alternative:** HTML mail (templates to
maintain, for three lines); including an excerpt (note text leaving the app by email).

### D139. New settings `WEB_APP_URL` and `DEFAULT_FROM_EMAIL`; push is recorded `unavailable` until it exists (5, 2026-10-02)

**Decided:** `WEB_APP_URL` (default `http://localhost:5173`) is where mail links point; the deploy
sets it with the hosting discussion. Until sub-task 3 adds push, a reminder with the `push`
channel records `"push": "unavailable"` and sends its email as usual. **Alternative:** build links
from the API host (the web client is served elsewhere in dev).
*Note: D172 supersedes the push line above; push now sends when VAPID keys are set.*

### D170. Web push: pywebpush, VAPID keys from env, on only when both keys are set (5, 2026-10-02)

**Decided:** `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` (URL-safe base64 raw keys, as
`manage.py generate_vapid_keys` prints them) and `VAPID_SUBJECT` (default `mailto:<SERVER_EMAIL>`).
Push is "on" when both keys are non-empty (`accounts/push.py: push_enabled`). Off: `GET
push/vapid-key/` and `POST me/push-subscriptions/` are 404 and a reminder's `push` channel records
`"push": "unavailable"`; `DELETE` still works so a client can clean up. **Alternative:** a separate
`PUSH_ENABLED` flag (a second switch that can disagree with the keys).

### D171. `PushSubscription`: endpoint unique across users, a re-register moves it (5, 2026-10-02)

**Decided:** `accounts.PushSubscription` (`user`, unique `endpoint`, `p256dh`, `auth`, `user_agent`
truncated to 200, `created_at`, `last_success_at`). `POST me/push-subscriptions/` is an
`update_or_create` by endpoint, so a different account signing in on the same browser profile takes
the row over; the old account stops getting pushes on that browser. `DELETE` (body `{endpoint}`)
removes only the caller's row and is 204 either way, so it does not reveal whether an endpoint
belongs to someone else. Keys are never returned. **Alternative:** a 409 for another user's
endpoint (the new sign-in could never subscribe, and the old owner is no longer using it).

### D172. The push payload is ids and the title; outcomes are strings in `channel_results` (5, 2026-10-02)

**Decided:** payload `{type: "reminder", reminder_id, note_id, title}` (title cleaned and cut as in
the email, D138), never note content: push services see the payload only encrypted, but the
service worker shows it on a lock screen. Sent to every subscription of the owner with a 12-hour
TTL. A 404 or 410 deletes the subscription (not a failure). Other errors are logged and leave the
subscription. `channel_results["push"]` is `sent`, `partial`, `failed`, `no_subscriptions` or
`unavailable`. No retries (D137). **Alternative:** deleting a subscription after repeated 5xx
(a push-service outage would drop everyone's subscriptions).

### D173. A push endpoint must be a known push service: https, allowlisted host, no userinfo or odd port (5, 2026-10-02)

**Decided:** the endpoint is a client-supplied URL the server later POSTs to, so
`POST me/push-subscriptions/` refuses (400, code `invalid_endpoint`) anything but: `https`, a host in
`PUSH_ENDPOINT_HOSTS` (exact, or `*.suffix` for subdomains only; defaults FCM, Mozilla, Windows
(WNS) and Apple), no userinfo, no port but 443, no whitespace or backslash, at most 1000
characters. An IP literal never matches. `accounts.push.send_to_user` re-checks each row before
sending and deletes one that fails (rows older than the check, or a shrunk allowlist). `p256dh`
must be base64url decoding to 65 bytes and `auth` 16 to 32. **Alternative:** resolving the host and
refusing private addresses (racy against DNS rebinding, and still lets a user aim us at any public
host); the allowlist needs a setting change for a new browser's service.

### D200. The web client reads and writes reminder times in the account's timezone, and mirrors the schedule rule for display only (5, 2026-10-02)

**Decided:** the due-time input is a `datetime-local` read as wall-clock time in `User.timezone` (not the
browser's) and sent as a UTC instant (`...Z`). `web/src/lib/schedule.ts` re-implements
`notes/schedule.py` (daily at the due time's local time of day, repeated time = first, skipped time = the
pre-change offset) so a note can say "8 notifications, next Fri 2 Oct, 09:00" without a round trip; the
server stays the only source for what is actually sent, and the calendar shows the server's occurrences.
Editing sends only the changed fields, so an untouched past due time is not refused. Lead days are a
number input 0-30 (the API's limit, default 7). **Alternative:** the browser's timezone for the input
(a reminder set while travelling would not match what the server sends); no client schedule (needs an
endpoint just for the summary).

### D201. Calendar: Monday-first, view and day in the URL, ranges cut at the account's midnights (5, 2026-10-02)

**Decided:** `/calendar?view=month|week&d=YYYY-MM-DD`; going into a note and back lands on the same page.
A month view is the whole Monday-to-Sunday weeks around the month (28-42 days), a week is 7; the API range is
the start of the first day to the start of the day after the last in the account's timezone (always well
under 62 days) and each occurrence is filed under its day in that zone. It reloads when a sync advances the
revision. A month cell shows 3 entries then "+N more" and the day number opens that week; filled entries
are due dates, light ones heads-ups, done reminders are struck through. **Alternative:** Sunday-first or a
locale-driven week start (a setting to add later if asked).

### D202. The service worker is for push only: no fetch handler, never cached, clicks go through the open tab (5, 2026-10-02)

**Decided:** `web/public/sw.js` (emitted unhashed at `/sw.js`; served `no-cache`, registered with
`updateViaCache: 'none'`) has install (`skipWaiting`), activate (`clients.claim`), `push` and
`notificationclick` handlers and no `fetch` handler, so it cannot serve stale code (D89). A push shows the
payload title (text only) with a per-reminder tag (today's replaces yesterday's). A click posts
`{type: 'open-path', path}` to an open tab (the app routes there, no reload, so a half-typed note is not
lost) or opens a new window; both ends accept only `/notes/<id>` or `/calendar`. **Alternative:**
`client.navigate(url)` (a full page load over unsaved edits).

### D203. Reminders ride on their note in the notes store, from `notes/changes/` (5, 2026-10-02)

**Decided:** `Note.reminders` is filled only by `changes` (which sends a note again whenever one of its
reminders changes); an `upsert` of a copy without them (a save, the list, one note) keeps the ones held.
After its own write the client applies the API's answer to the note at once and the next sync confirms it.
A note opened before the first sync shows "Loading reminders"; once synced, no reminders means none.
**Alternative:** a separate reminders store with its own cursor (a second sync loop for data that already
arrives with the note).

### D204. Push on this device: hidden when the server has it off; sign-out removes the subscription (5, 2026-10-02)

**Decided:** `GET push/vapid-key/` 404 hides every push control (settings section, reminder channel,
subscribe hint); a browser without service workers/Push/Notifications gets one line saying email still
works. "Turn on notifications" asks the browser's permission, subscribes with the server's key and POSTs
it; "Turn off" and signing out unsubscribe and DELETE it (sign-out waits at most 3 s, best effort), so a
shared browser stops receiving the previous account's reminders. **Alternative:** leave the subscription
on sign-out (the next account signing in on the browser takes it over, D171, but until then the old
account's reminders pop up on someone else's screen).

### D260. The web client locks the note while a format is made or looked at, and Format waits for pending saves (4-format)

**Decided:** `FormatPanel` reports a phase to `NoteEditor`; from "saving" to "applying" the editor and
title are read-only (`setEditable(false, false)`: the second argument matters, the default emits an
update that autosaves a no-op and moves the version) and the preview replaces the editor on screen.
Clicking Format with unsaved changes saves them first (`ensureSaved`) and only then starts the job,
so `base_version` is the version the person sees; if the save fails or conflicts, nothing is
formatted and the message says why. **Alternative:** leave the editor live and let Apply 409 whenever
the person kept typing (a normal outcome of a feature that takes seconds).

### D261. A 409 on Apply loads the server's copy and offers "Format again"; nothing is merged (4-format)

**Decided:** everything the person typed was saved before the job started, so the 409's `current`
is simply a newer note: the editor loads it, a notice says the note changed elsewhere and nothing
was overwritten, and "Format again" starts a new job on it (a new use; the stale preview is dropped).
**Alternative:** retry the PATCH on the new version (overwrites the other device's edit with a
proposal made from older text).

### D262. Job, poll, apply and error mapping are one pure module with injected API and clock (4-format)

**Decided:** `web/src/lib/formatJob.ts` (`runFormat`, `pollFormatJob`, `applyFormat`,
`describeStartError`, `describeJobFailure`, `whyDisabled`), tested without Vue or timers. Polling
backs off 0.8 s to 5 s and gives up after 3 minutes (the server sweeps and refunds a stuck job); a
POST with no answer or a 5xx retries with the same `Idempotency-Key`. A failed job shows its own
`error` text (our wording only if it sent none); `format_note_gone` is the one code with no "Try
again". **Alternative:** a Pinia store like asks (the state is per open note, so a component-local
state with a pure core is simpler and cannot leak across accounts).

### D263. The usage line and out-of-formats state come from `me/` `limits.format` (4-format)

**Decided:** shown beside the button ("3 / 5 formats used · resets <date>"); at the limit the
button is disabled with the reason, and a 429 `quota_exceeded` updates the stored usage from its
body. `me/` is re-read after each job ends (a failed job is not counted). Dates use the existing
`formatDate` (browser time zone, so a reset at midnight IST reads as the day before in UTC).
**Alternative:** only react to the 429.

### D300. A new conversation is created by its first question; `/chat/new` is a screen, not a row (1c, 2026-10-01)

**Decided:** "New conversation" and `/ask` go to `/chat/new`, which shows an empty thread. Nothing is
created until the first question: it is sent as `POST conversations/` with the question and an
`Idempotency-Key` (the API's own one-call form), then the URL is replaced by `/chat/<id>` (so Back
skips `/chat/new`) and the thread, already in hand, is not reloaded. **Alternative:** create an empty
conversation on click and navigate to it (each abandoned click would leave a blank-titled row in the
list, and a delete to clean up).

### D301. Thread logic lives in `lib/thread.ts`; the store only does the network (1c, 2026-10-01)

**Decided:** merging a polled turn (a snapshot never moves a turn backwards: a slow poll that
overtakes the finished answer cannot bring "pending" back), the thread's phase (empty, sending,
waiting, answering, failed, ready) and whether the composer may send, the list order, and the
wording of 429/503/network/409 failures (using `limits.chat_turns` from `me/`, the 429 body's own
numbers winning) are pure functions with vitest tests; `stores/chat.ts` has a smaller test with the
API mocked. **Alternative:** keep it all in the store (as the ask store did; untestable without
mocking timers and the network).

### D302. A 409 `turn_in_progress` is waited out: poll the named turn, then send again with the same key (1c, 2026-10-01)

**Decided:** on 409 the store shows the busy turn, polls it to the end (the "Waiting for the previous
answer to finish…" state, composer disabled), and re-sends the same question with the same
`Idempotency-Key`, up to 3 times (another tab may keep asking), then shows the error. The composer
already blocks a second send while a turn of this tab is pending, so a 409 only arises from another
tab or device. **Alternative:** show the 409 and make the person retry (the answer to "wait" is the
only thing they would do).

### D303. Retry of a failed turn asks its question again as a new turn (1c, 2026-10-01)

**Decided:** the last turn, when failed, shows its error and a Retry button that sends the same
question with a fresh key; the failed turn stays in the thread (it is history, and the backend skips
failed turns in the prompt, D224). Earlier failed turns show the error without a button.
**Alternative:** re-run the failed turn in place (the API has no such call; a failed turn is final).

### D304. `/ask` is gone as a screen: AskPanel and the ask store are removed; the answer body is a component (1c, 2026-10-01)

**Decided:** with `/ask` redirecting, nothing used `AskPanel.vue` and `stores/ask.ts` (a conversation
turn is also listed by `GET ask/`, so a "history" list there would duplicate the chat list). The
text-and-chips rendering (still `splitAnswer`, never `v-html`, D50) moved unchanged into
`AnswerBody.vue`, shared by every turn; its CSS stays. `auth.setAskUsage` now also updates
`limits.chat_turns` (the same numbers). **Alternative:** keep the unused panel until a later cleanup.


### D280. Folding consumes its own system-only limit key, `summarize_history`, not `condense` (1, 2026-10-01)

**Decided:** a fold call is `limits.consume(None, "summarize_history", ask=<last folded turn>)`,
5,000 a month by default (`LIMIT_DEFAULTS`, overridable by a `Limit` row), user `None` like
`condense` (D91, D222): the user pays nothing for it, only the system caps it. Provider, model and
tokens are recorded on the event. A fold is rarer than a turn (one per few turns), so the default
is a fraction of `condense`'s 20,000 (one per follow-up) and a little over twice
`chat_turns`' system 2,000. **Alternative:** reuse `condense` (one key fewer, but a runaway summariser, or a
provider outage that makes every fold retry, would then use up the budget every follow-up's
condensing depends on, and the ledger could not tell the two costs apart).

### D281. Folding starts when the unsummarised history exceeds the budget, and keeps half of it (1, 2026-10-01)

**Decided:** after a turn is answered, `should_fold` is true when the done turns after
`summary_through` -- exactly what the next prompt would repeat (`history_for`, same sizes) -- total
more than `CHAT_HISTORY_MAX_CHARS`: folding starts when `fit_history` would start dropping. A fold
keeps the newest turns that fit half the budget (`FOLD_KEEP_SHARE`; the newest turn always stays)
and folds the older ones, so the next fold is a few turns away. `summary_through` becomes the last
folded turn's position. **Alternative:** fold down to exactly the budget (every later turn would
overflow it again, one summariser call per turn); fold a fixed number of turns (a long answer would
still blow the prompt).

### D282. Folds write with a conditional UPDATE on `summary_through`, not a held lock (1, 2026-10-01)

**Decided:** `fold` reads `summary` and `summary_through`, calls the provider with no transaction
and no row lock, then writes `UPDATE conversation SET summary=..., summary_through=<last folded>
WHERE pk=? AND summary_through=<what it read>`. Two folds racing on one conversation: one UPDATE
matches, the other matches no row and its result is dropped -- no turn is folded twice, and none
is skipped, since `summary_through` only ever moves to the last turn this fold read. The loser's
call was made and paid for, so its event stays counted. Proven three ways: the interleaving
forced in one thread (a fold run to the end inside another's provider call), the same with the
winner folding further than the loser read, and two real threads held by a barrier until both
have read; each fails with the `summary_through` condition removed. **Alternative:**
`SELECT ... FOR UPDATE` on the conversation across the provider call (up to a minute: a new
turn's `updated_at` UPDATE of the same row, in `create_ask`, would wait behind it, so the user's
POST would hang); a Postgres advisory lock (same problem, or an idle-in-transaction connection);
the duplicate provider call it costs the rare loser is the price of not holding either.

### D283. One fold call is bounded; a longer backlog is folded in batches that queue themselves (1, 2026-10-01)

**Decided:** a fold sends the previous summary and at most 12,000 characters of turns, oldest
first, one turn at least, each answer cut to 2,000 (`FOLD_INPUT_MAX_CHARS`,
`FOLD_ANSWER_MAX_CHARS`). When the history is still over budget afterwards (a backlog left by
failed folds or a lowered budget), `fold_history` queues itself again; each run advances
`summary_through`, so it ends. **Alternative:** send everything unsummarised (an unbounded prompt
after a long outage of the provider).

### D284. A fold failure changes nothing; the next finished turn tries again; enqueueing never fails a turn (1, 2026-10-01)

**Decided:** the limit reached, a `ChatError` or `TransientChatError` (refunded: nothing was
billed), and an empty reply (stays counted: the call was made, like D226's) all leave `summary` and
`summary_through` as they were. `fold_history` is not retried by Celery: the history is still over
budget, so the next answered turn queues another fold, which is the retry, with no backoff to
tune; meanwhile `fit_history` keeps the prompt within budget by dropping the oldest, as before
folding existed. The fold is queued with `transaction.on_commit` from `_finish` (so it reads the
committed answer) and the `.delay` is wrapped: a broker that is down is logged and costs the fold
only, never the answer. A failed turn, a plain ask and a turn under budget queue nothing; a
deleted conversation is not folded. **Alternative:** Celery autoretry (a fold that cannot succeed
retries a minute for nothing, spending the limit each time); queueing the check in a task of its
own for every turn (a message per turn to learn "no").

### D285. The summary is bounded twice: 200 words in the prompt, `CHAT_SUMMARY_MAX_CHARS` in code (1, 2026-10-01)

**Decided:** `summarize-v1` asks for at most 200 words and to drop the oldest, least relevant
detail first; `clean_summary` removes a "Summary:" label and quotes and cuts whatever comes back to
1,500 characters (about 375 tokens) at a word, with an ellipsis, so the cap holds even when the
model ignores the rule. The summary rides in every later prompt of the conversation: its size is
that prompt's fixed cost. The cut keeps the start, not the end (a model told to merge new turns
into the old summary tends to put the newest last, so an over-long reply loses the newest: that
is the model breaking a rule, and the next fold rewrites the summary anyway). **Alternative:**
trust the prompt (an uncapped summary grows every fold); cut the head (starts mid-sentence).

### D286. The fake summariser writes one line per folded turn and keeps the newest six (1, 2026-10-01)

**Decided:** when the user message holds `<fold>`, the fake provider returns the old summary's
lines plus `- <question> -> <first sentence of the answer>` for each folded turn, keeping the
newest 6 (`FOLD_LINES`). Deterministic, so a test reads off exactly which turns were folded and in
which order; bounded like a real one is told to be, forgetting the oldest. The `fold` tag is
neutralised in history, excerpts and summary like the other delimiters (D56), so a note or an
answer cannot make a chat call look like a fold. **Alternative:** a fixed string (cannot show
which turns were folded); an unbounded concatenation (could not show the bound).

### D287. `eval_retrieval --conversations`: hybrid, questions-only history, a pure `run_condenser` (1, 2026-10-01)

**Decided:** the last turn of each fixture conversation is searched raw, condensed (the "stands
alone" heuristic first, then `run_condenser`, falling back to raw on failure or an empty reply, as
`prepare` does) and as the human `standalone`; recall@k and MRR in hybrid mode only (the mode asks
use), `--by-kind` per kind, and a count of how each last turn was handled. `condense` is split:
`run_condenser(question, history) -> (rewrite, result)` does the call with no bookkeeping, and
`condense(ask, history)` wraps it with the `condense` event (consume, refund on failure, cost).
The eval needs no AskQuery and consumes no limit. The earlier turns are given as the fixture's
questions with empty answers: producing real answers would mean running the whole ask on each,
and the fixtures carry none. This under-serves a follow-up that points at something only an
answer said ("the second one"), which the fixtures were not checked for. **Alternative:** answer the earlier turns
with the real pipeline first (costs a chat call and a search per turn, and makes the numbers depend
on the answer model too); all three modes per variant (nine rows, and only hybrid is what a turn
uses).

### D320. Attachments have their own storage alias, resolved on every call (6, 2026-10-01)

**Decided:** `STORAGES["attachments"]` is django-storages' `S3Storage` when `AWS_STORAGE_BUCKET_NAME` is
set (no ACL on objects, so the bucket's private policy rules; `querystring_auth` on; no overwrite;
credentials from boto3's own chain, never a setting), else `FileSystemStorage` under `MEDIA_ROOT`
(files `0600`, folders `0700`; no URL route serves it). `Attachment.file` points at a small proxy
(`notes/attachments.py`) that looks the alias up on each call, so the test runner swaps in a temp
dir whatever `.env` says, and the proxy's `url()` refuses: bytes leave only through the download
endpoint. **Alternative:** the `default` storage alias (static and future uses would share the
bucket), or a storage instance built at import (tests could then write to a configured bucket).

### D321. File type from magic bytes at offset 0; anything else is 415 (6, 2026-10-01)

**Decided:** JPEG `FF D8 FF`, PNG's 8-byte signature, WebP `RIFF....WEBP`, PDF `%PDF-`, all at the
first byte; the client's Content-Type and extension are ignored. Anything else, a renamed `.exe`
included, is `415 unsupported_file_type` (a non-multipart body stays DRF's `415
unsupported_media_type`). **Alternative:** `400` (the request is well formed; it is the file's type
that is refused, which is what 415 says), or accepting `%PDF-` anywhere in the first 1 KB as
readers do (lets a polyglot that is something else first through).

### D322. Stored names are random; the shown name is cleaned and always carries the type's extension (6, 2026-10-01)

**Decided:** stored as `attachments/<32 hex>.<ext of sniffed type>`. `original_name` keeps the last
path component, NFC, without control/format characters (a right-to-left override cannot disguise
an extension), trimmed of spaces and leading dots, at most 255 characters (the stem is cut, not the
extension), `attachment.<ext>` if nothing is left; and it always ends in an extension of the
sniffed type (`setup.exe` that is a PDF is shown and downloaded as `setup.exe.pdf`).
`Content-Disposition` is built by Django (`filename*=` for non-ASCII, quotes escaped).
**Alternative:** keep the name exactly as sent and fix it only in the download header (the list
would show `setup.exe` for a PDF).

### D323. Bytes are stored before the owner lock; dedup and quota are decided under it (6, 2026-10-01)

**Decided:** the view checks size and type, hashes the bytes; the service looks for the same hash
on the note without a lock (a re-upload returns at once, nothing stored), writes the bytes to
storage, then in one transaction locks the owner, re-reads the note, looks for the hash again,
consumes `storage_bytes` (user and system, D84), saves the row and stamps the note. A refusal or a
duplicate found under the lock deletes the bytes it stored. A duplicate costs nothing, even with
the quota full (`200`, the existing row). **Alternative:** write the file under the lock (a 10 MB
S3 write would hold up every other save of that user).

### D324. Deleting releases storage by refunding the upload's own event (6, 2026-10-01)

**Decided:** `Attachment.usage_event` is the `storage_bytes` event its upload consumed; a delete
(of the attachment or its note) marks it refunded, so the running total drops by exactly that
size. **Alternative:** a negative release event (`amount` is positive by design and `consume`
refuses less than 1; two rows per file for the same result).

### D325. Files are deleted on commit, inline, best effort (6, 2026-10-01)

**Decided:** `transaction.on_commit` deletes the files of soft-deleted attachments; a storage error
is logged, never raised (the delete already happened). A file left behind is unreachable (no live
row names it). Rows stay as soft-deleted tombstones. Deleting an *account* (CASCADE) does not yet
delete its files. **Alternative:** a Celery task per delete (needs a broker for a millisecond job);
a periodic orphan sweep (worth adding with account deletion).

### D326. An attachment write stamps its note's revision; `changes` sends the note with its attachments (6, 2026-10-01)

**Decided:** as reminders (D124): upload and delete take the owner lock and the next revision, set
on the note by a queryset update (version and `updated_at` untouched, nothing re-indexed). A live
note in `changes` carries `attachments`: every live one, metadata only (no bytes, no
`extracted_text`). A duplicate upload takes no revision. Extraction (sub-task 2) starts from one
seam, `services._after_attachment_added`, and must stamp the revision when it changes `status`.
**Alternative:** a separate attachments stream in sync.

### D327. The upload body allowance is keyed by view name, POST only (6, 2026-10-01)

**Decided:** `UPLOAD_SIZE_ALLOWANCES = {"api:v1:note-attachments": ATTACHMENT_MAX_BYTES + 64 KiB}`;
`MaxUploadSizeMiddleware` resolves the path only when a body is over the default limit, and only
for POST. A file over `ATTACHMENT_MAX_BYTES` but inside the envelope reaches the view and gets the
same `413 too_large`. **Alternative:** a path regex in the middleware (duplicates the URLconf).

### D328. Uploads have their own throttle scope, `upload`, 120 an hour (6, 2026-10-01)

**Decided:** POST only (listing is free), `API_UPLOAD_THROTTLE`. **Alternative:** only the general
user rate (3,000/hour), which lets a script churn uploads and deletes inside the storage quota.

### D329. Downloads are attachments with nosniff, a sandbox CSP and no-store (6, 2026-10-01)

**Decided:** `GET attachments/<id>/file/` streams with `FileResponse`, the sniffed type,
`Content-Disposition: attachment`, `X-Content-Type-Options: nosniff` (set explicitly, not only
when `DEBUG` is off), `Content-Security-Policy: default-src 'none'; sandbox` and
`Cache-Control: private, no-store`. Content negotiation is forced, so any `Accept` gets the file
and an error still renders as JSON. A live row without its file is a logged 404.
**Alternative:** presigned S3 redirects (no owner check at fetch time, links outlive sign-out).

### D330. A note's attachment list is cursor-paged; no per-note count cap yet (6, 2026-10-01)

**Decided:** `GET notes/<id>/attachments/` pages like every list (newest first). The only cap is
`storage_bytes`; whether a note needs a count cap (reminders have 20) is left to the owner.
**Alternative:** an unpaged list with a cap.

### D340. Extraction is its own task with tighter limits; a hostile PDF fails, never crashes (6, 2026-10-01)

**Decided:** `notes.tasks.extract_attachment`, enqueued on commit by `_after_attachment_added`, soft
limit `ATTACHMENT_EXTRACT_SOFT_TIME_LIMIT` (120 s) and hard 180 s instead of the default 5/10
minutes. pypdf runs inside `apply_configuration` with every decompression ceiling lowered to
`ATTACHMENT_PDF_MAX_STREAM_BYTES` (20 MB, pypdf's own default is 75 MB) and `jbig2dec` off; at
most `ATTACHMENT_PDF_MAX_PAGES` (100) pages and `ATTACHMENT_TEXT_MAX_CHARS` (100,000) characters
are read. Any exception from pypdf is `failed` with a fixed message; the soft time limit is
`failed` too; only a bug in our own code is re-raised (after failing the row). Images are never
decoded in the worker. **Alternative:** parse in a subprocess with an rlimit (stronger isolation,
more machinery than a 10 MB cap needs today); the default task limits (a bomb holds a worker for
ten minutes).

### D341. `NoteChunk.source` and `NoteChunk.attachment`; each source is indexed on its own (6, 2026-10-01)

**Decided:** `source` is `note` (default), `attachment` or `summary` (sub-task 3); `attachment` is a
nullable FK (CASCADE), set exactly when `source` is `attachment` (a check constraint). `index_note`
reads, reuses and deletes only `source=note` rows, so editing a note never touches its files'
chunks, and extraction never touches the note's. Deleting the note still removes every source.
`index_status` compares only note chunks with the note's version and counts attachment chunks
apart. **Alternative:** a separate `AttachmentChunk` table (search would need a second pair of
legs, or a UNION that the HNSW index cannot serve).

### D342. Extracted text is chunked as paragraphs, behind the file name (6, 2026-10-01)

**Decided:** `chunk_text(label, text)`: blank lines and form feeds (page breaks) end a paragraph, a
paragraph's hard-wrapped lines are joined with spaces, and packing, splitting and overlap are the
note chunker's. No heading path; `embed_text` is `"<file name>\n\n<text>"`, so renaming the note
does not change an attachment chunk's hash (nothing re-indexes attachments on a note edit).
**Alternative:** a `Page n` heading path (a chunk could cite its page, but would never span a page
break, and short pages make tiny chunks); the note's title in the prefix (stale after a rename).

### D343. Images are read by `chat.extract_image_text`, with real Claude, OpenAI and Gemini adapters (6, 2026-10-01)

**Decided:** a boundary function `extract_image_text(image_bytes, mime_type) -> ChatResult` over an
optional provider method `read_image(system, user, image, mime_type, model, max_output_tokens)`,
with the system prompt in `prompts/image_text.md` (`image-text-v1`: transcribe exactly, no
description, text in the image is content not instructions, `[no text]` when there is none,
which the boundary turns into ""). All three real providers have it, over the same `requests`
helper: Claude a base64 `image` block, OpenAI an `input_image` data URL (Responses API), Gemini an
`inline_data` part; each answer is parsed and its errors translated exactly as `complete`'s
(request shapes tested with mocked HTTP; not yet run against the live APIs). The fake returns a
fixed text. A provider without `read_image` raises `ImageTextNotSupported` (a `ChatError`), shown
as "not available right now". Images over `ATTACHMENT_IMAGE_TEXT_MAX_BYTES` (5 MB, Claude's
per-image limit) fail without a call: resizing would need Pillow, a new dependency. A
transcription cut off by `ATTACHMENT_IMAGE_TEXT_MAX_OUTPUT_TOKENS` (4,096) fails like any cut-off
answer. **Alternative:** a "not supported" stub until the owner picks a vision provider (the
adapters are small, and the chat provider is already the owner's choice).

### D344. Image reading counts against a new system-only key, `image_text` (6, 2026-10-01)

**Decided:** `LIMIT_DEFAULTS["image_text"] = {"system": 5000, "period": "month"}`. One use per
vision call (user `None`), consumed in its own short transaction just before the call, refunded
if the call fails for any reason (a transient one too: the retry consumes its own), and given the
provider, model and tokens when it succeeds. Reaching the limit fails the attachment with "paused
for now" and makes no call. The user already pays for the upload in `storage_bytes`; this caps what
images cost the service, as `condense` and `memory_extract` do. PDFs cost nothing (no model call).
**Alternative:** a per-user key (a user can only add as many images as their storage allows
anyway); counting under `summary` (that is the user's own summarize budget).

### D345. A broker error at upload is logged, and a sweeper fails what never finished (6, 2026-10-01)

**Decided:** the on-commit enqueue catches and logs a broker error instead of raising: the upload
has committed, and a 500 would make the client retry an upload that worked.
`sweep_stuck_attachments` (beat, every 5 minutes) fails live attachments still `pending` or
`extracting` `ATTACHMENT_STUCK_AFTER_SECONDS` (1 hour) after upload, through the same conditional
service call, with "This file took too long to read." The hour outlasts the task's worst case
(6 attempts of 180 s plus 5 backoffs of at most 300 s, a test checks it). A failed attachment stays
attached and downloadable; there is no "retry extraction" endpoint yet (re-uploading the same
bytes returns the failed row, so the user deletes and uploads again). **Alternative:** raise
(the request 500s after a successful upload); no sweeper (a lost message leaves `pending`
forever).

### D346. Status changes take the owner lock and a revision; chunks are written under it, after a live check (6, 2026-10-01)

**Decided:** `services.start_extraction` (pending -> extracting), `finish_extraction` (extracting ->
ready, writing the chunks in the same transaction) and `fail_extraction` (pending or extracting
-> failed, removing any chunk) each lock the owner, re-read the attachment (`FOR UPDATE OF` the
attachment, live, note live, in the expected status) and stamp the note with the next revision
only when the status really changes, so `changes` carries every status (D326) and a duplicate run
or the sweeper changes it once. Deleting an attachment (or its note) deletes its chunks in the
delete's own transaction, under the same owner lock, so a finish racing a delete either writes
first and is deleted with it, or finds the attachment deleted and writes nothing; search can
therefore keep reading chunks without a join on `attachment.deleted_at`. The note's own chunks are
removed by its tombstone's index task as before. **Alternative:** filter search on the
attachment's `deleted_at` (a join in the vector leg, which pgvector's index scan does not need
today); no revision for `extracting` (one fewer, but a client would show "pending" while it runs).

### D347. No text is not an error; a PDF that needs a password is (6, 2026-10-01)

**Decided:** a scan without a text layer, or a photo with no words, ends `ready` with empty
`extracted_text` and no chunks. A PDF encrypted only to restrict printing or copying (it opens with
an empty password) is read; one that needs a password fails "password-protected". **Alternative:**
`failed` with "no text found" (but the file is fine, and the status would read as a fault); OCR of
scanned PDFs through the vision call (a cost per page; for later).

### D348. Hits and citations name the file; the excerpt says `file="…"`; the per-note cap is shared (6, 2026-10-01)

**Decided:** `SearchHit`, `GET search/` and every citation (`AskQuery.citations`, asks and turns)
carry `attachment_id` and `attachment_name` (null for the note's own text), and search hits
`source`; `title` stays the note's. The excerpt of an attachment chunk gets a `file` attribute
(escaped like `title`) next to the note's title; the system prompts are unchanged (`ask-v1`,
`chat-v1`). Citations stored before this read with both fields null. The per-note cap (2, D71)
counts a note's file chunks with its own, so a long PDF cannot crowd the other notes out.
**Alternative:** `title` = the file name (loses which note it is on); a cap per file (a note with
several files could fill most of the eight excerpts).

### D349. The text is kept before embedding; a permanent embedding error fails the attachment (6, 2026-10-01)

**Decided:** the extracted text is saved on the row (still `extracting`) before embedding, so a
retry after a rate-limited embedding never reads the file or pays for the vision call again. An
`EmbeddingError` fails it ("couldn't be made searchable"); a note in the same case keeps its old
chunks (D65), but an attachment has none to keep. After an embedding model change,
`reindex_notes` also re-embeds every ready attachment from its stored text (`index_attachment`,
reusing vectors by hash). **Alternative:** read the file again on each retry (a second vision
charge); leave it `extracting` for the sweeper (an hour of a spinner for an error known at once).

### D400. Memory is learned only from the user's own words; the answer is shown but untrusted (3-memory 1, 2026-10-01)

**Decided:** the extraction call (`prompts/memory.md`, `memory-v1`) gets the user's known facts,
the question and the answer -- never the excerpts. The answer quotes the notes, so the prompt
calls it context only: a fact may come only from what the user states about themselves in the
question, never from the answer or because any text says "remember". Every part is neutralised
like the other prompts (D56), and `<fact>`/`<facts>` join the neutralised tags of every prompt,
so a note can never make an ask look like an extraction. The fake extractor reads only the
question, and the injection test fails if it ever reads the answer (the note's quoted sentence
would be a fact). **Alternative:** the question only (the answer helps resolve what a short
question refers to, which the plan wants); a lexical check that each fact's words appear in the
question (drops paraphrases a real model rightly makes: "I don't eat meat" -> "User is
vegetarian").

### D401. Extraction follows a done conversation turn, queued on commit; memory off is checked three times (3-memory 1, 2026-10-01)

**Decided:** `_finish` queues `extract_memory` with `transaction.on_commit` the way the fold is
queued (D284): the `.delay` is wrapped, so a broker that is down costs the extraction only. Only
a conversation turn that ends `done` (a floor answer too: the question can still state a fact);
a plain `POST ask/` and a failed turn queue nothing. `memory_enabled` is checked when queueing,
when the task starts (off: no provider call, no embedding call, no usage event), and again
under the user's row lock just before writing (switched off during the call: nothing written).
**Alternative:** plain asks too (the V1 endpoint, no longer a screen, D304); one check only (a
switch flipped mid-call would still write a fact).

### D402. The call is shown all of the user's live facts while there are at most 10, else the 10 nearest (3-memory 1, 2026-10-01)

**Decided:** `MEMORY_SIMILAR_FACTS` (10). Up to 10 live facts are all sent with no embedding
call; beyond that the question is embedded and the 10 nearest live facts of the current
embedding model are sent, owner-scoped in SQL with `hnsw.ef_search` raised as the chunk search
does (D68). The ids sent are the only ids an operation may name. **Alternative:** always
vector-search (an embedding call per turn, and HNSW's post-filter can miss a user's few facts
among many users'); send every fact (a prompt that grows without bound).

### D403. A fact that looks like a secret is dropped in code, whoever stated it (3-memory 1, 2026-10-01)

**Decided:** besides the prompt's rule, `parse_operations` drops a fact text that mentions a
password, passcode, PIN (not "PIN code", the postal one), OTP, CVV, API/secret/private key or
token, or holds a run of 9+ digits (card, account, phone and ID numbers; a date or a 6-digit
PIN code is not). Memory rides in later prompts and is listed on screen: it is the wrong place
for a secret even when the user typed it, and the filter also catches a model that obeyed a
planted "remember the password" despite the prompt. **Alternative:** trust the prompt alone (one
disobedient reply stores a credential).

### D404. The reply schema: one bad reply drops all, one bad operation drops itself (3-memory 1, 2026-10-01)

**Decided:** the reply must be `{"operations": [...]}` with at most `MEMORY_MAX_OPERATIONS` (5)
entries, or all of it is dropped. Each entry is then checked alone: `op` in add / update /
supersede / none; `id` a JSON integer (not `true`, not `"12"`) among the facts shown, targeted
once; `text` non-empty and at most 200 characters after collapsing whitespace (dropped, never
cut: a cut fact can change meaning) and not a secret (D403); `kind` static or dynamic (absent:
static, or unchanged on an update). Keys beyond these are ignored, and one surrounding code
fence is stripped, since models add both unasked. Dropped entries are logged by reason, never
with their text. Nothing is retried. **Alternative:** reject the whole reply for any bad entry
(one stray entry loses the good ones); reject extra keys (a "reason" field would lose facts).

### D405. `superseded_by` cascades: deleting a fact deletes the facts it replaced (3-memory 1, 2026-10-01)

**Decided:** `on_delete=CASCADE`. When a user deletes "User is vegan" (or it expires), the
"User is vegetarian" it superseded goes with it. **Alternative:** SET_NULL (the replaced fact
comes back to life: deleting one fact would resurrect an older, contradicted one); PROTECT
(a fact could not be deleted while it has history).

### D406. The daily purge: expired dynamic facts, and superseded ones 30 days after they were replaced (3-memory 1, 2026-10-01)

**Decided:** `purge_expired_facts` (beat, daily) deletes facts whose `valid_until` has passed
(dynamic ones, `MEMORY_DYNAMIC_FACT_DAYS` = 30 after they were learned or last updated) and
facts whose superseding fact is older than `MEMORY_SUPERSEDED_RETENTION_DAYS` (30). A superseded
fact is never used or shown to the extraction; it is kept a while only to debug an extraction
that went wrong, and then it is personal data with no purpose. **Alternative:** keep superseded
facts for ever (a growing record of what the user used to be); delete them on supersede (no
trace when a model supersedes wrongly). No `superseded_at` column: the replacing fact's
`created_at` is that moment.

### D407. An extraction runs at most once per turn and never retries; refunds when nothing was billed (3-memory 1, 2026-10-01)

**Decided:** the `memory_extract` use (user None, linked to the turn) is consumed in a short
transaction that locks the turn's row and first looks for any earlier `memory_extract` event of
that turn, refunded or not: a redelivered or duplicated task does nothing. The limit reached:
skipped, no call. A `ChatError`/`TransientChatError`, or a question that cannot be embedded
(checked before the call, so no chat call is paid for facts that could not be stored): refunded.
A malformed reply, or facts that cannot be embedded after the call: dropped and logged, the use
stays counted (the call was made). The task has no Celery retry: the user saying it again is
the retry. **Alternative:** autoretry (spends the limit on a call that keeps failing, and could
loop on a reply that is always malformed); store facts unembedded (they would be listed but
never found by the vector search that uses them).

### D408. Write rules: update in place, supersede by a new row, exact repeats skipped, all under the user's lock (3-memory 1, 2026-10-01)

**Decided:** one transaction that locks the user's row, then re-reads and locks the target
facts live and owner-scoped in SQL. `update` rewrites text, kind, vector and `source_ask` and
recomputes `valid_until` (restating a dynamic fact renews it). `supersede` creates the new fact
and sets the old one's `superseded_by`. `add` is skipped when a live fact has the same text,
ignoring case. A target superseded or expired since the call is dropped, so of two extractions
racing on one fact only the first supersedes it. **Alternative:** no lock (two extractions could
both supersede one fact, leaving two live successors); fuzzy duplicate detection (the model is
shown the similar facts and is the better judge).

### D409. The fake extractor: "I'm X" / "my X is Y" in the question, superseding by subject (3-memory 1, 2026-10-01)

**Decided:** when the user message starts with `<facts>`, the fake provider reads only the
`<question>`. Each sentence that is not a question and says "I'm X" / "I am X" (or "I'm not X",
"I'm no longer X") or "my X is Y", in at most six words of value, is a statement about a
subject ("is X", "my X"). A subject no known fact covers is an `add` ("User is X." / "User's X
is Y."); a different statement about a known fact's subject supersedes it; the same statement
is nothing; none at all is `{"op": "none"}`. "Today", "this week", "currently"... make it
dynamic. Deterministic, so the flow is tested without mocks. **Alternative:** a fixed reply
(cannot show add vs supersede); reading the answer too (would defeat the boundary test).

### D360. Streaming is an optional provider method; the boundary falls back to `complete` (2-streaming 1, 2026-10-01)

**Decided:** a provider may have `stream(system, user, model, max_output_tokens)`, yielding text
deltas and then one `ChatResult`, last (the `StreamingChatProvider` Protocol; `ChatProvider` is
unchanged, so a provider without it still matches). `chat.stream()` sits beside `chat.complete()`:
same provider, model and ceiling; it drops empty deltas, raises `ChatError` if no result comes, and
closes the provider's stream (and so its connection) when closed early. A provider without `stream`
is called through `complete` and yields its whole answer as one delta. The result's text is the
deltas joined and stripped, with the same stop-reason, status and finish-reason checks as
`complete`, so a streamed answer and a complete one are stored identically. Only the answer call
streams; condense, fold and format stay on `complete`. **Alternative:** an `on_delta` callback on
`complete` (changes the signature every existing caller and test double has); make `stream`
required (every test double and future provider would need one).

### D361. One SSE reader in `_http`; a stream that just stops is transient; error events map like statuses (2-streaming 1, 2026-10-01)

**Decided:** `post_stream` sends the request with `stream=True`, checks the status exactly as
`post_json` does (before any delta), then feeds `iter_content(chunk_size=None)` to `parse_sse`:
lines split on `\n` (a trailing `\r` dropped) and decoded only once whole, so a character split
between chunks survives; `:` comments (keep-alives), `id:` and `retry:` ignored; an event the stream
ends inside of is dropped, never parsed half-received; a line over 1 MB is refused. A connection that
drops or stalls (`CHAT_TIMEOUT_SECONDS` between two reads), or a stream that ends without the
vendor's last event (`message_stop`, `response.completed/incomplete/failed`, a `finishReason`), is
`TransientChatError`: that is what a dropped connection looks like. Mid-stream error events are
translated as their HTTP status would have been: Claude's `rate_limit_error`, `api_error`,
`overloaded_error` retry; OpenAI's `error` event by code (`rate_limit_exceeded`, `server_error`...);
Gemini's `error` object by `code` through the same status table, else by `status`
(`UNAVAILABLE`, `RESOURCE_EXHAUSTED`...). Everything else is `ChatError`. Tested against
handwritten recordings of each vendor's documented events (`assistant/tests/fixtures/streams/`),
whole, a byte at a time and in odd chunks, plus one opt-in live test each (D40). **Alternative:**
`iter_lines()` (its own buffering and decoding, harder to prove on split characters); a vendor SDK
(three new dependencies, D53).

### D362. The fake streams word by word, replaying the boundary's `chat.complete` (2-streaming 1, 2026-10-01)

**Decided:** `FakeProvider.stream` cuts its answer before each word that follows whitespace (joined,
the pieces are the answer exactly) and yields the result last. When the fake is the configured
provider, the answer it cuts up comes from `assistant.chat.complete`, looked up at call time (with
the ceiling as a keyword, so a spy still sees `(system, user)`); otherwise from its own `complete`.
So the 30-odd existing task and turn tests that script `chat.complete` (refusals, retries, token
counts, prompt contents) script the streamed answer too and pass unchanged. **Alternative:** point
those tests at `chat.stream` (a large diff to tests whose behaviour did not change, and they would no
longer prove the polling path unchanged).

### D363. Events on Redis pub/sub `ask:<id>`: numbered deltas with offsets, `reset`, and `done`/`failed` carrying the row (2-streaming 1, 2026-10-01)

**Decided:** every message is compact JSON with `seq` and `type`: `delta` (`offset`, `text`),
`reset`, and `done` or `failed` whose `ask` is the row through `AskQuerySerializer` -- exactly what
`GET ask/<id>/` returns, parsed citations included. The end event is published on commit
(`transaction.on_commit(..., robust=True)`), so a reader that fetches the row on `done` finds it
done; the row is read afresh for it. `seq` counts from 1 in each run of the task (a retry is a new
run), so 1 may follow anything and any other jump means missed messages. `offset` is where the delta
starts in the run's text, in code points: the same text `partial_answer` holds, so a reader that
caught up from the row skips what it has. **Alternative:** a Redis stream (`XADD`) with replay
(keys to expire and trim, for a catch-up the row already gives); a seq kept across runs in Redis (a
write per run, and it fails exactly when Redis does).

### D364. `partial_answer` is a new field, saved at most every 0.5 s while running, not in the API (2-streaming 1, 2026-10-01)

**Decided:** `AskQuery.partial_answer` (migration 0003) holds the text streamed so far; written by
an UPDATE conditional on `status=running` at most every `ASK_PARTIAL_SAVE_SECONDS` (0.5), so a
late save can never touch a finished row; cleared by the claim, by done (`answer` has it), by
failed and by the sweeper. Not serialized: polling shows a finished answer only, as before; the
stream endpoint (sub-task 2) reads it for catch-up. **Alternative:** reuse `answer` while running
(V1 polling clients would start seeing half an answer on a running row, and a failed ask would need
it cleared from the field clients read); save every delta (a write per word).

### D365. A retry starts over: the partial text is cleared and `reset` published (2-streaming 1, 2026-10-01)

**Decided:** a transient error after some text was streamed clears `partial_answer` and publishes
`reset` before re-raising for Celery's retry, so readers drop the text at once rather than during
the backoff; a run that takes up an ask that was already `running` (a retry, or a redelivery after a
crash that published deltas it never saved) publishes `reset` first and clears the text with its
claim. A duplicate reset is harmless. Limits, refunds and cost are untouched: a retry still does not
refund, giving up still does. **Alternative:** continue from the partial text (the provider cannot
resume an answer; a new call writes a different one).

### D366. Publishing can never fail an answer (2-streaming 1, 2026-10-01)

**Decided:** `ASK_EVENTS_REDIS_URL` defaults to `CELERY_BROKER_URL` (pub/sub ignores the database
number, and a deployment that set the broker gets streaming without another setting); empty turns
events off, and the test runner forces it off as it forces the fake providers. The client has 0.5 s
connect and 1 s socket timeouts; every publish error is logged once per run and swallowed; after the
first failure the run skips its remaining deltas (one timeout, not one per word) but still tries
`reset` and the end event; a URL that cannot be parsed turns events off. With events off no on-commit
callback is queued at all. The publisher takes any client with `publish(channel, message)`, so the
task tests use a recorder; one test uses the real local Redis on database 15 and is skipped without
one. **Alternative:** a separate default database (pub/sub does not use it); fail the run on a Redis
error (polling would have had the answer).
