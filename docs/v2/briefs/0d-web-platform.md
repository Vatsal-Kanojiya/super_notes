# Brief — `v2-feat/0d-web-platform`: router, tests, no stale JavaScript, lifecycle (D86, D89, D90)

**Model:** Sonnet. **Checkpoint:** after Part 1. **Queue:** item 3. Edits `web/` only (plus the
CI web job). Built against the contract in `0e-lifecycle.md`; can start before 0e merges.

## Read
- `CLAUDE.md`; `docs/DECISIONS.md` D46–D52 (current client design), D86, D88, D89, D90, D93, D94.
- `web/src/` (stores, `api/`, components), `web/vite.config.ts`, `web/package.json`,
  `.github/workflows/ci.yml` (web job), and `docs/v2/briefs/0e-lifecycle.md` (the API contract).

## Part 1 — router and tests
- `vue-router` (approved): `/notes`, `/notes/:id`, `/ask` (until phase 1 adds `/chat`),
  `/settings`; the view store (D46) goes; back button, refresh and deep links work; a guard sends
  signed-out users to sign-in.
- `vitest` (approved): `npm test`; tests for citation splitting, the refresh single-flight and the
  notes sync loop; add `npm test` to the CI web job.
**Stop and report.**

## Part 2 — no stale JavaScript, lifecycle
- Build id `VITE_APP_VERSION` = `YYYYMMDDHHMM-<shortsha>` injected at build (vite `define`).
- Version check on load, on focus and every 5 minutes against `GET app/version/`, plus the
  `X-Client-Min-Version` response header: newer build → a "New version — reload" bar (auto-reload
  when no edit is unsaved); below minimum → forced reload.
- `vite:preloadError` → one reload (guarded against loops with `sessionStorage`).
- `web/README.md`: the hosting rules — hashed `assets/*` `immutable, max-age=31536000`;
  `index.html` and the service worker `no-cache`.
- Lifecycle (D93): `POST session/open/` on launch with a valid session, and on **resume** — the
  first user interaction (click, key, scroll, touch) after `5` hours with none; track
  `lastInteractionAt` in memory and `localStorage`, so a window left open for months still
  reports each new day of use. Render notices — the memory banner (prominent vs
  subtle per D88, "Review / turn off" → `/settings`, dismiss → `memory-notice/seen/`) and the
  update bar.
- Settings page: memory on/off (`PATCH me/`), timezone (sent automatically from
  `Intl.DateTimeFormat().resolvedOptions().timeZone` when the user's is the default).
- Branch checklist in `CLAUDE.md`; `npm run build` and `npm test` green. BUILD_LOG entry
  "V2 0d — web platform".

## Sub-tasks (conductor's checklist)
- [x] 1. vue-router replacing the view store, guards, deep links (Part 1a)
- [x] 2. vitest + the three test suites + CI `npm test` (Part 1b)
- [x] 3. Build id, version check and update bar, `vite:preloadError`, hosting rules in README (Part 2a)
- [ ] 4. Lifecycle: app-open detection (launch / 5 h idle), notices UI, settings page (Part 2b)
