# Brief — `v2-feat/0e-lifecycle`: app version, lifecycle hooks, memory notices (D88–D90)

**Model:** Sonnet. **Checkpoint:** after Part 1. **Queue:** item 2. Parallel-safe with 0c except
`accounts/google.py` (add the signal call only; keep the diff small).

## Read
- `CLAUDE.md`; `docs/DECISIONS.md` D88, D89, D90; `docs/V2_PLAN.md` §5 Phase 0 "Added" block.
- `accounts/models.py`, `accounts/api.py` (`MeView`, `issue_tokens`), `accounts/google.py`,
  `accounts/devices.py` (`SignedInDevice`, the `device` claim in tokens), `config/api/urls.py`,
  `config/middleware.py`.

## The contract (the web client is built against exactly this)
- `GET /api/v1/app/version/` — public, unthrottled:
  `{"latest": "<build>", "min_supported": "<build or empty>"}` from settings
  `CLIENT_LATEST_VERSION` / `CLIENT_MIN_VERSION` (env). A build id is
  `YYYYMMDDHHMM-<shortsha>`; ids compare by their timestamp prefix.
- Every API response carries `X-Client-Min-Version: <build>` when `CLIENT_MIN_VERSION` is set
  (and CORS exposes the header).
- `POST /api/v1/session/open/` (authenticated) — body `{"platform": "web"|"android",
  "app_version": "<build>", "reason": "launch"|"resume"}` → `200 {"notices": [...],
  "server_time": "<iso>"}`. Notices:
  - `{"kind": "update", "required": bool}` when `app_version` is older than `latest`
    (`required` when older than `min_supported`);
  - `{"kind": "memory", "style": "prominent"|"subtle", "state": "on"|"off"}` per D88 — due on
    the user's first session open and then every `MEMORY_NOTICE_DAYS` (default 14) since
    `memory_notice_seen_at`.
  Sends `app_opened(user, device, platform, app_version, reason)`; at most once per device per
  `APP_OPEN_MIN_INTERVAL_SECONDS` (default 300) — notices are returned either way. Updates the
  device's `last_seen_at`.
- `POST /api/v1/me/memory-notice/seen/` → 204, sets `memory_notice_seen_at`.
- `PATCH /api/v1/me/` `{"timezone"?, "memory_enabled"?}` — timezone validated as an IANA name;
  setting `memory_enabled` also sets `memory_choice_explicit = true`.
- `user_signed_in(user, request, device, created)` sent on every successful Google sign-in.

## Part 1
`User` fields (`timezone` default `Asia/Kolkata`, `memory_enabled` default true,
`memory_choice_explicit`, `memory_notice_seen_at`) + migration; the two signals in
`accounts/signals.py`; `user_signed_in` wired into sign-in; `PATCH me/`; tests. **Stop and report.**

## Part 2
`app/version/`, the header middleware, `session/open/` with notices and throttling,
`memory-notice/seen/`, tests (notice rules for all three D88 states, throttle, version compare,
another user's device can't be used). Branch checklist in `CLAUDE.md`. Decisions from the next
free number. BUILD_LOG entry "V2 0e — lifecycle".
