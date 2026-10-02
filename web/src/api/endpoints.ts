/**
 * One typed function per endpoint. Paths are relative to `/api/v1/` and keep
 * Django's trailing slash.
 */
import { request, requestBlob, upload } from './client'
import type {
  AskQuery,
  Attachment,
  AskRequest,
  ChangesResponse,
  Conversation,
  ConversationDetail,
  CursorPage,
  Device,
  FormatJob,
  Id,
  Me,
  MeUpdateRequest,
  Note,
  NoteCreateRequest,
  NoteListParams,
  NoteUpdateRequest,
  PushSubscriptionRequest,
  Reminder,
  ReminderInRange,
  ReminderWriteRequest,
  SearchHit,
  SearchParams,
  SessionOpenResponse,
  SignInResponse,
  SummaryJob,
  UserFact,
  VapidKey,
} from './types'

export const authApi = {
  google: (idToken: string) =>
    request<SignInResponse>('auth/google/', { method: 'POST', body: { id_token: idToken }, auth: false }),
  // Logout needs no access token (the schema marks it public): the refresh token is what it revokes, and
  // an expired access token must not stop someone from signing out.
  logout: (refresh: string) => request<void>('auth/logout/', { method: 'POST', body: { refresh }, auth: false }),
  me: () => request<Me>('me/'),
  updateMe: (body: MeUpdateRequest) => request<Me>('me/', { method: 'PATCH', body }),
  memoryNoticeSeen: () => request<void>('me/memory-notice/seen/', { method: 'POST' }),
  devices: () => request<Device[]>('auth/devices/'),
  signOutDevice: (id: Id) => request<void>(`auth/devices/${id}/`, { method: 'DELETE' }),
}

export const sessionApi = {
  /** Report an app open (D93); the reply carries the notices to show. */
  open: (appVersion: string, reason: 'launch' | 'resume') =>
    request<SessionOpenResponse>('session/open/', {
      method: 'POST',
      body: { platform: 'web', app_version: appVersion, reason },
    }),
}

export const appApi = {
  /** Public: the newest build and the oldest one still accepted. */
  version: () => request<{ latest: string; min_supported: string }>('app/version/', { auth: false }),
}

export const notesApi = {
  list: (params: NoteListParams = {}) => request<CursorPage<Note>>('notes/', { query: { ...params } }),
  get: (id: Id) => request<Note>(`notes/${id}/`),
  create: (body: NoteCreateRequest) => request<Note>('notes/', { method: 'POST', body }),
  update: (id: Id, body: NoteUpdateRequest, options: { keepalive?: boolean } = {}) =>
    request<Note>(`notes/${id}/`, { method: 'PATCH', body, keepalive: options.keepalive }),
  remove: (id: Id) => request<void>(`notes/${id}/`, { method: 'DELETE' }),
  changes: (after: number) => request<ChangesResponse>('notes/changes/', { query: { after } }),
}

export const searchApi = {
  search: (params: SearchParams) => request<SearchHit[]>('search/', { query: { ...params } }),
}

export const askApi = {
  create: (body: AskRequest, idempotencyKey: string) =>
    request<AskQuery>('ask/', { method: 'POST', body, headers: { 'Idempotency-Key': idempotencyKey } }),
  get: (id: Id) => request<AskQuery>(`ask/${id}/`),
  list: (cursor?: string) => request<CursorPage<AskQuery>>('ask/', { query: { cursor } }),
}

export const remindersApi = {
  create: (noteId: Id, body: ReminderWriteRequest & { due_at: string }) =>
    request<Reminder>(`notes/${noteId}/reminders/`, { method: 'POST', body }),
  update: (id: Id, body: ReminderWriteRequest) => request<Reminder>(`reminders/${id}/`, { method: 'PATCH', body }),
  done: (id: Id) => request<Reminder>(`reminders/${id}/done/`, { method: 'POST' }),
  remove: (id: Id) => request<void>(`reminders/${id}/`, { method: 'DELETE' }),
  /** Reminders with a notification in [from, to) (at most 62 days), with those notifications. */
  range: (from: string, to: string) =>
    request<{ results: ReminderInRange[] }>('reminders/', { query: { from, to } }),
}

export const pushApi = {
  /** 404 means web push is off on this server. */
  vapidKey: () => request<VapidKey>('push/vapid-key/'),
  subscribe: (body: PushSubscriptionRequest) => request<void>('me/push-subscriptions/', { method: 'POST', body }),
  unsubscribe: (endpoint: string) =>
    request<void>('me/push-subscriptions/', { method: 'DELETE', body: { endpoint } }),
}

export const formatApi = {
  /** Starts a format job; the note is not changed. A fresh `Idempotency-Key` per request. */
  create: (noteId: Id, idempotencyKey: string) =>
    request<FormatJob>(`notes/${noteId}/format/`, { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey } }),
  get: (id: Id) => request<FormatJob>(`format-jobs/${id}/`),
}

export const conversationsApi = {
  list: (cursor?: string) => request<CursorPage<Conversation>>('conversations/', { query: { cursor } }),
  /** Empty (201), or with a question asked as turn 1 (needs the key; 201, or 200 for a replayed key). */
  create: (question?: string, idempotencyKey?: string) =>
    request<ConversationDetail>('conversations/', {
      method: 'POST',
      body: question === undefined ? {} : { question },
      headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
    }),
  get: (id: Id) => request<ConversationDetail>(`conversations/${id}/`),
  rename: (id: Id, title: string) => request<Conversation>(`conversations/${id}/`, { method: 'PATCH', body: { title } }),
  remove: (id: Id) => request<void>(`conversations/${id}/`, { method: 'DELETE' }),
  /** 202 with the pending turn, 200 for a replayed key, 409 `turn_in_progress` while the last turn runs. */
  ask: (id: Id, question: string, idempotencyKey: string) =>
    request<AskQuery>(`conversations/${id}/turns/`, {
      method: 'POST',
      body: { question },
      headers: { 'Idempotency-Key': idempotencyKey },
    }),
}

export const memoryApi = {
  list: (cursor?: string) => request<CursorPage<UserFact>>('memory/facts/', { query: { cursor } }),
  forget: (id: Id) => request<void>(`memory/facts/${id}/`, { method: 'DELETE' }),
  forgetAll: () => request<void>('memory/facts/', { method: 'DELETE' }),
}

export const attachmentsApi = {
  /** Newest first, one page. */
  list: (noteId: Id, cursor?: string) =>
    request<CursorPage<Attachment>>(`notes/${noteId}/attachments/`, { query: { cursor } }),
  /** Multipart; 201 for a new file, 200 when the note already had it. Reports 0..1 as it goes. */
  upload: (noteId: Id, file: File, onProgress?: (fraction: number) => void, signal?: AbortSignal) => {
    const form = new FormData()
    form.append('file', file)
    return upload<Attachment>(`notes/${noteId}/attachments/`, form, { onProgress, signal })
  },
  remove: (id: Id) => request<void>(`attachments/${id}/`, { method: 'DELETE' }),
  /** The bytes, fetched with the bearer header (never a URL with a token in it). */
  file: (id: Id) => requestBlob(`attachments/${id}/file/`),
  summarize: (id: Id, idempotencyKey: string) =>
    request<SummaryJob>(`attachments/${id}/summarize/`, { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey } }),
}

export const summaryApi = {
  summarizeNote: (noteId: Id, idempotencyKey: string) =>
    request<SummaryJob>(`notes/${noteId}/summarize/`, { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey } }),
  get: (id: Id) => request<SummaryJob>(`summary-jobs/${id}/`),
}
