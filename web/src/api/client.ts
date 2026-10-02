/**
 * The one way the client talks to the API.
 *
 * A thin fetch wrapper: base URL, JSON both ways, the bearer token, and the
 * refresh dance. On a 401 it refreshes once (single-flight, so ten requests
 * failing together cause one refresh, D48) and retries the request once. If
 * the refresh itself is refused, the session is over: the tokens are cleared
 * and the registered handler signs the app out. A network failure during a
 * refresh is *not* a refusal and keeps the tokens.
 */
import { clearTokens, getAccess, getRefresh, setTokens } from './tokens'
import type { ErrorBody, RefreshResponse } from './types'

export const API_BASE_URL: string = (
  import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api/v1'
).replace(/\/+$/, '')

/** A failed request. `status` 0 means no response at all (offline, CORS, DNS). */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly detail: string
  readonly body: ErrorBody | null

  constructor(status: number, code: string, detail: string, body: ErrorBody | null) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.detail = detail
    this.body = body
  }
}

export type Query = Record<string, string | number | undefined | null>

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'
  body?: unknown
  query?: Query
  headers?: Record<string, string>
  /** Send the bearer token and refresh on 401. Off for sign-in and refresh. */
  auth?: boolean
  signal?: AbortSignal
  /**
   * Let the request outlive the page (for a save as the tab closes). Only
   * honoured when the body fits KEEPALIVE_MAX_BYTES; a larger one is sent as
   * a normal request, since a browser rejects an oversized keepalive body.
   */
  keepalive?: boolean
}

/**
 * Browsers cap the bodies of all in-flight keepalive requests together at
 * 64 KiB. Stay well under it, so one save does not crowd out another.
 */
export const KEEPALIVE_MAX_BYTES = 60 * 1024

let onAuthLost: (() => void) | null = null
let onMinVersion: ((minVersion: string) => void) | null = null

/** The app-version store hooks in here to hear the `X-Client-Min-Version` header. */
export function setMinVersionHandler(handler: (minVersion: string) => void): void {
  onMinVersion = handler
}

/** The auth store registers its local sign-out here (avoids an import cycle). */
export function setAuthLostHandler(handler: () => void): void {
  onAuthLost = handler
}

function buildUrl(path: string, query?: Query): string {
  const url = new URL(`${API_BASE_URL}/${path.replace(/^\/+/, '')}`)
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== '') url.searchParams.set(key, String(value))
  }
  return url.toString()
}

/** The first human-readable message in an error body. */
function detailFrom(body: ErrorBody | null, fallback: string): string {
  if (!body) return fallback
  if (typeof body.detail === 'string' && body.detail) return body.detail
  // Field errors: {"title": ["This field is required."], "code": "invalid"}.
  for (const [field, value] of Object.entries(body)) {
    if (field === 'code') continue
    if (Array.isArray(value) && typeof value[0] === 'string') {
      return field === 'non_field_errors' ? value[0] : `${field}: ${value[0]}`
    }
  }
  return fallback
}

async function send(path: string, options: RequestOptions, token: string | null): Promise<Response> {
  const headers: Record<string, string> = { Accept: 'application/json', ...options.headers }
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  if (token) headers.Authorization = `Bearer ${token}`
  const body = options.body === undefined ? undefined : JSON.stringify(options.body)
  const keepalive =
    options.keepalive === true && body !== undefined && new Blob([body]).size <= KEEPALIVE_MAX_BYTES
  try {
    const response = await fetch(buildUrl(path, options.query), {
      method: options.method ?? 'GET',
      headers,
      body,
      signal: options.signal,
      keepalive,
    })
    const minVersion = response.headers?.get('X-Client-Min-Version')
    if (minVersion) onMinVersion?.(minVersion)
    return response
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError(0, 'network_error', 'Could not reach the server. Check your connection.', null)
  }
}

async function parse<T>(response: Response): Promise<T> {
  if (response.status === 204) return undefined as T
  const text = await response.text()
  let body: unknown = null
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      body = null
    }
  }
  if (!response.ok) {
    const errorBody = body && typeof body === 'object' ? (body as ErrorBody) : null
    throw new ApiError(
      response.status,
      typeof errorBody?.code === 'string' ? errorBody.code : `http_${response.status}`,
      detailFrom(errorBody, `Request failed (${response.status}).`),
      errorBody,
    )
  }
  return body as T
}

// ---------------------------------------------------------------- refresh --

let refreshing: Promise<boolean> | null = null

/**
 * Get a fresh access token. Resolves true if one is now stored.
 *
 * Single-flight inside the tab (one shared promise) and, where the browser
 * has the Web Locks API, across tabs too: every tab shares one rotating
 * refresh token, so two tabs refreshing at once would have the second
 * refused (its token was just rotated away) and sign that tab out. Inside
 * the lock, a token that changed since the failed request means another tab
 * already refreshed, and that tab's pair is used instead.
 */
