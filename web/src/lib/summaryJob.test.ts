import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import type { SummaryJob } from '../api/types'
import { describeSummaryFailure, describeSummaryStartError, runSummary } from './summaryJob'

const job = (over: Partial<SummaryJob> = {}): SummaryJob => ({
  id: 5,
  note_id: 3,
  attachment_id: null,
  status: 'pending',
  base_version: 2,
  summary: '',
  error_code: '',
  error: '',
  created_at: '2026-10-01T00:00:00Z',
  completed_at: null,
  ...over,
})
const apiError = (status: number, code: string, body: Record<string, unknown> = {}) =>
  new ApiError(status, code, 'server words', { code, ...body })
const fast = { sleep: async () => {}, now: (() => { let t = 0; return () => (t += 1000) })() }

describe('describeSummaryStartError', () => {
  it('429 quota carries the numbers, throttled does not', () => {
    const q = describeSummaryStartError(apiError(429, 'quota_exceeded', { used: 5, limit: 5, resets_at: '2026-11-01T00:00:00Z' }))
    expect(q).toMatchObject({ retryable: false, usage: { used: 5, limit: 5 } })
    expect(q.message).toMatch(/all 5 summaries/)
    const t = describeSummaryStartError(apiError(429, 'throttled'))
    expect(t.retryable).toBe(true)
    expect(t.message).toMatch(/too fast/)
  })
  it('503 means paused or unavailable, retryable', () => {
    expect(describeSummaryStartError(apiError(503, 'system_limit_reached'))).toMatchObject({ retryable: true })
    expect(describeSummaryStartError(apiError(503, 'unavailable')).message).toMatch(/unavailable/)
  })
  it('knows the 400s, and reuses the key after a lost reply', () => {
    expect(describeSummaryStartError(apiError(400, 'nothing_to_summarize')).retryable).toBe(false)
    expect(describeSummaryStartError(apiError(400, 'attachment_not_ready')).message).toMatch(/not been read/)
    expect(describeSummaryStartError(new ApiError(0, 'network_error', 'x', null)).reuseKey).toBe(true)
    expect(describeSummaryStartError(apiError(500, 'http_500')).reuseKey).toBe(true)
  })
})

describe('describeSummaryFailure', () => {
  it('prefers the server words, then ours; a gone note is not retryable', () => {
    expect(describeSummaryFailure(job({ status: 'failed', error_code: 'summary_busy', error: 'Busy now' })).message).toBe('Busy now')
    expect(describeSummaryFailure(job({ status: 'failed', error_code: 'summary_busy' })).message).toMatch(/busy/)
    expect(describeSummaryFailure(job({ status: 'failed', error_code: 'summary_gone' })).retryable).toBe(false)
  })
})

describe('runSummary', () => {
  it('follows pending to done and reports every state', async () => {
    const states = [job({ status: 'running' }), job({ status: 'done', summary: 'The gist.' })]
    const seen: string[] = []
    const out = await runSummary(
      { create: async () => job(), get: async () => states.shift()! },
      'key-1',
      { ...fast, onJob: (j) => seen.push(j.status) },
    )
    expect(out).toMatchObject({ kind: 'done', job: { summary: 'The gist.' } })
    expect(seen).toEqual(['pending', 'running', 'done'])
  })
  it('a replayed 200 that is already done needs no polling', async () => {
    const get = vi.fn()
    const out = await runSummary({ create: async () => job({ status: 'done', summary: 'S' }), get }, 'k', fast)
    expect(out.kind).toBe('done')
    expect(get).not.toHaveBeenCalled()
  })
  it('a failed job and a failed start are problems', async () => {
    const failed = await runSummary(
      { create: async () => job(), get: async () => job({ status: 'failed', error_code: 'summary_failed' }) },
      'k',
      fast,
    )
    expect(failed).toMatchObject({ kind: 'failed', problem: { stage: 'job', code: 'summary_failed' } })
    const refused = await runSummary(
      { create: async () => { throw apiError(429, 'quota_exceeded', { used: 1, limit: 1 }) }, get: vi.fn() },
      'k',
      fast,
    )
    expect(refused).toMatchObject({ kind: 'failed', problem: { code: 'quota_exceeded' } })
  })
  it('a done job with no text is a failure; cancel stops quietly; 404 on poll is lost', async () => {
    expect(await runSummary({ create: async () => job({ status: 'done' }), get: vi.fn() }, 'k', fast)).toMatchObject({
      kind: 'failed',
    })
    expect(await runSummary({ create: async () => job(), get: vi.fn() }, 'k', { ...fast, cancelled: () => true })).toEqual({
      kind: 'cancelled',
    })
    expect(
      await runSummary({ create: async () => job(), get: async () => { throw apiError(404, 'not_found') } }, 'k', fast),
    ).toMatchObject({ kind: 'failed', problem: { code: 'job_lost' } })
  })
})
