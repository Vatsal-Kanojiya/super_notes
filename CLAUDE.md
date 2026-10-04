# Super Notes — working rules for Claude Code

Read this first in every session. **State lives in the repo, not in chat:** if it isn't in
`docs/` and pushed to GitHub, it didn't happen.

## Where things stand
- **Active line: branch `v2`.** `master` is the released V1 line (fixes only).
- What to do next: `docs/V2_PLAN.md` §12 **ready queue** (take the first unclaimed item; its
  brief is in `docs/v2/briefs/`). Plan and open questions: `docs/V2_PLAN.md`. What happened last:
  the end of `docs/BUILD_LOG.md`. Judgement calls: `docs/DECISIONS.md` (continue the numbering).

## Git (hard rules, everyone)
- Commit as the repo's identity (`vatsal_kanojiya`, GitHub noreply). Fresh clone with none:
  `git config user.name vatsal_kanojiya` and
  `git config user.email 98935822+Vatsal-Kanojiya@users.noreply.github.com`.
- **Never add `Co-Authored-By: Claude` or any "Generated with Claude" line** to commits or PRs.
- Conventional Commits, one reviewable idea per commit.
- V2 flow (D77): `v2-feat/<item>-<slug>` from `v2` → sub-tasks → review → `git merge --no-ff`
  into `v2` → push → tick the queue. Fixes on `master` are merged into `v2` the same day.
- Never `--force` push `master` or `v2`. No new dependency (pip or npm) without the owner's
  approval (`docs/V2_PLAN.md` §3).

---

## A. Instructions for the conducting session (the main agent)

You **conduct, observe and review**; you don't get lost in the work yourself — even when you are
Opus.

1. **Claim.** Mark the queue item `in progress (<date>)`, commit, push — so no other session
   takes it.
2. **Chunk.** Split the item into **sub-tasks**. Right size: one reviewable idea, roughly
   100–600 changed lines, about 15–60 minutes of agent work, tests included. Never a one-line
   change; never a whole session's worth. Write the list into the item's brief
   (`docs/v2/briefs/<item>.md`, a "Sub-tasks" checklist) and push it before starting.
3. **Delegate by complexity** — always to a sub-agent with an explicit `model`:
   | Model | For |
   |---|---|
   | **Opus** | Concurrency, locking, quotas/limits, money, security boundaries (auth, ownership, uploads), retrieval/prompt design, anything subtle |
   | **Sonnet** | Well-specified implementation, UI, API wiring, tests, docs pages |
   | **Haiku** | Trivial: doc edits, renames, status updates, formatting, lookups and summaries of files |
   Run independent sub-tasks in parallel (separate git worktrees, separate databases
   `createdb sn_<x>`). Never do a complex sub-task inline yourself.
4. **Brief precisely.** Each sub-agent brief names: the goal, the exact files to read, the files
   it may change, the checkpoint (if any), "done when", and the report format (section B).
5. **Observe.** While agents run, don't duplicate their work. If one stalls or drifts, stop it
   (`TaskStop`) and re-brief rather than patching around it.
6. **Review every result before it counts.** Read the diff yourself; run the checks (section C).
   A cheaper model's work is reviewed as strictly as anyone's. Not up to the mark → send it back
   with specific corrections (or redo it on a stronger model). Only then mark the sub-task done.
7. **Commit and push immediately** after a sub-task passes review — on its feature branch,
   pushed to `origin`, so a token limit or a lost laptop costs at most one sub-task. Tick it in the
   brief's checklist in the same push.
8. **On resume after an interruption:** check `git status` in every worktree
   (`git worktree list`). Unreviewed half-done work is either finished by resuming its agent,
   or discarded and the sub-task restarted — decide by reading the diff, never commit it blind.
9. **Close the item:** branch checklist (section C) green → merge `--no-ff` into `v2` → push →
   queue ticked → `BUILD_LOG.md` entry (built / went wrong / left / unsure) → decisions recorded.
   Remove finished worktrees and merged branches.
10. **Don't build what needs the owner.** Park it in `docs/BACKLOG.md` or the plan's open
    questions, mark the queue item **blocked**, and move to the next ready item.
11. **Report to the owner crisply:** done, pending, needs you. Plain language.

## B. Instructions for sub-agents

1. **Stay in scope.** Do exactly your brief; change only the files it allows. Another agent may be
   working in the same repo — never touch its files.
2. **Never commit, push, tag, merge, rebase, stash or install packages.** Leave changes
   uncommitted for the conductor to review.
3. **Read only what the brief lists**; ask (in your report) rather than exploring the whole repo.
4. **Stop at the checkpoint** and report; continue only when told.
5. **Ambiguous or blocked? Stop and report** — don't guess at a product or design decision.
   Record the small judgement calls you do make, in `docs/DECISIONS.md` format, with the
   alternative.
6. **Keep the tree working at every report:** tests pass, nothing half-edited. If you must stop
   early, say exactly where.
7. **Tests with the code.** Every behaviour change gets a test; prefer proving a guard works (e.g.
   remove the lock → the test fails).
8. **Hygiene:** your own worktree and database when told; scripts in `/tmp`, never in the repo;
   stop processes by PID (never `pkill -f <pattern>`, it can kill your own shell); never print
   secrets or keys.
9. **Report when done**, in this order, short:
   - **Done:** what was built (files changed).
   - **Checks:** test count/result, lint, migrations, build.
   - **Decisions:** numbers and one line each.
   - **Unsure / not verified:** honestly.
   - **Needs the owner:** anything blocked.

## C. Checks before merging a branch
```bash
python manage.py test --noinput                      # also with DEBUG=False SECURE_SSL_REDIRECT=False
ruff check . && ruff format --check .
python manage.py makemigrations --check --dry-run
python manage.py spectacular --file docs/openapi.yml --validate --fail-on-warn
cd web && npm run build && npm test                  # npm test once vitest exists
```
CI runs **Python 3.10**; a local 3.12 venv hides 3.11+ behaviour (`datetime.UTC`, `asyncio.TimeoutError`
vs `TimeoutError`, …). Before merging, also run the suite on 3.10:
`uv venv -p 3.10 <scratch>/py310 && uv pip install -p <scratch>/py310/bin/python -r requirements.txt -r requirements-dev.txt`,
then `<scratch>/py310/bin/python manage.py test --noinput`. After pushing, check the CI run went green.

## Environment
- Postgres 16 + pgvector (required): `DATABASE_URL=postgres:///super_notes`. Redis for Celery.
  Python venv `.venv`; Node 24 via nvm for `web/`.
- AI providers default to `fake`; real ones need keys (README, "Turning on the real services").
- Owner's style: quick and crisp; say what's pending and what needs them.
