import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, request, setAuthLostHandler } from './client'
import { getAccess, getRefresh, setTokens } from './tokens'

function memoryStorage(): Storage {
  const data = new Map<string, string>()
  return {
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, String(v)),
    removeItem: (k) => void data.delete(k),
    clear: () => data.clear(),
    key: (i) => [...data.keys()][i] ?? null,
    get length() {
      return data.size
    },
  }
}

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

/** A fake server: `data/` needs the "new" access token; auth/refresh/ is scripted. */
function fakeServer(refresh: () => Promise<Response> | Response) {
  const calls = { refresh: 0, data: 0 }
  const fetchMock = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const u = String(url)
    if (u.endsWith('/auth/refresh/')) {
      calls.refresh++
      return refresh()
    }
    calls.data++
    const auth = (init?.headers as Record<string, string>).Authorization
    return auth === 'Bearer access-new' ? json(200, { ok: true }) : json(401, { detail: 'expired' })
  })
  vi.stubGlobal('fetch', fetchMock)
  return calls
}

beforeEach(() => {
  vi.stubGlobal('localStorage', memoryStorage())
  setTokens({ access: 'access-old', refresh: 'refresh-old' })
})
afterEach(() => {
  vi.unstubAllGlobals()
  setAuthLostHandler(() => undefined)
})

describe('single-flight refresh', () => {
  it('ten concurrent 401s cause one refresh and every request is retried', async () => {
    const calls = fakeServer(() => json(200, { access: 'access-new', refresh: 'refresh-new' }))
    const results = await Promise.all(Array.from({ length: 10 }, () => request<{ ok: boolean }>('data/')))
    expect(results.every((r) => r.ok)).toBe(true)
    expect(calls.refresh).toBe(1)
    expect(calls.data).toBe(20) // 10 failed + 10 retried
    expect(getAccess()).toBe('access-new')
    expect(getRefresh()).toBe('refresh-new') // the rotated token is kept
  })

  it('a refused refresh signs out once and clears the tokens', async () => {
    const lost = vi.fn()
    setAuthLostHandler(lost)
    const calls = fakeServer(() => json(401, { detail: 'dead' }))
    const outcomes = await Promise.allSettled([request('data/'), request('data/'), request('data/')])
    expect(outcomes.every((o) => o.status === 'rejected')).toBe(true)
    expect(calls.refresh).toBe(1)
    expect(getAccess()).toBeNull()
    expect(getRefresh()).toBeNull()
    expect(lost).toHaveBeenCalled()
  })

  it('a network failure during refresh keeps the tokens and does not sign out', async () => {
    const lost = vi.fn()
    setAuthLostHandler(lost)
    fakeServer(() => {
      throw new TypeError('offline')
    })
    await expect(request('data/')).rejects.toMatchObject({ status: 0 })
    expect(getRefresh()).toBe('refresh-old')
    expect(lost).not.toHaveBeenCalled()
  })

  it('a refresh finished by "another tab" (token changed) is reused, not repeated', async () => {
    const calls = fakeServer(() => json(200, { access: 'access-new', refresh: 'refresh-new' }))
    // The first call fails with the old token; meanwhile storage already holds a new one.
    const first = request('data/')
    setTokens({ access: 'access-new', refresh: 'refresh-other-tab' })
    await first
    expect(calls.refresh).toBe(0)
  })

  it('does not refresh when auth is off', async () => {
    const calls = fakeServer(() => json(200, { access: 'access-new', refresh: 'x' }))
    await expect(request('data/', { auth: false })).rejects.toBeInstanceOf(ApiError)
    expect(calls.refresh).toBe(0)
  })
})
