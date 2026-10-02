import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import type { Attachment } from '../api/types'
import {
  MAX_FILE_BYTES,
  anyWorking,
  canSummarize,
  describeUploadError,
  formatSize,
  isStorageFull,
  mergeAttachment,
  pollAttachments,
  statusText,
  storageText,
  uploadOne,
  validateFile,
} from './attachments'

const att = (over: Partial<Attachment> = {}): Attachment => ({
  id: 1,
  note: 3,
  original_name: 'a.pdf',
  mime_type: 'application/pdf',
  size: 1000,
  sha256: 'x',
  status: 'pending',
  error: '',
  summary: '',
  created_at: '2026-10-01T00:00:00Z',
  ...over,
})
const file = (name: string, size: number, type: string) => ({ name, size, type }) as File
const apiError = (status: number, code: string, body: Record<string, unknown> = {}) =>
  new ApiError(status, code, 'server words', { code, ...body })

describe('validateFile', () => {
  it('accepts the four types', () => {
    for (const [n, t] of [['a.jpg', 'image/jpeg'], ['a.png', 'image/png'], ['a.webp', 'image/webp'], ['a.pdf', 'application/pdf']] as const) {
      expect(validateFile(file(n, 100, t))).toBeNull()
    }
  })
  it('refuses an empty, an oversized and a wrong-type file', () => {
    expect(validateFile(file('a.pdf', 0, 'application/pdf'))).toMatch(/empty/)
    expect(validateFile(file('big.pdf', MAX_FILE_BYTES + 1, 'application/pdf'))).toMatch(/over the 10 MB/)
    expect(validateFile(file('a.exe', 10, 'application/x-msdownload'))).toMatch(/not a JPEG/)
  })
  it('takes the extension when the browser gives no type, but not for a given wrong type', () => {
    expect(validateFile(file('a.PDF', 10, ''))).toBeNull()
    expect(validateFile(file('a.pdf', 10, 'text/plain'))).toMatch(/not a JPEG/)
    expect(validateFile(file('a.txt', 10, ''))).toMatch(/not a JPEG/)
  })
  it('accepts exactly the cap', () => {
    expect(validateFile(file('a.png', MAX_FILE_BYTES, 'image/png'))).toBeNull()
  })
})

describe('describeUploadError', () => {
  it('says too large, wrong type, throttled, paused and offline in words', () => {
    expect(describeUploadError(apiError(413, 'too_large'), 'a.pdf').message).toMatch(/limit for one file/)
    expect(describeUploadError(apiError(415, 'unsupported_file_type'), 'a.exe').message).toMatch(/not a JPEG/)
    expect(describeUploadError(apiError(429, 'throttled'), 'a.pdf').message).toMatch(/too fast/)
    expect(describeUploadError(apiError(503, 'system_limit_reached'), 'a.pdf').message).toMatch(/paused for everyone/)
    expect(describeUploadError(new ApiError(0, 'network_error', 'x', null), 'a.pdf').message).toMatch(/could not reach/)
  })
  it('a full storage quota shows the numbers', () => {
    const out = describeUploadError(apiError(429, 'quota_exceeded', { used: 100 * 1024 * 1024, limit: 100 * 1024 * 1024 }), 'a.pdf')
    expect(out.code).toBe('quota_exceeded')
    expect(out.message).toMatch(/storage is full \(100 MB of 100 MB used\)/)
  })
  it('falls back to the server detail', () => {
    expect(describeUploadError(apiError(400, 'invalid'), 'a.pdf').message).toBe('a.pdf: server words')
  })
})

