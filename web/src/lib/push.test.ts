import { describe, expect, it, vi } from 'vitest'
import source from '../../public/sw.js?raw'
import { pushSupported, toSubscriptionRequest, urlBase64ToBytes } from './push'

describe('urlBase64ToBytes', () => {
  it('decodes base64url with no padding', () => {
    // "hello?>" -> aGVsbG8_Pg in base64url ('_' for '/')
    expect(Array.from(urlBase64ToBytes('aGVsbG8_Pg'))).toEqual([104, 101, 108, 108, 111, 63, 62])
  })
  it('decodes a 65-byte VAPID key (87 characters)', () => {
    const key = 'B' + 'A'.repeat(86)
    expect(urlBase64ToBytes(key)).toHaveLength(65)
  })
})

describe('toSubscriptionRequest', () => {
  it('flattens the keys', () => {
    expect(toSubscriptionRequest({ endpoint: 'https://e', keys: { p256dh: 'p', auth: 'a' } })).toEqual({
      endpoint: 'https://e',
      p256dh: 'p',
      auth: 'a',
    })
  })
  it('is null when a part is missing', () => {
    expect(toSubscriptionRequest({ endpoint: 'https://e', keys: { p256dh: 'p' } })).toBeNull()
    expect(toSubscriptionRequest({ keys: { p256dh: 'p', auth: 'a' } })).toBeNull()
    expect(toSubscriptionRequest({})).toBeNull()
  })
})

describe('pushSupported', () => {
  it('needs all three browser features', () => {
    expect(pushSupported({ serviceWorker: {}, PushManager: {}, Notification: {} })).toBe(true)
    expect(pushSupported({ serviceWorker: {}, PushManager: {} })).toBe(false)
    expect(pushSupported({})).toBe(false)
  })
})

// The worker is plain JS in public/, served as is; run it against a fake scope.
describe('public/sw.js', () => {
  function load() {
    const handlers: Record<string, (event: unknown) => void> = {}
    const shown: { title: string; options: Record<string, unknown> }[] = []
    const postMessage = vi.fn()
    const focus = vi.fn(async () => undefined)
    const openWindow = vi.fn(async () => undefined)
    const windows: unknown[] = []
    const self = {
      addEventListener: (name: string, fn: (event: unknown) => void) => (handlers[name] = fn),
      skipWaiting: vi.fn(),
      clients: {
        claim: vi.fn(async () => undefined),
        matchAll: vi.fn(async () => windows),
        openWindow,
      },
      registration: {
        showNotification: vi.fn(async (title: string, options: Record<string, unknown>) => {
          shown.push({ title, options })
        }),
      },
      location: { origin: 'https://app.example' },
    }
    new Function('self', source)(self)
    return { self, handlers, shown, windows, postMessage, focus, openWindow }
  }

  const waitable = () => {
    const waits: Promise<unknown>[] = []
    return { waits, waitUntil: (p: Promise<unknown>) => waits.push(p) }
  }

  it('has no fetch handler: it can never serve cached app code (D89)', () => {
    expect(load().handlers.fetch).toBeUndefined()
    expect(source).not.toMatch(/caches\./)
  })

  it('takes over at once', async () => {
    const { self, handlers } = load()
    handlers.install!({})
    expect(self.skipWaiting).toHaveBeenCalled()
    const ev = waitable()
    handlers.activate!(ev)
    await Promise.all(ev.waits)
    expect(self.clients.claim).toHaveBeenCalled()
  })

  it('shows a push as a notification with the note title, one per reminder', async () => {
    const { handlers, shown } = load()
    const ev = { ...waitable(), data: { json: () => ({ type: 'reminder', reminder_id: 4, note_id: 9, title: 'Passport' }) } }
    handlers.push!(ev)
    await Promise.all(ev.waits)
    expect(shown[0]!.title).toBe('Passport')
    expect(shown[0]!.options).toMatchObject({ tag: 'reminder-4', data: { path: '/notes/9' } })
  })

  it('survives an empty or malformed payload', async () => {
    const { handlers, shown } = load()
    for (const data of [null, { json: () => { throw new Error('bad') } }, { json: () => 'text' }]) {
      const ev = { ...waitable(), data }
      handlers.push!(ev)
      await Promise.all(ev.waits)
    }
    expect(shown).toHaveLength(3)
    expect(shown.every((s) => (s.options.data as { path: string }).path === '/calendar')).toBe(true)
  })

  it('a click opens a window on the note when none is open', async () => {
    const { handlers, openWindow } = load()
    const close = vi.fn()
    const ev = { ...waitable(), notification: { close, data: { path: '/notes/9' } } }
    handlers.notificationclick!(ev)
    await Promise.all(ev.waits)
    expect(close).toHaveBeenCalled()
    expect(openWindow).toHaveBeenCalledWith('https://app.example/notes/9')
  })

  it('a click goes through an open tab (message + focus), not a reload', async () => {
    const { handlers, windows, openWindow, postMessage, focus } = load()
    windows.push({ url: 'https://app.example/notes', postMessage, focus })
    const ev = { ...waitable(), notification: { close: vi.fn(), data: { path: '/notes/9' } } }
    handlers.notificationclick!(ev)
    await Promise.all(ev.waits)
    expect(postMessage).toHaveBeenCalledWith({ type: 'open-path', path: '/notes/9' })
    expect(focus).toHaveBeenCalled()
    expect(openWindow).not.toHaveBeenCalled()
  })

  it('a click with tampered data cannot open anything but a note or the calendar', async () => {
    const { handlers, openWindow } = load()
    const ev = { ...waitable(), notification: { close: vi.fn(), data: { path: 'https://evil.example/' } } }
    handlers.notificationclick!(ev)
    await Promise.all(ev.waits)
    expect(openWindow).toHaveBeenCalledWith('https://app.example/calendar')
  })
})
