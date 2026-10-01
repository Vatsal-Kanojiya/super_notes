/**
 * One typed function per endpoint. Paths are relative to `/api/v1/` and keep
 * Django's trailing slash.
 */
import { request } from './client'
import type {
  AskQuery,
  AskRequest,
  ChangesResponse,
  CursorPage,
  Device,
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
