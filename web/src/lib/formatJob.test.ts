import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import type { DocNode, FormatJob, Note } from '../api/types'
import {
  applyFormat,
  describeJobFailure,
  describeStartError,
  isDocEmpty,
  isOutOfFormats,
  pollFormatJob,
  runFormat,
  usageText,
  whyDisabled,
} from './formatJob'

const DOC: DocNode = { type: 'doc', content: [{ type: 'heading', content: [{ type: 'text', text: 'Hi' }] }] }

const job = (over: Partial<FormatJob> = {}): FormatJob => ({
  id: 7,
  note_id: 3,
  status: 'pending',
  base_version: 4,
  proposed_content: null,
  error_code: '',
  error: '',
  created_at: '2026-10-01T00:00:00Z',
  completed_at: null,
  ...over,
})
const done = (over: Partial<FormatJob> = {}) => job({ status: 'done', proposed_content: DOC, ...over })
const failed = (code: string, error = '') => job({ status: 'failed', error_code: code, error })

const apiError = (status: number, code: string, body: Record<string, unknown> = {}) =>
  new ApiError(status, code, (body.detail as string) ?? 'server words', { code, ...body })

/** A clock that only moves when the (fake) sleep does. */
function clock() {
  let t = 0
  const waits: number[] = []
  return {
    waits,
    now: () => t,
    sleep: async (ms: number) => {
      waits.push(ms)
      t += ms
    },
  }
}

describe('isDocEmpty', () => {
  it.each([
    [{ type: 'doc', content: [{ type: 'paragraph' }] }, true],
    [{ type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text: '  ' }] }] }, true],
    [{ type: 'doc', content: [{ type: 'taskList', content: [{ type: 'taskItem', content: [{ type: 'paragraph' }] }] }] }, true],
    [{ type: 'doc' }, true],
    [DOC, false],
  ])('%j -> %s', (doc, expected) => {
    expect(isDocEmpty(doc as DocNode)).toBe(expected)
  })

  it('is empty for a missing document', () => {
    expect(isDocEmpty(null)).toBe(true)
  })
})

describe('usage and the disabled reason', () => {
  const usage = (used: number, limit: number | null) => ({ used, limit, resets_at: '2026-11-01T00:00:00Z' })

  it('reads the numbers', () => {
    expect(usageText(usage(3, 20))).toBe('3 / 20 formats used')
    expect(usageText(usage(3, null))).toBe('3 formats this month · unlimited')
    expect(usageText(null)).toBe('')
  })

  it('is out only at a finite limit', () => {
    expect(isOutOfFormats(usage(20, 20))).toBe(true)
    expect(isOutOfFormats(usage(19, 20))).toBe(false)
    expect(isOutOfFormats(usage(999, null))).toBe(false)
    expect(isOutOfFormats(undefined)).toBe(false)
  })

  it('explains why, most specific first', () => {
    expect(whyDisabled({ empty: false, usage: usage(1, 5) })).toBe('')
    expect(whyDisabled({ empty: true, usage: usage(1, 5) })).toMatch(/nothing to format/)
    expect(whyDisabled({ empty: false, usage: usage(5, 5) })).toMatch(/used all/)
    expect(whyDisabled({ empty: true, usage: usage(1, 5), unavailable: 'Deleted.' })).toBe('Deleted.')
  })
})

describe('describeStartError', () => {
  it('note_empty and note_too_long cannot be retried', () => {
    expect(describeStartError(apiError(400, 'note_empty'))).toMatchObject({ code: 'note_empty', retryable: false })
    expect(describeStartError(apiError(400, 'note_too_long'))).toMatchObject({ code: 'note_too_long', retryable: false })
  })

  it('a 429 quota error carries the new usage', () => {
    const p = describeStartError(
      apiError(429, 'quota_exceeded', { used: 20, limit: 20, resets_at: '2026-11-01T00:00:00Z' }),
    )
    expect(p.message).toBe('You have used all 20 formats for this month.')
    expect(p.usage).toEqual({ used: 20, limit: 20, resets_at: '2026-11-01T00:00:00Z' })
    expect(p.retryable).toBe(false)
  })

  it('a 429 quota error without numbers still reads well', () => {
    const p = describeStartError(apiError(429, 'quota_exceeded'))
    expect(p.message).toMatch(/used all your formats/)
    expect(p.usage).toBeUndefined()
  })

  it('a rate limit is retryable; the system cap is its own message', () => {
    expect(describeStartError(apiError(429, 'throttled'))).toMatchObject({ code: 'throttled', retryable: true })
    const sys = describeStartError(apiError(503, 'system_limit_reached'))
    expect(sys.message).toMatch(/paused for everyone/)
    expect(sys.reuseKey).toBe(false)
  })

  it('a reused key is retried with a new one', () => {
    expect(describeStartError(apiError(422, 'idempotency_key_reused'))).toMatchObject({
      retryable: true,
      reuseKey: false,
    })
  })

  it('no answer or a 5xx may have made the job: reuse the key', () => {
    expect(describeStartError(new ApiError(0, 'network_error', 'x', null)).reuseKey).toBe(true)
    expect(describeStartError(apiError(500, 'http_500')).reuseKey).toBe(true)
    expect(describeStartError(apiError(400, 'invalid')).reuseKey).toBe(false)
  })

  it('a missing note is final', () => {
    expect(describeStartError(apiError(404, 'not_found'))).toMatchObject({ retryable: false })
  })

  it('anything else gets a generic message', () => {
    expect(describeStartError(new Error('boom')).message).toMatch(/Something went wrong/)
  })
})

