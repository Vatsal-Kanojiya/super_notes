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
  Note,
  NoteCreateRequest,
  NoteListParams,
  NoteUpdateRequest,
  SearchHit,
  SearchParams,
  SignInResponse,
} from './types'

export const authApi = {
  google: (idToken: string) =>
    request<SignInResponse>('auth/google/', { method: 'POST', body: { id_token: idToken }, auth: false }),
  // Logout needs no access token: the refresh token is what it revokes, and
  // an expired access token must not stop someone from signing out.
  logout: (refresh: string) => request<void>('auth/logout/', { method: 'POST', body: { refresh }, auth: false }),
  me: () => request<Me>('me/'),
  devices: () => request<Device[]>('auth/devices/'),
  signOutDevice: (id: Id) => request<void>(`auth/devices/${id}/`, { method: 'DELETE' }),
}

export const notesApi = {
  list: (params: NoteListParams = {}) => request<CursorPage<Note>>('notes/', { query: { ...params } }),
  get: (id: Id) => request<Note>(`notes/${id}/`),
  create: (body: NoteCreateRequest) => request<Note>('notes/', { method: 'POST', body }),
  update: (id: Id, body: NoteUpdateRequest) => request<Note>(`notes/${id}/`, { method: 'PATCH', body }),
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
