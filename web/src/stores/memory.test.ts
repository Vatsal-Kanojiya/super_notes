import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { CursorPage, UserFact } from '../api/types'

const list = vi.fn<(cursor?: string) => Promise<CursorPage<UserFact>>>()
const forget = vi.fn<(id: number) => Promise<void>>()
const forgetAll = vi.fn<() => Promise<void>>()
vi.mock('../api/endpoints', () => ({
  memoryApi: {
    list: (c?: string) => list(c),
    forget: (id: number) => forget(id),
    forgetAll: () => forgetAll(),
  },
}))

import { useMemoryStore } from './memory'

const fact = (id: number, kind: 'static' | 'dynamic' = 'static'): UserFact => ({
  id,
  text: `fact ${id}`,
  kind,
  valid_until: kind === 'dynamic' ? '2026-12-01T00:00:00Z' : null,
  created_at: '2026-10-01T00:00:00Z',
})

beforeEach(() => {
  setActivePinia(createPinia())
  list.mockReset()
  forget.mockReset()
  forgetAll.mockReset()
})

describe('memory store', () => {
  it('lists facts and marks loaded', async () => {
    list.mockResolvedValueOnce({ next: null, results: [fact(2), fact(1, 'dynamic')] })
    const store = useMemoryStore()
    await store.load()
    expect(store.facts.map((f) => f.id)).toEqual([2, 1])
    expect(store.loaded).toBe(true)
    expect(store.cursor).toBeNull()
  })

  it('follows the cursor without duplicating facts', async () => {
    list
      .mockResolvedValueOnce({ next: 'http://x/api/v1/memory/facts/?cursor=abc', results: [fact(3), fact(2)] })
      .mockResolvedValueOnce({ next: null, results: [fact(2), fact(1)] })
    const store = useMemoryStore()
    await store.load()
    expect(store.cursor).toBe('abc')
    await store.loadMore()
    expect(list).toHaveBeenLastCalledWith('abc')
    expect(store.facts.map((f) => f.id)).toEqual([3, 2, 1])
    expect(store.cursor).toBeNull()
  })

  it('delete calls the API and reloads (the server may drop replaced facts too)', async () => {
    list.mockResolvedValueOnce({ next: null, results: [fact(2), fact(1)] })
    const store = useMemoryStore()
    await store.load()
    forget.mockResolvedValueOnce()
    list.mockResolvedValueOnce({ next: null, results: [] })
    await store.forget(2)
    expect(forget).toHaveBeenCalledWith(2)
    expect(store.facts).toEqual([])
  })

  it('a failed delete leaves the list alone', async () => {
    list.mockResolvedValueOnce({ next: null, results: [fact(1)] })
    const store = useMemoryStore()
    await store.load()
    forget.mockRejectedValueOnce(new Error('nope'))
    await expect(store.forget(1)).rejects.toThrow('nope')
    expect(store.facts).toHaveLength(1)
  })

  it('forget-all clears the list', async () => {
    list.mockResolvedValueOnce({ next: 'http://x/?cursor=z', results: [fact(2), fact(1)] })
    const store = useMemoryStore()
    await store.load()
    forgetAll.mockResolvedValueOnce()
    await store.forgetAll()
    expect(forgetAll).toHaveBeenCalled()
    expect(store.facts).toEqual([])
    expect(store.cursor).toBeNull()
  })
})