describe('describeJobFailure', () => {
  it("shows the server's words", () => {
    const p = describeJobFailure(failed('format_changed_content', 'It changed your note.'))
    expect(p).toMatchObject({ code: 'format_changed_content', message: 'It changed your note.', stage: 'job', retryable: true })
  })

  it('falls back to our own words per code, and a default', () => {
    expect(describeJobFailure(failed('format_busy')).message).toMatch(/busy/)
    expect(describeJobFailure(failed('something_new')).message).toMatch(/could not be formatted/)
    expect(describeJobFailure(failed('')).code).toBe('format_failed')
  })

  it('only a deleted note is not worth retrying', () => {
    expect(describeJobFailure(failed('format_note_gone')).retryable).toBe(false)
    expect(describeJobFailure(failed('format_note_changed')).retryable).toBe(true)
  })
})

describe('pollFormatJob', () => {
  it('polls with growing waits until done', async () => {
    const c = clock()
    const get = vi
      .fn()
      .mockResolvedValueOnce(job({ status: 'running' }))
      .mockResolvedValueOnce(job({ status: 'running' }))
      .mockResolvedValueOnce(done())
    const seen: string[] = []
    const result = await pollFormatJob(get, 7, { ...c, onJob: (j) => seen.push(j.status) })
    expect(result).toMatchObject({ kind: 'finished', job: { status: 'done' } })
    expect(get).toHaveBeenCalledTimes(3)
    expect(get).toHaveBeenCalledWith(7)
    expect(seen).toEqual(['running', 'running', 'done'])
    expect(c.waits).toEqual([800, 1200, 1800])
  })

  it('stops on failed', async () => {
    const get = vi.fn().mockResolvedValueOnce(failed('format_failed'))
    const result = await pollFormatJob(get, 7, clock())
    expect(result).toMatchObject({ kind: 'finished', job: { status: 'failed' } })
  })

  it('caps the wait at 5 seconds', async () => {
    const c = clock()
    const get = vi.fn().mockResolvedValue(job({ status: 'running' }))
    await pollFormatJob(get, 7, { ...c, giveUpMs: 60_000 })
    expect(Math.max(...c.waits)).toBe(5000)
  })

  it('gives up after the deadline', async () => {
    const c = clock()
    const get = vi.fn().mockResolvedValue(job({ status: 'running' }))
    const result = await pollFormatJob(get, 7, { ...c, giveUpMs: 10_000 })
    expect(result).toEqual({ kind: 'timeout' })
    expect(c.now()).toBeGreaterThanOrEqual(10_000)
  })

  it('retries a network failure or a 5xx', async () => {
    const get = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(0, 'network_error', 'x', null))
      .mockRejectedValueOnce(apiError(502, 'http_502'))
      .mockResolvedValueOnce(done())
    expect(await pollFormatJob(get, 7, clock())).toMatchObject({ kind: 'finished' })
    expect(get).toHaveBeenCalledTimes(3)
  })

  it.each([401, 403, 404])('%i ends the polling', async (status) => {
    const get = vi.fn().mockRejectedValue(apiError(status, `http_${status}`))
    expect(await pollFormatJob(get, 7, clock())).toEqual({ kind: 'lost', status })
    expect(get).toHaveBeenCalledTimes(1)
  })

  it('stops quietly when cancelled, without another request', async () => {
    let stop = false
    const c = clock()
    const get = vi.fn().mockResolvedValue(job({ status: 'running' }))
    const result = await pollFormatJob(get, 7, {
      ...c,
      cancelled: () => stop,
      sleep: async (ms) => {
        await c.sleep(ms)
        stop = true
      },
    })
    expect(result).toEqual({ kind: 'cancelled' })
    expect(get).not.toHaveBeenCalled()
  })
})