function refreshAccess(staleAccess: string | null): Promise<boolean> {
  if (!refreshing) {
    const run = () => doRefresh(staleAccess)
    const locked =
      typeof navigator !== 'undefined' && navigator.locks
        ? navigator.locks.request('superNotes.refresh', run)
        : run()
    refreshing = locked.finally(() => {
      refreshing = null
    })
  }
  return refreshing
}

async function doRefresh(staleAccess: string | null): Promise<boolean> {
  const current = getAccess()
  if (current && current !== staleAccess) return true
  const refresh = getRefresh()
  if (!refresh) return false
  try {
    const pair = await parse<RefreshResponse>(
      await send('auth/refresh/', { method: 'POST', body: { refresh } }, null),
    )
    // Rotation: the old refresh token is now blacklisted, always keep the new one.
    setTokens(pair)
    return true
  } catch (error) {
    // The schema refuses a dead refresh token with a 401 (a 400 is tolerated
    // too). Anything else (network, 429, 5xx) is not a verdict on the
    // session: keep the tokens and let the caller see the error.
    if (error instanceof ApiError && (error.status === 400 || error.status === 401)) return false
    throw error
  }
}

function sessionEnded(): void {
  clearTokens()
  onAuthLost?.()
}

/** Send a request with the token, refreshing once on a 401; the raw response (a 401 that stays ends the session). */
async function authedSend(path: string, options: RequestOptions): Promise<Response> {
  const auth = options.auth ?? true
  const token = auth ? getAccess() : null
  let response = await send(path, options, token)

  if (response.status === 401 && auth) {
    if (!(await refreshAccess(token))) {
      sessionEnded()
      return response
    }
    response = await send(path, options, getAccess())
    if (response.status === 401) {
      // A brand new token refused: the account is gone or signed out.
      sessionEnded()
    }
  }
  return response
}

/** Make a request and return its parsed JSON body (undefined for a 204). */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return parse<T>(await authedSend(path, options))
}

/**
 * Fetch a binary body (a file download) with the bearer token in the header, never in a URL.
 * An error comes back as the usual ApiError.
 */
export async function requestBlob(path: string, options: RequestOptions = {}): Promise<Blob> {
  const response = await authedSend(path, { ...options, headers: { ...options.headers, Accept: '*/*' } })
  if (!response.ok) return parse<Blob>(response)
  return response.blob()
}

export interface UploadOptions {
  /** Called with 0..1 as the request body goes out. */
  onProgress?: (fraction: number) => void
  signal?: AbortSignal
}

/** One XHR round: resolves with status and body text; status 0 means the network failed. */
function xhrPost(
  path: string,
  form: FormData,
  token: string | null,
  options: UploadOptions,
): Promise<{ status: number; text: string }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', buildUrl(path))
    xhr.setRequestHeader('Accept', 'application/json')
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) options.onProgress?.(event.loaded / event.total)
    }
    xhr.onload = () => resolve({ status: xhr.status, text: xhr.responseText })
    xhr.onerror = () => resolve({ status: 0, text: '' })
    xhr.ontimeout = () => resolve({ status: 0, text: '' })
    xhr.onabort = () => reject(new DOMException('Aborted', 'AbortError'))
    if (options.signal) {
      if (options.signal.aborted) {
        reject(new DOMException('Aborted', 'AbortError'))
        return
      }
      options.signal.addEventListener('abort', () => xhr.abort(), { once: true })
    }
    // No Content-Type: the browser adds the multipart boundary itself.
    xhr.send(form)
  })
}

function parseText<T>(status: number, text: string): Promise<T> {
  if (status === 0) throw new ApiError(0, 'network_error', 'Could not reach the server. Check your connection.', null)
  return parse<T>(new Response(status === 204 ? null : text, { status }))
}

/**
 * POST a multipart form with upload progress (fetch cannot report it, so this is XHR). The same
 * token and refresh rules as `request`. Resolves to the status as well, since 200 and 201 differ.
 */
export async function upload<T>(
  path: string,
  form: FormData,
  options: UploadOptions = {},
): Promise<{ status: number; body: T }> {
  const token = getAccess()
  let result = await xhrPost(path, form, token, options)
  if (result.status === 401) {
    if (!(await refreshAccess(token))) {
      sessionEnded()
    } else {
      result = await xhrPost(path, form, getAccess(), options)
      if (result.status === 401) sessionEnded()
    }
  }
  const body = await parseText<T>(result.status, result.text)
  return { status: result.status, body }
}

/** The `cursor` query parameter from a DRF `next`/`previous` URL. */
export function cursorFrom(pageUrl: string | null | undefined): string | null {
  if (!pageUrl) return null
  try {
    return new URL(pageUrl).searchParams.get('cursor')
  } catch {
    return null
  }
}

/** A short message for any thrown value, for showing to people. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.detail
  return 'Something went wrong. Please try again.'
}
