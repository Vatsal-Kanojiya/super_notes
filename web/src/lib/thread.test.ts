import { describe, expect, it } from 'vitest'
import type { AskQuery, Conversation } from '../api/types'
import {
  canCompose,
  conversationLabel,
  describeTurnFailure,
  failedLastTurn,
  mergeTurn,
  mergeTurns,
  overLimit,
  pendingTurn,
  sortConversations,
  threadPhase,
  upsertConversation,
  usageLine,
} from './thread'

const turn = (id: number, position: number | null, status: AskQuery['status'] = 'done', extra: Partial<AskQuery> = {}): AskQuery => ({
  id,
  conversation: 1,
  position,
  question: `q${id}`,
  status,
  answer: status === 'done' ? `a${id}` : '',
  citations: [],
  error: '',
  created_at: '2026-10-01T10:00:00Z',
  completed_at: status === 'done' || status === 'failed' ? '2026-10-01T10:00:05Z' : null,
  ...extra,
})

const conv = (id: number, updated_at: string, title = `c${id}`): Conversation => ({
  id,
  title,
  created_at: '2026-10-01T09:00:00Z',
  updated_at,
})

describe('mergeTurn', () => {
  it('adds a new turn in position order', () => {
    const merged = mergeTurn([turn(1, 1), turn(3, 3)], turn(2, 2))
    expect(merged.map((t) => t.id)).toEqual([1, 2, 3])
  })

  it('replaces a pending turn with its answer', () => {
    const merged = mergeTurn([turn(1, 1), turn(2, 2, 'pending')], turn(2, 2, 'done'))
    expect(merged[1]!.status).toBe('done')
    expect(merged[1]!.answer).toBe('a2')
  })

  it('moves pending to running', () => {
    expect(mergeTurn([turn(1, 1, 'pending')], turn(1, 1, 'running'))[0]!.status).toBe('running')
  })

  it('never takes a finished turn back to pending (a slow poll overtaken by the answer)', () => {
    const merged = mergeTurn([turn(1, 1, 'done')], turn(1, 1, 'pending'))
    expect(merged[0]!.status).toBe('done')
    expect(mergeTurn([turn(1, 1, 'failed')], turn(1, 1, 'running'))[0]!.status).toBe('failed')
  })

  it('does not mutate its input', () => {
    const before = [turn(1, 1, 'pending')]
    mergeTurn(before, turn(1, 1, 'done'))
    expect(before[0]!.status).toBe('pending')
  })

  it('orders by id when a turn has no position', () => {
    expect(mergeTurn([turn(5, null)], turn(4, null)).map((t) => t.id)).toEqual([4, 5])
  })
})

describe('mergeTurns', () => {
  it('merges a server snapshot into what is on screen, oldest first', () => {
    const merged = mergeTurns([turn(2, 2, 'running')], [turn(2, 2, 'done'), turn(1, 1)])
    expect(merged.map((t) => [t.id, t.status])).toEqual([
      [1, 'done'],
      [2, 'done'],
    ])
  })

  it('keeps a turn already finished on screen over a stale snapshot', () => {
    expect(mergeTurns([turn(2, 2, 'done')], [turn(2, 2, 'pending')])[0]!.status).toBe('done')
  })
})

describe('pending and failed turns', () => {
  it('finds the unfinished turn, pending or running', () => {
    expect(pendingTurn([turn(1, 1), turn(2, 2, 'pending')])?.id).toBe(2)
    expect(pendingTurn([turn(1, 1), turn(2, 2, 'running')])?.id).toBe(2)
    expect(pendingTurn([turn(1, 1), turn(2, 2)])).toBeNull()
    expect(pendingTurn([])).toBeNull()
  })

  it('offers a retry only for a failed last turn', () => {
    expect(failedLastTurn([turn(1, 1), turn(2, 2, 'failed')])?.id).toBe(2)
    expect(failedLastTurn([turn(1, 1, 'failed'), turn(2, 2)])).toBeNull()
    expect(failedLastTurn([])).toBeNull()
  })
})

describe('threadPhase', () => {
  it('names what the thread is doing', () => {
    expect(threadPhase([])).toBe('empty')
    expect(threadPhase([turn(1, 1)])).toBe('ready')
    expect(threadPhase([turn(1, 1, 'pending')])).toBe('answering')
    expect(threadPhase([turn(1, 1, 'failed')])).toBe('failed')
    expect(threadPhase([turn(1, 1)], { sending: true })).toBe('sending')
    expect(threadPhase([turn(1, 1, 'running')], { waiting: true })).toBe('waiting')
  })

  it('lets the composer send only when no turn is in flight', () => {
    expect(canCompose('empty')).toBe(true)
    expect(canCompose('ready')).toBe(true)
    expect(canCompose('failed')).toBe(true)
    expect(canCompose('answering')).toBe(false)
    expect(canCompose('sending')).toBe(false)
    expect(canCompose('waiting')).toBe(false)
  })
})