describe('runFormat', () => {
  it('creates, polls and returns the proposal', async () => {
    const create = vi.fn().mockResolvedValue(job())
    const get = vi.fn().mockResolvedValueOnce(job({ status: 'running' })).mockResolvedValueOnce(done())
    const seen: string[] = []
    const out = await runFormat({ create, get }, 3, 'key-1', { ...clock(), onJob: (j) => seen.push(j.status) })
    expect(create).toHaveBeenCalledWith(3, 'key-1')
    expect(out).toMatchObject({ kind: 'ready', job: { id: 7, base_version: 4 } })
    expect(seen).toEqual(['pending', 'running', 'done'])
  })

  it('does not poll a replayed job that is already done', async () => {
    const create = vi.fn().mockResolvedValue(done())
    const get = vi.fn()
    expect(await runFormat({ create, get }, 3, 'k', clock())).toMatchObject({ kind: 'ready' })
    expect(get).not.toHaveBeenCalled()
  })

  it('maps a failed job to its message', async () => {
    const create = vi.fn().mockResolvedValue(job())
    const get = vi.fn().mockResolvedValue(failed('format_changed_content', 'Not used.'))
    const out = await runFormat({ create, get }, 3, 'k', clock())
    expect(out).toMatchObject({ kind: 'failed', problem: { code: 'format_changed_content', message: 'Not used.', stage: 'job' } })
  })

  it('maps a POST error without polling', async () => {
    const create = vi.fn().mockRejectedValue(apiError(429, 'quota_exceeded', { used: 5, limit: 5, resets_at: null }))
    const get = vi.fn()
    const out = await runFormat({ create, get }, 3, 'k', clock())
    expect(out).toMatchObject({ kind: 'failed', problem: { code: 'quota_exceeded', stage: 'start' } })
    expect(get).not.toHaveBeenCalled()
  })

  it('a poll timeout and a vanished job are failures with their own codes', async () => {
    const create = vi.fn().mockResolvedValue(job())
    const slow = vi.fn().mockResolvedValue(job({ status: 'running' }))
    expect(await runFormat({ create, get: slow }, 3, 'k', { ...clock(), giveUpMs: 3000 })).toMatchObject({
      kind: 'failed',
      problem: { code: 'poll_timeout', retryable: true },
    })
    const gone = vi.fn().mockRejectedValue(apiError(404, 'not_found'))
    expect(await runFormat({ create, get: gone }, 3, 'k', clock())).toMatchObject({
      kind: 'failed',
      problem: { code: 'job_lost' },
    })
  })

  it('a "done" job with no document is a failure, not a proposal', async () => {
    const create = vi.fn().mockResolvedValue(job({ status: 'done', proposed_content: null }))
    expect(await runFormat({ create, get: vi.fn() }, 3, 'k', clock())).toMatchObject({ kind: 'failed' })
  })

  it('reports cancelled', async () => {
    const create = vi.fn().mockResolvedValue(job())
    const out = await runFormat({ create, get: vi.fn() }, 3, 'k', { ...clock(), cancelled: () => true })
    expect(out).toEqual({ kind: 'cancelled' })
  })
})

describe('applyFormat', () => {
  const saved = { id: 3, version: 5 } as unknown as Note

  it('PATCHes the proposal at base_version, nothing else', async () => {
    const patch = vi.fn().mockResolvedValue(saved)
    const out = await applyFormat({ patch }, done())
    expect(patch).toHaveBeenCalledWith(3, { version: 4, content: DOC })
    expect(out).toEqual({ kind: 'applied', note: saved })
  })

  it('a 409 hands back the current copy and writes nothing more', async () => {
    const current = { id: 3, version: 9 }
    const patch = vi.fn().mockRejectedValue(apiError(409, 'version_conflict', { current }))
    const out = await applyFormat({ patch }, done())
    expect(out).toEqual({ kind: 'conflict', current })
    expect(patch).toHaveBeenCalledTimes(1)
  })

  it('a 409 of another kind is an error', async () => {
    const patch = vi.fn().mockRejectedValue(apiError(409, 'something_else', { detail: 'No.' }))
    expect(await applyFormat({ patch }, done())).toEqual({ kind: 'error', message: 'No.' })
  })

  it('a 404 means the note is gone', async () => {
    const patch = vi.fn().mockRejectedValue(apiError(404, 'not_found'))
    expect(await applyFormat({ patch }, done())).toEqual({ kind: 'gone' })
  })

  it('other errors keep their message', async () => {
    const patch = vi.fn().mockRejectedValue(new ApiError(0, 'network_error', 'Offline.', null))
    expect(await applyFormat({ patch }, done())).toEqual({ kind: 'error', message: 'Offline.' })
  })

  it('refuses a job that is not done', async () => {
    const patch = vi.fn()
    expect(await applyFormat({ patch }, job({ status: 'running' }))).toMatchObject({ kind: 'error' })
    expect(await applyFormat({ patch }, failed('format_failed'))).toMatchObject({ kind: 'error' })
    expect(patch).not.toHaveBeenCalled()
  })
})
