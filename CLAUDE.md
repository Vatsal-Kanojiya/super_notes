# Super Notes — working rules for Claude Code

Read this first in every session. State lives in the repo, not in chat: if it isn't in `docs/`,
it didn't happen.

## Where things stand
- **Active line: branch `v2`.** `master` is the released V1 line (fixes only). Check out `v2`
  before doing V2 work.
- Status and next steps: `docs/V2_PLAN.md` (plan, open questions in §2), `docs/COMMIT_PLAN.md`
  (V1 phase table), the end of `docs/BUILD_LOG.md` (what happened last), `docs/BACKLOG.md`.
- Every judgement call is recorded in `docs/DECISIONS.md` (`D1…`, continue the numbering).

## Git (hard rules)
- Commit as the repo's configured identity (`vatsal_kanojiya`, GitHub noreply). If a fresh clone
  has none: `git config user.name vatsal_kanojiya` and
  `git config user.email 98935822+Vatsal-Kanojiya@users.noreply.github.com`.
- **Never add `Co-Authored-By: Claude` or any "Generated with Claude" line** to commits or PRs.
- Conventional Commits, one reviewable idea per commit.
- V2 workflow (D77): branch `v2-feat/<phase>-<slug>` from `v2` → build → review → `git merge
  --no-ff` into `v2` → push → tag `v2-phase-N-<name>` when a phase completes. Fixes on `master`
  are merged into `v2` the same day. Never push `--force` to `master` or `v2`.
- Push after every merge, so GitHub is always the source of truth.

## Working method
- Before merging: `python manage.py test`, `ruff check . && ruff format --check .`,
  `python manage.py makemigrations --check --dry-run`, regenerate `docs/openapi.yml`
  (`manage.py spectacular --file docs/openapi.yml --validate --fail-on-warn`),
  `cd web && npm run build`. Update `BUILD_LOG.md` and `DECISIONS.md`.
- Sub-agents: they leave work **uncommitted**; the main session reviews the diff, then commits.
  Split briefs into checkpoints. Use Opus only for correctness/security/concurrency-critical work,
  Sonnet for well-specified work. Give exact files to read.
- No new dependency (pip or npm) without the owner's approval (`docs/V2_PLAN.md` §3).
- Don't build what needs the owner's decision; park it in `docs/BACKLOG.md` or ask.
- Owner's style: answers quick and crisp; plain language; say what's pending.

## Environment
- Postgres 16 + pgvector, required (no SQLite). Local: `DATABASE_URL=postgres:///super_notes`.
  Redis for Celery. Python venv in `.venv`; Node 24 via nvm for `web/`.
- AI providers default to `fake`; real ones need keys (README "Turning on the real services").
