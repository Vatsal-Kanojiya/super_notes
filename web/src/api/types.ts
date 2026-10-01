/**
 * Every request and response shape the client relies on, in one place.
 *
 * Reconciled with `docs/openapi.yml`. Stores and components import their
 * types from here and never spell a payload out themselves.
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
  next?: string | null
  previous?: string | null
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

/** One limit key's usage (D84, D132). `limit` and `resets_at` are null for an unlimited key. */
export interface AskUsage {
  used: number
  limit: number | null
  resets_at: DateTime | null
}

/** `GET me/`; the sign-in response's `user` has the same shape. */
export interface Me extends User {
  ask_usage: AskUsage
  /** An IANA name; the server's default is Asia/Kolkata. */
  timezone: string
  memory_enabled: boolean
  /** True once the user has set `memory_enabled` themselves (D88). */
  memory_choice_explicit: boolean
}

/** `PATCH me/`. */
export interface MeUpdateRequest {
  timezone?: string
  memory_enabled?: boolean
}

/** One item of `session/open/`'s `notices`. */
export interface Notice {
  kind: 'update' | 'memory'
  /** `update` only: this build is below the minimum. */
  required?: boolean
  /** `memory` only. */
  style?: 'prominent' | 'subtle'
  state?: 'on' | 'off'
}

export interface SessionOpenResponse {
  notices: Notice[]
  server_time: DateTime
}

export interface GoogleSignInRequest {
  id_token: string
}

export interface TokenPair {
  access: string
  refresh: string
}

/** `POST auth/google/`: the pair plus the account (same shape as `me/`). */
export interface SignInResponse extends TokenPair {
  user: Me
}

/** `POST auth/refresh/`: rotation, so a new refresh token comes back every time. */
export interface RefreshRequest {
  refresh: string
}
/** `RefreshedPair` in the schema: the same two fields as a `TokenPair`. */
export type RefreshResponse = TokenPair

/** `POST auth/logout/` → 204. */
export interface LogoutRequest {
  refresh: string
}

/** `GET auth/devices/` → a plain list (not paginated). */
export interface Device {
  id: Id
  /** Untrusted text: only ever rendered as text. */
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
  /** Always null on a live note (every endpoint except `changes` leaves deleted ones out). */
  deleted_at: DateTime | null
}

/** A deleted note, as `changes` reports it: no title, content or content_text. */
export interface Tombstone {
  id: Id
  type: NoteType
  version: number
  revision: number
  updated_at: DateTime
  deleted_at: DateTime
}

export interface NoteListParams {
  type?: NoteType
  q?: string
  cursor?: string
  /** 1-100, default 25. */
  page_size?: number
}

export interface NoteCreateRequest {
  type: NoteType
  title: string
  content: DocNode
}

/** `PATCH notes/<id>/`: `version` is required, everything else is what changed. */
export interface NoteUpdateRequest {
  /** The version this edit was made on top of. Stale → 409. */
  version: number
  type?: NoteType
  title?: string
  content?: DocNode
}

/** 409 body of a stale PATCH. */
export interface VersionConflictBody {
  detail: string
  code: 'version_conflict'
  current: Note
}

/** `GET notes/changes/?after=<revision>&limit=<1-1000>`, oldest write first. */
export interface ChangesResponse {
  results: (Note | Tombstone)[]
  latest_revision: number
  /** True when the batch was cut at `limit`: call again at once with `latest_revision`. */
  has_more: boolean
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
  /** The headings above the chunk, "A > B" (may be empty). */
  heading_path: string
  /** The whole chunk. */
  text: string
  /** The chunk's start, cut at a word, for a results list. */
  snippet: string
  /** Fused rank score: orders hits, says nothing about relevance alone. */
  score: number
  /** Cosine similarity to the query; null if found by keyword only. */
  similarity: number | null
  /** Full-text rank; null if found by meaning only. */
  keyword_rank: number | null
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

/** `POST ask/` (with an `Idempotency-Key` header) → 202 AskQuery, or 200 for a replayed key. Question is 1-1000 characters. */
export interface AskRequest {
  question: string
}

/** 429 body when the month's asks are used up (`code: "throttled"` is a plain rate limit, no usage). */
export interface QuotaExceededBody extends AskUsage {
  detail: string
  code: 'quota_exceeded'
}
