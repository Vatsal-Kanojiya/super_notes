# Conventions carried over from the reference

Reference: `expense_management` at `c8b841a` (master). This page says what Super Notes copies from
it, what it changes, and what it deliberately leaves behind. **Copy the conventions, not the code
volume.**

## 1. Settings (`config/settings.py`)

| Carried over | Changed here |
|---|---|
| One settings module; django-environ with typed defaults, read from a gitignored `.env` | — |
| `SECRET_KEY` has no default (fail loud); `DEBUG` defaults to `False` | — |
| The `if not DEBUG:` block: HSTS (1 hour default, D9), SSL redirect, secure + `__Host-` cookies, nosniff, referrer policy, `security.W021` silenced | Cookie security is not overridable: there is no compose stack serving plain HTTP |
| `TRUSTED_PROXY_COUNT` feeding DRF's `NUM_PROXIES` and the rate limiter | — |
| `LOGGING` with a request id on every line, `mail_admins` for 5xx | — |
| Celery: JSON only, `acks_late`, prefetch 1, soft/hard time limits, `ALWAYS_EAGER` for tests | Eager mode is forced by the test runner, not by an env var |
| `REST_FRAMEWORK`: deny by default, cursor pagination, user/anon throttles, `NamespaceVersioning`, drf-spectacular, custom exception handler | JWT only (no `SessionAuthentication`, D4); scoped throttles `auth`, `search`, `ask` |
| `SIMPLE_JWT`: 30-minute access, 14-day refresh, rotate + blacklist | `CHECK_REVOKE_TOKEN` dropped: nobody has a password to change |
| CORS: explicit origins, `/api/` only, no credentials | `Idempotency-Key` allowed as a request header |
| `SPECTACULAR_SETTINGS`: split request/response components, `/api/v1` prefix | — |
| `DATABASES` from `DATABASE_URL` | **No SQLite default. Postgres with pgvector is required** (D2), enforced by `config.E001` |

## 2. Middleware and runner

- `config/middleware.py`: `MaxUploadSizeMiddleware` (first; now answers `413` as `{detail, code}`),
  `ContentSecurityPolicyMiddleware` (strict default, a wider policy for the Swagger UI only),
  `RequestIDMiddleware` + `RequestIDFilter` (a `ContextVar`, honours an inbound `X-Request-ID`).
- `config/test_runner.py`: the fast hasher, a dummy cache, eager Celery, and the **fake** embedding
  and chat providers forced regardless of `.env`.
- `config/checks.py`: the Postgres rule and the shared-cache warning for `check --deploy`.

## 3. API shape (`config/api/`, D3)

- Everything under `/api/v1/`, versioned by `NamespaceVersioning`; the schema lives inside the
  version (`/api/v1/schema/`, `/api/v1/docs/`).
- `IdCursorPagination`, ordered by `-id`: a cursor needs a unique, unchanging field, and
  `updated_at` changes on every autosave.
- Errors are `{"detail", "code"}`. `detail` is for people, `code` is for programs. The handler adds
  `code` to every DRF error (`not_authenticated`, `throttled`, `not_found`...), and a serializer's
  field errors keep their shape plus `code: "invalid"`.
- Ownership: **every queryset is filtered by `request.user` in `get_queryset()`**; another user's id
  is a `404`, never a `403`.
- The job pattern (`OwnerJobViewSet` in the reference): create a row → `transaction.on_commit(task.delay)`
  → `202` → client polls `GET …/<id>/`. Used by Ask.

## 4. Provider pattern (`expenses/extraction/` in the reference)

Mirrored for embeddings (`retrieval/embeddings/`) and chat (`assistant/chat/`):

- a `Protocol` in `providers/base.py` (matched, not inherited);
- a registry of **import paths as strings**, resolved lazily, so a deployment using one provider
  never needs another's SDK installed;
- a `fake` provider: no network, no keys, deterministic. It is the default, so a fresh clone and CI
  work with nothing configured;
- one error class at the package boundary; vendor errors are translated into it, transient ones
  are re-raised for Celery to retry;
- API keys come from each SDK's own env var, never a setting.

## 5. Auth (`accounts/google.py`, `accounts/api.py` in the reference)

- `verify_id_token`: `google-auth` checks signature, audience and expiry; `iss` and
  `email_verified` are checked on top. Every failure collapses to one generic refusal, logged by
  exception type only, never the token.
- **Changed:** audience is a *list* (`GOOGLE_OAUTH_CLIENT_IDS`: web and Android), and a repeat
  sign-in is matched on Google's `sub` first, then email.
- `issue_tokens(user)` returns `{access, refresh, user}`; refresh rotates and blacklists; logout
  blacklists one refresh token. `@sensitive_variables()` on every view holding a token.
- Google-only: no passwords, no email verification, no MFA (Google's own MFA covers the account).

## 6. Security posture: ASVS Level 2, as in the reference

The owner asked for the reference's Level 2 posture (D6). Carried over:

| Area | Where |
|---|---|
| Security event trail (`SecurityEvent`, `audit.record`, never raises, never secrets, retention purge) | `accounts/audit.py` |
| Rate limits on sign-in by address, keyed through `TRUSTED_PROXY_COUNT` | `accounts/ratelimit.py` |
| Signed-in device limit (`MAX_SIGNED_IN_DEVICES`, default 2): a further sign-in ends the oldest | `accounts/devices.py` |
| CSP, HSTS, `__Host-` cookies, nosniff, request ids, 413 before parsing | `config/` |
| Sensitive variables scrubbed from error reports | views holding tokens |
| Dependency SLA (7/14/30/90 days) and Dependabot | `SECURITY.md`, `.github/dependabot.yml` |
| Ownership in SQL, `404` for other users' ids, cross-user tests on every resource | every app |

Not carried over: password auth and its validators, breached-password checks, MFA, email
verification, web sessions and templates, exports, Docker.

## 7. Tooling and working method

- `pyproject.toml`: ruff (line length 100, rules `E W F I UP B DJ C4`), coverage with `fail_under`
  (90 to start, D12; the reference's bar is 95).
- `.pre-commit-config.yaml`: whitespace, YAML/TOML, private-key detection, ruff, `makemigrations --check`.
- CI: lint, format, migrations check, `check`, **schema is current** (`docs/openapi.yml`), tests
  with coverage against a pgvector Postgres service, and a separate `check --deploy` job.
- Django's test runner, not pytest. Concurrency tests are `TransactionTestCase` with threads.
- Phases on `master`, one annotated tag each. Conventional Commits, vertical slices.
- `COMMIT_PLAN.md` (phases), `DECISIONS.md` (`D1…`: decided · alternatives · why · reverse it if),
  `BUILD_LOG.md` (what each phase took), `BACKLOG.md` (out of scope).
- Every phase ends with: tests green, ruff clean, `makemigrations --check` clean, schema
  regenerated, docs updated, a built / left / unsure summary.
