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
