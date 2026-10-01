# Brief — `v2-feat/0c-limits`: the limits layer (D84)

**Model:** Opus (concurrency-critical). **Checkpoint:** after Part 1. **Queue:** item 1.

## Read
- `CLAUDE.md`, then `docs/DECISIONS.md` D73–D76 (Ask quota and task) and D84 (this design).
- `docs/V2_PLAN.md` §4 (`UsageEvent`), §5 Phase 0 and its "Added 2026-10-02" block.
- `assistant/quota.py`, `assistant/services.py` (`create_ask`, the user lock),
  `assistant/tasks.py` (where asks end done/failed, `sweep_stuck_asks`), `assistant/api.py` (429
  body, `AskUsageSerializer`), `accounts/api.py` (`MeSerializer.ask_usage`), `accounts/google.py`
  (`find_or_create_user`).
- Concurrency test pattern: `assistant/tests/test_concurrency.py`.

## Part 1 — a new `limits` app
- `Limit`: `key` (unique slug), `user_free`, `user_premium`, `system` (nullable int; null =
  unlimited), `period` (`month` | `day` | `total`), `enabled`, `updated_at`. Admin-editable.
  Defaults in a `LIMIT_DEFAULTS` setting (e.g. `ai_actions` 20 / 500 / system 20000 per month;
  `signups` system 200 per day); a DB row overrides its default.
- `UsageEvent`: `user` (nullable for system-only keys), `key`, `amount`, `refunded`, `ask` FK
  (nullable), `provider`, `model`, `input_tokens`, `output_tokens`, `created_at`. Indexes
  `(user, key, created_at)` and `(key, created_at)`.
- `limits/service.py`:
  - `usage(user, key) -> {used, limit, resets_at}`, `system_usage(key)`.
  - `consume(user, key, amount=1, **meta) -> UsageEvent` — called inside the caller's
    transaction, after the caller locked the user row. Checks the user limit for the period
    (calendar month/day in `TIME_ZONE`, non-refunded events), then the system limit; raises
    `UserLimitExceeded(used, limit, resets_at)` or `SystemLimitExceeded(key)`.
  - `refund(event)`.
- System-limit race: e.g. `pg_advisory_xact_lock(hashtext(key))` only when a system limit is
  set. Decide and record.
- First time a system limit is hit in a period: mail the admins once.
- Tests: per-user by plan, period boundaries, refunds, unlimited; system limit across many users;
  parallel tests at the user edge and the system edge (`TransactionTestCase` + threads) — exactly
  N pass; admin mail once.

**Stop and report.**

## Part 2 — wire it in
- `create_ask` consumes `ai_actions` (linked to the ask) under its existing lock; a failed ask (in
  the task or by the sweeper) refunds it. V1 behaviour holds: failed asks don't count; a replayed
  idempotency key consumes once. Data migration: one event per existing non-failed `AskQuery`.
  `assistant/quota.py` becomes a thin wrapper or goes (record which). V1 quota, concurrency and
  API tests pass with only minimal, justified assertion changes.
- 429 `quota_exceeded` body unchanged; new 503 `{detail, code: "system_limit_reached"}` handled
  once in `config/api/exceptions.py` so any feature gets it.
- `me/`: `ask_usage` unchanged in shape; add `limits: {key: {used, limit, resets_at}}`.
- `find_or_create_user` consumes `signups` (system-only) before creating an account; exhausted →
  new accounts refused with `signups_closed`; existing users still sign in.
- Done when the branch checklist in `CLAUDE.md` passes. Decisions from D91. BUILD_LOG entry
  "V2 0c — limits"; update the quota paragraph in `docs/RAG.md`.
