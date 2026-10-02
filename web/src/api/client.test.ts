import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, request, requestBlob, setAuthLostHandler, upload } from './client'
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

  it('requestBlob sends the token in the header, not the URL, and refreshes on a 401', async () => {
    const urls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
        const u = String(url)
        urls.push(u)
        if (u.endsWith('/auth/refresh/')) return json(200, { access: 'access-new', refresh: 'refresh-new' })
        const auth = (init?.headers as Record<string, string>).Authorization
        return auth === 'Bearer access-new' ? new Response('PDFBYTES', { status: 200 }) : json(401, { detail: 'expired' })
      }),
    )
    const blob = await requestBlob('attachments/4/file/')
    expect(await blob.text()).toBe('PDFBYTES')
    expect(urls.every((u) => !u.includes('access') && !u.includes('token'))).toBe(true)
  })

  it('requestBlob turns an error body into an ApiError', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json(404, { detail: 'No such attachment.', code: 'not_found' })))
    await expect(requestBlob('attachments/9/file/')).rejects.toMatchObject({ status: 404, code: 'not_found' })
  })

  it('upload reports progress, sends the bearer header and resolves status and body', async () => {
    const sent: { headers: Record<string, string>; body: unknown }[] = []
    class FakeXhr {
      headers: Record<string, string> = {}
      upload: { onprogress?: (e: unknown) => void } = {}
      status = 0
      responseText = ''
      onload?: () => void
      onerror?: () => void
      ontimeout?: () => void
      onabort?: () => void
      open() {}
      setRequestHeader(k: string, v: string) {
        this.headers[k] = v
      }
      abort() {}
      send(body: unknown) {
        sent.push({ headers: this.headers, body })
        this.upload.onprogress?.({ lengthComputable: true, loaded: 5, total: 10 })
        this.status = 201
        this.responseText = JSON.stringify({ id: 1 })
        this.onload?.()
      }
    }
    vi.stubGlobal('XMLHttpRequest', FakeXhr)
    const form = new FormData()
    form.append('file', new Blob(['x']), 'a.pdf')
    const fractions: number[] = []
    setTokens({ access: 'access-new', refresh: 'r' })
    const out = await upload<{ id: number }>('notes/3/attachments/', form, { onProgress: (f) => fractions.push(f) })
    expect(out).toEqual({ status: 201, body: { id: 1 } })
    expect(fractions).toEqual([0.5])
    expect(sent[0]!.headers.Authorization).toBe('Bearer access-new')
    expect(sent[0]!.headers['Content-Type']).toBeUndefined()
    expect(sent[0]!.body).toBe(form)
  })

  it('upload maps a server refusal to an ApiError and a dead network to status 0', async () => {
    let mode: 'refuse' | 'down' = 'refuse'
    class FakeXhr {
      upload = {}
      status = 0
      responseText = ''
      onload?: () => void
      onerror?: () => void
      open() {}
      setRequestHeader() {}
      abort() {}
      send() {
        if (mode === 'down') return void this.onerror?.()
        this.status = 413
        this.responseText = JSON.stringify({ detail: 'Too big.', code: 'too_large' })
        this.onload?.()
      }
    }
    vi.stubGlobal('XMLHttpRequest', FakeXhr)
    setTokens({ access: 'access-new', refresh: 'r' })
    await expect(upload('notes/3/attachments/', new FormData())).rejects.toMatchObject({ status: 413, code: 'too_large' })
    mode = 'down'
    await expect(upload('notes/3/attachments/', new FormData())).rejects.toMatchObject({ status: 0 })
  })
})