describe('uploadOne', () => {
  it('never sends an invalid file', async () => {
    const upload = vi.fn()
    const out = await uploadOne({ upload }, 3, file('a.exe', 10, 'application/x-msdownload'))
    expect(upload).not.toHaveBeenCalled()
    expect(out).toMatchObject({ kind: 'refused', skipped: true })
  })
  it('201 is new, 200 is the existing one', async () => {
    const a = att()
    expect(await uploadOne({ upload: async () => ({ status: 201, body: a }) }, 3, file('a.pdf', 10, 'application/pdf'))).toEqual({
      kind: 'ok',
      attachment: a,
      existing: false,
    })
    const again = await uploadOne({ upload: async () => ({ status: 200, body: a }) }, 3, file('a.pdf', 10, 'application/pdf'))
    expect(again).toMatchObject({ kind: 'ok', existing: true })
  })
  it('turns a server refusal into a message and keeps the code', async () => {
    const out = await uploadOne(
      {
        upload: async () => {
          throw apiError(429, 'quota_exceeded', { used: 5, limit: 5 })
        },
      },
      3,
      file('a.pdf', 10, 'application/pdf'),
    )
    expect(out).toMatchObject({ kind: 'refused', code: 'quota_exceeded', skipped: false })
  })
})

describe('status and storage', () => {
  it('pending and extracting are working; ready and failed are not', () => {
    expect(anyWorking([att({ status: 'ready' }), att({ status: 'failed' })])).toBe(false)
    expect(anyWorking([att({ status: 'ready' }), att({ status: 'extracting' })])).toBe(true)
    expect(anyWorking([att({ status: 'pending' })])).toBe(true)
  })
  it('status text carries a failure message; only ready can be summarized', () => {
    expect(statusText(att({ status: 'failed', error: 'needs a password' }))).toBe('Failed: needs a password')
    expect(statusText(att({ status: 'ready' }))).toBe('Ready')
    expect(canSummarize(att({ status: 'ready' }))).toBe(true)
    expect(canSummarize(att({ status: 'failed' }))).toBe(false)
    expect(canSummarize(att({ status: 'pending' }))).toBe(false)
  })
  it('sizes and storage', () => {
    expect(formatSize(512)).toBe('512 B')
    expect(formatSize(2048)).toBe('2 KB')
    expect(formatSize(1.5 * 1024 * 1024)).toBe('1.5 MB')
    expect(storageText({ used: 1024 * 1024, limit: 10 * 1024 * 1024, resets_at: null })).toBe('1.0 MB of 10 MB stored')
    expect(isStorageFull({ used: 10, limit: 10, resets_at: null })).toBe(true)
    expect(isStorageFull({ used: 10, limit: null, resets_at: null })).toBe(false)
  })
  it('merge replaces by id or puts a new one first', () => {
    const list = [att({ id: 2 }), att({ id: 1 })]
    expect(mergeAttachment(list, att({ id: 1, status: 'ready' })).map((a) => a.status)).toEqual(['pending', 'ready'])
    expect(mergeAttachment(list, att({ id: 9 })).map((a) => a.id)).toEqual([9, 2, 1])
  })
})

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

describe('pollAttachments', () => {
  it('polls with growing waits until nothing is pending or extracting', async () => {
    const c = clock()
    const steps = [[att({ status: 'pending' })], [att({ status: 'extracting' })], [att({ status: 'ready' })]]
    const load = vi.fn(async () => steps.shift()!)
    const seen: string[] = []
    const out = await pollAttachments({ load, ...c, onList: (l) => seen.push(l[0]!.status) })
    expect(out).toBe('settled')
    expect(seen).toEqual(['pending', 'extracting', 'ready'])
    expect(c.waits[1]!).toBeGreaterThan(c.waits[0]!)
  })
  it('retries a blip, stops on 404, on cancel and at the deadline', async () => {
    const c = clock()
    let n = 0
    const out = await pollAttachments({
      load: async () => {
        if (n++ === 0) throw new ApiError(0, 'network_error', 'x', null)
        return [att({ status: 'ready' })]
      },
      ...c,
      onList: () => {},
    })
    expect(out).toBe('settled')
    expect(
      await pollAttachments({ load: async () => { throw apiError(404, 'not_found') }, ...clock(), onList: () => {} }),
    ).toBe('lost')
    expect(
      await pollAttachments({ load: async () => [att()], ...clock(), onList: () => {}, cancelled: () => true }),
    ).toBe('cancelled')
    expect(
      await pollAttachments({ load: async () => [att()], ...clock(), onList: () => {}, giveUpMs: 20_000 }),
    ).toBe('timeout')
  })
})
