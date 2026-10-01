import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import type { AskQuery, ConversationDetail } from '../api/types'

const api = vi.hoisted(() => ({
  create: vi.fn(),
  get: vi.fn(),
  ask: vi.fn(),
  askGet: vi.fn(),
  list: vi.fn(),
}))
vi.mock('../api/endpoints', () => ({
  conversationsApi: { create: api.create, get: api.get, ask: api.ask, list: api.list },
  askApi: { get: api.askGet },
}))

vi.mock('./auth', () => ({
  useAuthStore: () => ({ user: null, loadMe: vi.fn().mockResolvedValue(null), setAskUsage: vi.fn() }),
}))

import { useChatStore } from './chat'

const turn = (id: number, status: AskQuery['status'], position = id, extra: Partial<AskQuery> = {}): AskQuery => ({
  id,
  conversation: 7,
  position,
  question: `q${id}`,
  status,
  answer: status === 'done' ? `a${id}` : '',
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
const problem = (status: number, code: string, extra: Record<string, unknown> = {}) =>
  new ApiError(status, code, `detail ${code}`, { detail: `detail ${code}`, code, ...extra })

beforeEach(() => {
  vi.useFakeTimers()
  setActivePinia(createPinia())
  Object.values(api).forEach((f) => f.mockReset())
})
afterEach(() => vi.useRealTimers())

describe('chat store', () => {
  it('opens a thread and polls an unfinished turn until it is answered', async () => {
    api.get.mockResolvedValue(detail([turn(1, 'done'), turn(2, 'pending')]))
    api.askGet.mockResolvedValueOnce(turn(2, 'running')).mockResolvedValueOnce(turn(2, 'done'))
    const chat = useChatStore()
    await chat.open(7)
    expect(chat.phase).toBe('answering')
    await vi.advanceTimersByTimeAsync(800)
    expect(chat.turns[1]!.status).toBe('running')
    await vi.advanceTimersByTimeAsync(1200)
    expect(chat.turns[1]!.status).toBe('done')
    expect(chat.phase).toBe('ready')
  })

  it('leaving and re-entering a thread quickly still polls the pending turn', async () => {
    api.get.mockResolvedValue(detail([turn(1, 'done'), turn(2, 'pending')]))
    api.askGet.mockResolvedValue(turn(2, 'done'))
    const chat = useChatStore()
    await chat.open(7)
    await chat.open(null)
    await chat.open(7)
    await vi.advanceTimersByTimeAsync(5000)
    expect(chat.turns[1]!.status).toBe('done')
  })

  it('a first question creates the conversation, adopts it and returns its id', async () => {
    api.create.mockResolvedValue(detail([turn(1, 'pending')]))
    const chat = useChatStore()
    await chat.open(null)
    const id = await chat.send('  What is due?  ')
    expect(id).toBe(7)
    expect(api.create).toHaveBeenCalledWith('What is due?', expect.any(String))
    expect(chat.currentId).toBe(7)
    expect(chat.phase).toBe('answering')
    // The view moving to /chat/7 must not reload the thread it was just given.
    await chat.open(7)
    expect(api.get).not.toHaveBeenCalled()
    expect(chat.turns).toHaveLength(1)
  })

  it('a follow-up posts to the conversation and shows the pending turn', async () => {
    api.get.mockResolvedValue(detail([turn(1, 'done')]))
    api.ask.mockResolvedValue(turn(2, 'pending'))
    const chat = useChatStore()
    await chat.open(7)
    await chat.send('When is it due?')
    expect(api.ask).toHaveBeenCalledWith(7, 'When is it due?', expect.any(String))
    expect(chat.turns.map((t) => t.id)).toEqual([1, 2])
    expect(chat.phase).toBe('answering')
  })

  it('on a 409 waits for the running turn, then sends with the same key', async () => {
    api.get.mockResolvedValue(detail([turn(1, 'running')]))
    api.ask.mockRejectedValueOnce(problem(409, 'turn_in_progress', { turn: 1 })).mockResolvedValueOnce(turn(2, 'pending'))
    api.askGet.mockResolvedValueOnce(turn(1, 'running')).mockResolvedValue(turn(1, 'done'))
    const chat = useChatStore()
    await chat.open(7)
    const sent = chat.send('Next?')
    await vi.advanceTimersByTimeAsync(10)
    expect(chat.phase).toBe('waiting')
    expect(api.ask).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(5000)
    await sent
    expect(api.ask).toHaveBeenCalledTimes(2)
    expect(api.ask.mock.calls[1]![2]).toBe(api.ask.mock.calls[0]![2])
    expect(chat.sendError).toBeNull()
    expect(chat.turns.map((t) => [t.id, t.status])).toEqual([
      [1, 'done'],
      [2, 'pending'],
    ])
  })

  it('shows the quota message from the chat_turns limit on a 429', async () => {
    api.get.mockResolvedValue(detail([turn(1, 'done')]))
    api.ask.mockRejectedValue(problem(429, 'quota_exceeded', { used: 30, limit: 30, resets_at: '2026-11-01T00:00:00+05:30' }))
    const chat = useChatStore()
    await chat.open(7)
    await chat.send('More?')
    expect(chat.sendError?.kind).toBe('quota')
    expect(chat.sendError?.message).toContain('all 30 chat turns')
    expect(chat.phase).toBe('ready')
  })

  it('shows the paused message on a 503 system limit', async () => {
    api.get.mockResolvedValue(detail([turn(1, 'done')]))
    api.ask.mockRejectedValue(problem(503, 'system_limit_reached'))
    const chat = useChatStore()
    await chat.open(7)
    await chat.send('More?')
    expect(chat.sendError?.kind).toBe('paused')
  })

  it('retries a dropped connection with the same key, and a new question with a new one', async () => {
    api.get.mockResolvedValue(detail([turn(1, 'done')]))
    api.ask
      .mockRejectedValueOnce(new ApiError(0, 'network_error', 'offline', null))
      .mockResolvedValueOnce(turn(2, 'pending'))
    const chat = useChatStore()
    await chat.open(7)
    await chat.send('Same?')
    expect(chat.sendError?.kind).toBe('network')
    await chat.send('Same?')
    expect(api.ask.mock.calls[1]![2]).toBe(api.ask.mock.calls[0]![2])
    expect(chat.sendError).toBeNull()
  })

  it('retrying a failed turn asks its question again as a new turn', async () => {
    api.get.mockResolvedValue(detail([turn(1, 'done'), turn(2, 'failed', 2, { error: 'The model is down.' })]))
    api.ask.mockResolvedValue(turn(3, 'pending'))
    const chat = useChatStore()
    await chat.open(7)
    expect(chat.phase).toBe('failed')
    expect(chat.failedLast?.error).toBe('The model is down.')
    await chat.retry(chat.failedLast!)
    expect(api.ask).toHaveBeenCalledWith(7, 'q2', expect.any(String))
    expect(chat.turns.map((t) => t.id)).toEqual([1, 2, 3])
    expect(chat.phase).toBe('answering')
  })

  it('marks a conversation that is gone', async () => {
    api.get.mockRejectedValue(problem(404, 'not_found'))
    const chat = useChatStore()
    await chat.open(9)
    expect(chat.missing).toBe(true)
  })
})
