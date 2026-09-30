/**
 * Every request and response shape the client relies on, in one place.
 *
 * Written against the plan's API contract (§7) before the backend's OpenAPI
 * schema existed. When `docs/openapi.yml` lands, this file is the only one
 * that should need reconciling: stores and components import their types
 * from here and never spell a payload out themselves.
 */

// ---------------------------------------------------------------- common --

/** Database ids are integers (`-id` cursor ordering, plan §7). */
export type Id = number

/** ISO 8601 date-time string, as DRF renders it. */
export type DateTime = string

/**
 * The error body. `detail` is for people, `code` for programs. A
 * serializer's field errors keep their shape (`{field: [msgs]}`) plus
 * `code: "invalid"`, so any other key may hold a list of messages.
 */
export interface ErrorBody {
  detail?: string
  code?: string
  [field: string]: unknown
}

/** DRF cursor pagination. `next`/`previous` are full URLs carrying `cursor`. */
export interface CursorPage<T> {
  next: string | null
  previous: string | null
  results: T[]
}

// ------------------------------------------------------------------ auth --

export type Plan = 'free' | 'premium'

export interface User {
  id: Id
  email: string
  name: string
  avatar_url: string
  plan: Plan
  date_joined: DateTime
}

export interface AskUsage {
  used: number
  limit: number
  resets_at: DateTime
}

/** `GET me/`: the user plus this month's ask usage. */
export interface Me extends User {
  ask_usage?: AskUsage
}

export interface GoogleSignInRequest {
  id_token: string
}

export interface TokenPair {
  access: string
  refresh: string
}

/** `POST auth/google/`. */
export interface SignInResponse extends TokenPair {
  user: User
}

/** `POST auth/refresh/`: rotation, so a new refresh token comes back every time. */
export interface RefreshRequest {
  refresh: string
}
export type RefreshResponse = TokenPair

/** `POST auth/logout/` → 204. */
export interface LogoutRequest {
  refresh: string
}

/** `GET auth/devices/` → a plain list (not paginated). */
export interface Device {
  id: Id
  kind: 'web' | 'api'
  /** The User-Agent, truncated. Untrusted text: only ever rendered as text. */
  label: string
  created_at: DateTime
  last_seen_at: DateTime
  current: boolean
}

// ----------------------------------------------------------------- notes --

export type NoteType = 'text' | 'checklist'

/** TipTap/ProseMirror JSON. Kept loose: the server stores it as given. */
export interface DocNode {
  type?: string
  attrs?: Record<string, unknown>
  content?: DocNode[]
  marks?: { type: string; attrs?: Record<string, unknown> }[]
  text?: string
  [key: string]: unknown
}

export interface Note {
  id: Id
  type: NoteType
  title: string
  content: DocNode
  /** Plain text derived by the server; read-only. */
  content_text: string
  /** Per-note optimistic-lock counter; sent back on PATCH. */
  version: number
  /** Per-user sync counter (`User.notes_revision` at the time of the write). */
  revision: number
  created_at: DateTime
  updated_at: DateTime
}

/** A deleted note, as `changes` reports it. */
export interface Tombstone {
  id: Id
  revision: number
  deleted_at: DateTime
}

export interface NoteListParams {
  type?: NoteType
  q?: string
  cursor?: string
}

export interface NoteCreateRequest {
  type: NoteType
  title: string
  content: DocNode
}

export interface NoteUpdateRequest {
  /** The version this edit was made on top of. Stale → 409. */
  version: number
  title?: string
  content?: DocNode
}

/** 409 body of a stale PATCH. */
export interface VersionConflictBody {
  detail: string
  code: 'version_conflict'
  current: Note
}

/** `GET notes/changes/?after=<revision>`. */
export interface ChangesResponse {
  results: (Note | Tombstone)[]
  latest_revision: number
  /** Present when the server caps a page; the client loops while true. */
  has_more?: boolean
}

export function isTombstone(item: Note | Tombstone): item is Tombstone {
  return 'deleted_at' in item && Boolean((item as Tombstone).deleted_at)
}

// ---------------------------------------------------------------- search --

export interface SearchParams {
  q: string
  k?: number
}

/** `GET search/` → a plain list of hits, best first. */
export interface SearchHit {
  note_id: Id
  chunk_id: Id
  title: string
  heading_path: string
  text: string
  score: number
}

// ------------------------------------------------------------------- ask --

export type AskStatus = 'pending' | 'running' | 'done' | 'failed'

export interface Citation {
  /** The `[n]` marker in the answer. */
  n: number
  note_id: Id
  chunk_id: Id
  title: string
  snippet: string
}

export interface AskQuery {
  id: Id
  question: string
  status: AskStatus
  answer: string
  citations: Citation[]
  error: string
  created_at: DateTime
  completed_at: DateTime | null
}

/** `POST ask/` (with an `Idempotency-Key` header) → 202 AskQuery. */
export interface AskRequest {
  question: string
}

/** 429 body when the month's asks are used up. */
export interface QuotaExceededBody extends AskUsage {
  detail?: string
  code: 'quota_exceeded'
}
