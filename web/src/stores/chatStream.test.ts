import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AskQuery, ConversationDetail } from '../api/types'

const api = vi.hoisted(() => ({ get: vi.fn(), askGet: vi.fn(), open: vi.fn() }))
vi.mock('../api/endpoints', () => ({
  conversationsApi: { get: api.get },
  askApi: { get: api.askGet },
}))
vi.mock('../api/stream', () => ({ openAskStream: api.open }))
vi.mock('./auth', () => ({
  useAuthStore: () => ({ user: null, loadMe: vi.fn().mockResolvedValue(null), setAskUsage: vi.fn() }),
}))

import { useChatStore } from './chat'

const turn = (id: number, status: AskQuery['status'], extra: Partial<AskQuery> = {}): AskQuery => ({
  id,
  conversation: 7,
  position: id,
  question: `q${id}`,
  status,
  answer: '',
  citations: [],
  error: '',
  created_at: '2026-10-01T10:00:00Z',
  completed_at: null,
  ...extra,
})
const detail = (turns: AskQuery[]): ConversationDetail => ({
  id: 7,
  title: 'q1',
  created_at: '2026-10-01T10:00:00Z',
  updated_at: '2026-10-01T10:00:00Z',
  turns,
})
const enc = new TextEncoder()
const sse = (type: string, data: object = {}) => enc.encode(`event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`)

/** A stream the test feeds by hand. */
function controlled(signal: AbortSignal) {
  let controller!: ReadableStreamDefaultController<Uint8Array>
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c
      signal.addEventListener('abort', () => {
        try {
          c.error(new DOMException('aborted', 'AbortError'))
        } catch {
          // already closed
        }
      })
    },
  })
  return { stream, push: (b: Uint8Array) => controller.enqueue(b) }
}

beforeEach(() => {
  vi.useFakeTimers()
  setActivePinia(createPinia())
  Object.values(api).forEach((f) => f.mockReset())
})
afterEach(() => vi.useRealTimers())

describe('chat store streaming', () => {
  it('shows text as it arrives, then the finished turn', async () => {
    api.get.mockResolvedValue(detail([turn(2, 'running')]))
    let feed!: ReturnType<typeof controlled>
    api.open.mockImplementation(async (_id: number, signal: AbortSignal) => (feed = controlled(signal)).stream)
    const chat = useChatStore()
    await chat.open(7)
    await vi.advanceTimersByTimeAsync(0)
    feed.push(sse('snapshot', { text: 'Hel', offset: 3 }))
    await vi.advanceTimersByTimeAsync(0)
    expect(chat.streaming[2]).toBe('Hel')
    feed.push(sse('delta', { offset: 3, text: 'lo [1]' }))
    await vi.advanceTimersByTimeAsync(0)
    expect(chat.streaming[2]).toBe('Hello [1]')
    const done = turn(2, 'done', { answer: 'Hello [1]' })
    feed.push(sse('done', { ask: done }))
    await vi.advanceTimersByTimeAsync(0)
    expect(chat.turns[0]!.status).toBe('done')
    expect(chat.streaming[2]).toBeUndefined()
    expect(api.askGet).not.toHaveBeenCalled()
  })

  it('falls back to polling when the stream cannot be opened (404, 429, error)', async () => {
    api.get.mockResolvedValue(detail([turn(2, 'pending')]))
    api.open.mockResolvedValue(null)
    api.askGet.mockResolvedValue(turn(2, 'done', { answer: 'a' }))
    const chat = useChatStore()
    await chat.open(7)
    await vi.advanceTimersByTimeAsync(1000)
    expect(chat.turns[0]!.status).toBe('done')
  })

  it('falls back to polling when the stream says unavailable or breaks off', async () => {
    api.get.mockResolvedValue(detail([turn(2, 'pending')]))
    api.open.mockImplementation(async (_id: number, signal: AbortSignal) => {
      const f = controlled(signal)
      f.push(sse('snapshot', { text: 'x', offset: 1 }))
      f.push(sse('unavailable'))
      return f.stream
    })
    api.askGet.mockResolvedValue(turn(2, 'done', { answer: 'a' }))
    const chat = useChatStore()
    await chat.open(7)
    await vi.advanceTimersByTimeAsync(1000)
    expect(chat.turns[0]!.status).toBe('done')
    expect(chat.streaming[2]).toBeUndefined()
  })

  it('leaving the thread aborts the stream', async () => {
    api.get.mockResolvedValue(detail([turn(2, 'running')]))
    let signal!: AbortSignal
    api.open.mockImplementation(async (_id: number, s: AbortSignal) => {
      signal = s
      return controlled(s).stream
    })
    const chat = useChatStore()
    await chat.open(7)
    await vi.advanceTimersByTimeAsync(0)
    expect(signal.aborted).toBe(false)
    chat.close()
    expect(signal.aborted).toBe(true)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(api.askGet).not.toHaveBeenCalled()
  })
})
