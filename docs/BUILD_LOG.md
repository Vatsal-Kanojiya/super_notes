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
