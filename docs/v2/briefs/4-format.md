# Brief — `v2-feat/4-format`: "format my note" (Phase 4)

**Queue:** item 5. Decisions D160–D169.

## Read
- `CLAUDE.md`; `docs/V2_PLAN.md` §4 (FormatJob) and §5 Phase 4; `docs/DECISIONS.md` D23 (content
  text), D53–D57 (providers, prompts), D84, D91, D101–D104, D130.
- `notes/` (models, content.py — the validator and text derivation, services, api),
  `assistant/chat/` (the `complete` boundary, errors), `assistant/tasks.py` (the job pattern:
  claim, finish, fail + refund), `limits/service.py`.

## Design anchors
- `POST notes/<id>/format/` with `Idempotency-Key` → `202` FormatJob (`base_version` = the note's
  version now); poll `GET format-jobs/<id>/`. Consumes the `format` limit (D91) under the user
  lock; a failed job refunds it (same pattern as asks, D102).
- The task sends the TipTap JSON with a versioned prompt `notes/prompts/format.md`: improve
  structure (headings, lists, checklists), fix obvious typos, never add or remove facts; strict
  JSON output.
- **Guardrail** (the core): the result must pass `notes/content.py`'s validator, its derived text
  must keep the original's words (token-set overlap ≥ a setting, e.g. 0.9), and it must introduce
  no number or date absent from the original. Otherwise the job fails with
  `format_changed_content` (refunded).
- **Apply** is the client's ordinary `PATCH` with `version = base_version` — a note edited
  meanwhile gets V1's 409. The server never writes the note itself.

## Sub-tasks (conductor's checklist)
- [x] 1. **Backend** (Sonnet) — FormatJob model + migration, service (lock, limit, idempotency),
  task (claim/finish/fail+refund), prompt, guardrail as a pure, heavily tested function, API,
  admin; tests incl. ~15 guardrail cases (dropped paragraph, invented date/number, changed
  amount, typo fix allowed, restructuring allowed), ownership, limit 429, refund on failure. The
  fake chat provider path: mock `complete` in tests with canned outputs.
- [x] 2. **Web** (Sonnet) — a "Format" button on a note, polling, a before/after preview, "Apply"
  via PATCH with `base_version` (conflict prompt on 409), usage shown from `me/` `limits.format`;
  vitest; headless check.

Done when the branch checklist in `CLAUDE.md` passes; BUILD_LOG "V2 4 — format".