describe('conversations', () => {
  it('orders newest activity first, ids breaking ties', () => {
    const sorted = sortConversations([
      conv(1, '2026-10-01T10:00:00Z'),
      conv(3, '2026-10-01T12:00:00Z'),
      conv(2, '2026-10-01T12:00:00Z'),
    ])
    expect(sorted.map((c) => c.id)).toEqual([3, 2, 1])
  })

  it('upserts a conversation and re-orders (a new turn moves it to the top)', () => {
    const list = [conv(2, '2026-10-01T12:00:00Z'), conv(1, '2026-10-01T10:00:00Z')]
    expect(upsertConversation(list, conv(1, '2026-10-01T13:00:00Z')).map((c) => c.id)).toEqual([1, 2])
    expect(upsertConversation(list, conv(3, '2026-10-01T11:00:00Z')).map((c) => c.id)).toEqual([2, 3, 1])
  })

  it('drops the extra fields of a detail when upserting', () => {
    const detail = { ...conv(1, '2026-10-01T10:00:00Z'), turns: [turn(1, 1)] }
    expect(upsertConversation([], detail)[0]).not.toHaveProperty('turns')
  })

  it('labels a conversation with no title yet', () => {
    expect(conversationLabel({ title: '' })).toBe('New conversation')
    expect(conversationLabel({ title: '  ' })).toBe('New conversation')
    expect(conversationLabel(null)).toBe('New conversation')
    expect(conversationLabel({ title: 'Vitamin D' })).toBe('Vitamin D')
  })
})

describe('describeTurnFailure', () => {
  const failure = (status: number, code: string, body: unknown = null, detail = '') => ({ status, code, detail, body })
  const limit = { used: 100, limit: 100, resets_at: '2026-11-01T00:00:00+05:30' }

  it('says the monthly chat turns are used up, with the limit from me/', () => {
    const e = describeTurnFailure(failure(429, 'quota_exceeded'), limit)
    expect(e.kind).toBe('quota')
    expect(e.message).toContain('all 100 chat turns')
    expect(e.message).toContain('reset on')
    expect(e.retrySameKey).toBe(false)
  })

  it('prefers the numbers in the 429 body', () => {
    const body = { used: 50, limit: 50, resets_at: null }
    expect(describeTurnFailure(failure(429, 'quota_exceeded', body), limit).message).toContain('all 50 chat turns')
  })

  it('copes with no limit known at all', () => {
    expect(describeTurnFailure(failure(429, 'quota_exceeded'), null).message).toContain('all your chat turns')
  })

  it('tells a plain rate limit from the quota', () => {
    const e = describeTurnFailure(failure(429, 'throttled'), limit)
    expect(e.kind).toBe('throttled')
    expect(e.message).toContain('too fast')
  })

  it('says chat is paused for everyone on a system limit (503)', () => {
    const e = describeTurnFailure(failure(503, 'system_limit_reached'), limit)
    expect(e.kind).toBe('paused')
    expect(e.message).toContain('paused for everyone')
    expect(e.retrySameKey).toBe(false)
  })

  it('retries a dropped connection and a 5xx with the same key', () => {
    expect(describeTurnFailure(failure(0, 'network_error'), null)).toMatchObject({ kind: 'network', retrySameKey: true })
    expect(describeTurnFailure(failure(502, 'http_502', null, 'Bad gateway'), null)).toMatchObject({ kind: 'other', retrySameKey: true })
  })

  it('handles a reused key, a missing conversation, and a field error', () => {
    expect(describeTurnFailure(failure(422, 'idempotency_key_reused'), null).kind).toBe('reused')
    expect(describeTurnFailure(failure(404, 'not_found'), null).kind).toBe('missing')
    expect(describeTurnFailure(failure(400, 'invalid', null, 'question: too long'), null).message).toBe('question: too long')
  })
})

describe('usage', () => {
  it('writes the usage line and knows when the limit is reached', () => {
    expect(usageLine(null)).toBe('')
    expect(usageLine({ used: 3, limit: null, resets_at: null })).toContain('unlimited')
    expect(usageLine({ used: 3, limit: 10, resets_at: null })).toBe('3 / 10 chat turns used')
    expect(overLimit({ used: 10, limit: 10, resets_at: null })).toBe(true)
    expect(overLimit({ used: 9, limit: 10, resets_at: null })).toBe(false)
    expect(overLimit({ used: 99, limit: null, resets_at: null })).toBe(false)
    expect(overLimit(null)).toBe(false)
  })
})
