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

/** Make a request and return its parsed JSON body (undefined for a 204). */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const auth = options.auth ?? true
  const token = auth ? getAccess() : null
  let response = await send(path, options, token)

  if (response.status === 401 && auth) {
    if (!(await refreshAccess(token))) {
      sessionEnded()
      return parse<T>(response)
    }
    response = await send(path, options, getAccess())
    if (response.status === 401) {
      // A brand new token refused: the account is gone or signed out.
      sessionEnded()
    }
  }
  return parse<T>(response)
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
