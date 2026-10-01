import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Notice } from '../api/types'

const open = vi.fn<(version: string, reason: string) => Promise<{ notices: Notice[]; server_time: string }>>()
const reload = vi.fn()
const seen = vi.fn<() => Promise<void>>()
vi.mock('../api/endpoints', () => ({
  sessionApi: { open: (v: string, r: string) => open(v, r) },
  authApi: { memoryNoticeSeen: () => seen(), updateMe: vi.fn() },
  appApi: { version: vi.fn() },
  notesApi: { changes: vi.fn() },
}))

import { useAppVersionStore } from './appVersion'
import { useLifecycleStore } from './lifecycle'

beforeEach(() => {
  setActivePinia(createPinia())
  open.mockReset()
  seen.mockReset()
  // The stores touch a few browser globals; stand in for them (Node, no jsdom).
  reload.mockReset()
  vi.stubGlobal('window', {
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    localStorage: undefined,
    sessionStorage: { getItem: () => null, setItem: () => undefined },
    location: { reload },
  })
  vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => undefined, removeItem: () => undefined })
})

afterEach(() => vi.unstubAllGlobals())

function activate(store: ReturnType<typeof useLifecycleStore>) {
  store.begin()
}

describe('lifecycle store', () => {
  it('shows the memory notice the server returns', async () => {
    open.mockResolvedValue({ notices: [{ kind: 'memory', style: 'prominent', state: 'on' }], server_time: '' })
    const store = useLifecycleStore()
    activate(store)
    await store.open('launch')
    expect(open).toHaveBeenCalledWith(expect.any(String), 'launch')
    expect(store.memoryNotice).toEqual({ style: 'prominent', state: 'on' })
  })

  it('hands an update notice to the version store', async () => {
    open.mockResolvedValue({ notices: [{ kind: 'update', required: true }], server_time: '' })
    const store = useLifecycleStore()
    activate(store)
    await store.open('resume')
    const version = useAppVersionStore()
    expect(version.required).toBe(true)
    expect(version.showBar).toBe(true)
    await Promise.resolve()
    expect(reload).toHaveBeenCalled() // nothing unsaved: the update applies at once
  })

  it('dismissing clears the banner and tells the server it was seen', async () => {
    open.mockResolvedValue({ notices: [{ kind: 'memory', style: 'subtle', state: 'off' }], server_time: '' })
    seen.mockResolvedValue(undefined)
    const store = useLifecycleStore()
    activate(store)
    await store.open('launch')
    await store.markMemoryNoticeSeen()
    expect(store.memoryNotice).toBeNull()
    expect(seen).toHaveBeenCalledTimes(1)
  })

  it('a failed open is quiet; a failed "seen" still clears the banner', async () => {
    open.mockRejectedValue(new Error('offline'))
    seen.mockRejectedValue(new Error('offline'))
    const store = useLifecycleStore()
    activate(store)
    await expect(store.open('launch')).resolves.toBeUndefined()
    store.memoryNotice = { style: 'subtle', state: 'on' }
    await store.markMemoryNoticeSeen()
    expect(store.memoryNotice).toBeNull()
  })

  it('does nothing after sign-out', async () => {
    const store = useLifecycleStore()
    activate(store)
    store.end()
    await store.open('resume')
    expect(open).toHaveBeenCalledTimes(1) // only begin()'s own launch
  })
})
