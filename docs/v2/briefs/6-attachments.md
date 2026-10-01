# Brief — `v2-feat/6-attachments`: attachments and summaries (Phase 6)

**Queue:** item 9. Decisions D320–D359.

## Read
- `CLAUDE.md`; `docs/V2_PLAN.md` §4 (Attachment, NoteChunk, NoteSummary) and §5 Phase 6, §6
  (security); `docs/DECISIONS.md` D84, D85, D91 (`summary`, `storage_bytes` limits), D92, D101–D104.
- `notes/` (models, services and the owner lock, api, `changes`), `config/settings.py`
  (`MaxUploadSizeMiddleware`, LIMIT_DEFAULTS), `limits/service.py`, `retrieval/` (chunking,
  indexing, `NoteChunk`, search), `assistant/citations.py`, `assistant/chat/` (provider layer),
  `notes/format_*` and `notes/tasks.py` (the job pattern as most recently built).

## Design anchors
- **Storage** (D85): `django-storages` S3 backend when `AWS_STORAGE_BUCKET_NAME` is set, local
  disk (`MEDIA_ROOT`, never under a served URL) otherwise; files under a random name, never the
  client's name.
- **Upload** `POST notes/<id>/attachments/` (multipart): JPEG, PNG, WebP, PDF only, type sniffed
  from magic bytes (the client's type and extension are ignored); per-file cap (setting, 10 MB)
  and the `storage_bytes` limit (per user and system, D84/D91) checked and recorded under the
  owner lock; `sha256` unique per note so a re-upload returns the existing row (200).
  `MaxUploadSizeMiddleware` gets a per-path allowance for this endpoint only.
- **Download** `GET attachments/<id>/file/`: authenticated, owner-scoped in SQL (404 otherwise),
  `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, the sniffed type.
- **Extraction** (Celery, on commit): PDF text with `pypdf`; images through a new provider-layer
  `extract_image_text` (fake returns fixed text; real vision adapters can follow). Text chunked
  into `NoteChunk` with `attachment` FK and `source=attachment`; citations name the file.
- **Summaries**: `POST notes/<id>/summarize/` → job (the `summary` limit, refunded on failure) →
  `Note.summary` + `summary_version`; indexed as a `source=summary` chunk. Attachments get a
  summary on extraction (counted under `summary` as a system cost only — decide and record).
- **Deletion**: deleting an attachment (soft) or its note de-indexes its chunks, releases its
  `storage_bytes`, and deletes the file (on commit).
- Attachments and summaries ride on the note in `notes/changes/` (metadata only, never bytes).

## Sub-tasks (conductor's checklist)
- [ ] 1. **Storage, model, upload/download/delete API** (Opus) — `Attachment` model + migration,
  storage settings, magic-byte sniffing, size cap, `storage_bytes` limit under the owner lock,
  sha256 dedup, middleware allowance, download headers, soft delete releasing storage and
  deleting the file on commit, note delete cascading; attachments in `changes`. Tests: a renamed
  `.exe` as `.pdf` refused; another user → 404 on metadata and file; size and quota enforced
  (429); dedup; delete removes the file.
- [ ] 2. **Extraction and indexing** (Opus) — `extracting → ready|failed` task, `pypdf`, the
  `extract_image_text` provider method (fake), chunking into `NoteChunk` (`attachment` FK,
  `source`), search and Ask/chat citations naming the attachment, de-indexing on delete; caps on
  extracted text and PDF pages (decompression bombs, encrypted PDFs → failed, not crashed).
- [ ] 3. **Summaries** (Sonnet) — `POST notes/<id>/summarize/` job, `Note.summary` /
  `summary_version`, summary chunk, stale summary visible, attachment summaries, `summary` limit
  with refunds; tests.
- [ ] 4. **Web** (Sonnet) — attach (picker + drop), list with status, download, delete, the
  note's summary with "Summarize" / stale marker, citation chips naming the file; vitest;
  headless check against the real backend.

Done when the branch checklist in `CLAUDE.md` passes; BUILD_LOG entry "V2 6 — attachments".
