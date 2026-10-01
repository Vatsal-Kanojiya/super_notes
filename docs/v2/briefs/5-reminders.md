# Brief — `v2-feat/5-reminders`: reminders and calendar (Phase 5)

**Queue:** item 8. Independent of the limits layer (reminders make no AI calls).

## Read
- `CLAUDE.md`; `docs/V2_PLAN.md` §4 (Reminder, ReminderDelivery, PushSubscription) and §5
  Phase 5; `docs/DECISIONS.md` D5, D25 (sync), D87 (email + web push), D89 (service worker rules),
  D95 (the schedule).
- `notes/models.py`, `notes/services.py` (the owner lock and revision stamping — reminder writes
  go through the same lock and bump `notes_revision`), `notes/api/` (views, serializers, the
  `changes` endpoint), `accounts/models.py` (`User.timezone`).

## The schedule (D95)
A reminder has `due_at` (aware, stored UTC) and `lead_days` (default 7, 0–30). Its
occurrences are: one per day at `due_at`'s **local time of day in the user's timezone**, from
`due_date − lead_days` to the due date inclusive (8 for the default). "Mark done" stops the rest.
A pure function `occurrences(due_at, lead_days, tz) -> list[datetime]` is the single source.

## Sub-tasks (conductor's checklist)
- [ ] 1. **Model, schedule, API** (Opus) — `Reminder` (`note`, `owner`, `due_at`, `lead_days`,
  `channels`, `status` `scheduled|done|cancelled`, timestamps, `deleted_at`), `ReminderDelivery`
  (`reminder`, `occurrence_at`, unique together, `sent_at`, `channel_results`), migration;
  `occurrences()`; `POST notes/<id>/reminders/`, `PATCH/DELETE reminders/<id>/`,
  `POST reminders/<id>/done/`, `GET reminders/?from=&to=` (each reminder with its occurrences in
  range; range ≤ 62 days); writes through the owner lock bumping `notes_revision`; reminders
  included per note in `notes/changes/`; a deleted note cancels its reminders. Tests: DST
  (`Europe/London` across the October change), lead 0 and 7, ownership (other user → 404),
  range validation, sync.
- [ ] 2. **Delivery engine + email** (Opus) — a Celery beat task every minute: find due
  occurrences (≤ now, not yet delivered, reminder `scheduled`, note live), claim each by inserting
  its `ReminderDelivery` (the unique constraint makes a second worker's insert fail → at most
  once), enqueue sending on commit. After an outage, only the latest missed occurrence per
  reminder is sent. Email channel via Django's mail (no note body unless D-decided otherwise —
  title + due date + link). Tests: exactly-once with two concurrent sweeps
  (`TransactionTestCase` + threads), outage catch-up, done/cancelled/deleted never sent.
- [ ] 3. **Web push backend** (Sonnet) — `pywebpush` (approved D87; the conductor adds it to
  `requirements.txt`), `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` / `VAPID_SUBJECT` settings (env),
  a `generate_vapid_keys` management command, `PushSubscription` model, `GET push/vapid-key/`,
  `POST/DELETE me/push-subscriptions/`, the push channel in delivery (payload: reminder id, note
  id, title only), 404/410 → subscription deleted. Push off (no keys) → channel skipped silently.
- [ ] 4. **Web UI** (Sonnet) — a service worker for push only (D89: never caches app code;
  `no-cache`, `skipWaiting` + `clients.claim`), the permission prompt and subscribe flow, a
  reminder control on a note (date-time + lead days), "mark done", a calendar page `/calendar`
  (month and week views of occurrences; clicking opens the note), reminders kept in sync from
  `changes`. vitest for the calendar and schedule display logic. Headless check against the real
  backend.

Done when the branch checklist in `CLAUDE.md` passes; BUILD_LOG entry "V2 5 — reminders".
