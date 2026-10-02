/**
 * Attachments on a note, without Vue: what to check before an upload, what the server's refusals
 * mean in words, what a status looks like, and how the list is kept fresh while files are read.
 *
 * The browser's check is a courtesy (D560): the server reads the file's bytes and decides, so a
 * file that passes here can still be refused, and one that fails here is never sent.
 */
import { ApiError, errorMessage } from '../api/client'
import type { Attachment, LimitUsage } from '../api/types'

/** The server's per-file cap (`ATTACHMENT_MAX_BYTES`, 10 MB). The API does not publish it. */
export const MAX_FILE_BYTES = 10 * 1024 * 1024
export const ACCEPT = 'image/jpeg,image/png,image/webp,application/pdf,.jpg,.jpeg,.png,.webp,.pdf'
const TYPES = ['image/jpeg', 'image/png', 'image/webp', 'application/pdf']
const EXTENSIONS = ['.jpg', '.jpeg', '.png', '.webp', '.pdf']

export function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  const mb = bytes / (1024 * 1024)
  return `${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB`
}

export const LIMIT_TEXT = `JPEG, PNG, WebP or PDF, up to ${formatSize(MAX_FILE_BYTES)} each`

/** A message if the file should not be sent; null if it may be. */
export function validateFile(file: { name: string; size: number; type: string }): string | null {
  const name = file.name || 'This file'
  if (file.size === 0) return `${name} is empty.`
  if (file.size > MAX_FILE_BYTES) {
    return `${name} is ${formatSize(file.size)}, over the ${formatSize(MAX_FILE_BYTES)} limit for one file.`
  }
  const lower = file.name.toLowerCase()
  const okType = TYPES.includes(file.type) || (file.type === '' && EXTENSIONS.some((e) => lower.endsWith(e)))
  if (!okType) return `${name} is not a JPEG, PNG, WebP or PDF.`
  return null
}

/** What the upload's failure means, in words. `code` is the server's, or one made here. */
export function describeUploadError(e: unknown, fileName: string): { code: string; message: string } {
  if (!(e instanceof ApiError)) return { code: 'unknown', message: `${fileName}: ${errorMessage(e)}` }
  switch (e.status) {
    case 0:
      return { code: e.code, message: `${fileName}: could not reach the server. Check your connection and try again.` }
    case 413:
      return { code: e.code, message: `${fileName} is over the ${formatSize(MAX_FILE_BYTES)} limit for one file.` }
    case 415:
      return { code: e.code, message: `${fileName} is not a JPEG, PNG, WebP or PDF.` }
    case 404:
      return { code: e.code, message: 'This note no longer exists.' }
    case 429: {
      if (e.code === 'quota_exceeded') {
        const body = e.body as { used?: number; limit?: number | null } | null
        const detail =
          body && typeof body.used === 'number' && typeof body.limit === 'number'
            ? ` (${formatSize(body.used)} of ${formatSize(body.limit)} used)`
            : ''
        return { code: e.code, message: `Your file storage is full${detail}. Delete an attachment to make room.` }
      }
      return { code: e.code, message: 'You are uploading too fast. Wait a moment and try again.' }
    }
    case 503:
      return { code: e.code, message: 'Uploads are paused for everyone right now. Your notes still work; try again later.' }
  }
  return { code: e.code, message: `${fileName}: ${e.detail}` }
}

/** "3.2 MB of 100 MB used", or "" when there is no figure. */
export function storageText(usage: LimitUsage | null | undefined): string {
  if (!usage) return ''
  if (usage.limit === null) return `${formatSize(usage.used)} stored · unlimited`
  return `${formatSize(usage.used)} of ${formatSize(usage.limit)} stored`
}

export function isStorageFull(usage: LimitUsage | null | undefined): boolean {
  return !!usage && usage.limit !== null && usage.used >= usage.limit
}

// ---------------------------------------------------------------- status --

/** Its text is still being read: the list keeps polling while any attachment is. */
export function isWorking(a: Pick<Attachment, 'status'>): boolean {
  return a.status === 'pending' || a.status === 'extracting'
}

export function anyWorking(list: readonly Pick<Attachment, 'status'>[]): boolean {
  return list.some(isWorking)
}

export function statusText(a: Pick<Attachment, 'status' | 'error'>): string {
  switch (a.status) {
    case 'pending':
      return 'Waiting to be read'
    case 'extracting':
      return 'Reading…'
    case 'ready':
      return 'Ready'
    case 'failed':
      return a.error ? `Failed: ${a.error}` : 'Failed'
  }
  return ''
}

/** A summary can be asked for only once the text has been read. */
export function canSummarize(a: Pick<Attachment, 'status'>): boolean {
  return a.status === 'ready'
}

/** Newest first, as the server sends them, with a copy of `incoming` replacing the one with its id. */
export function mergeAttachment(list: readonly Attachment[], incoming: Attachment): Attachment[] {
  const i = list.findIndex((a) => a.id === incoming.id)
  if (i >= 0) return list.map((a, k) => (k === i ? incoming : a))
  return [incoming, ...list]
}

// --------------------------------------------------------------- polling --

export const LIST_POLL_FIRST_MS = 1500
export const LIST_POLL_MAX_MS = 8000
export const LIST_POLL_GIVE_UP_MS = 5 * 60_000

export interface ListPollDeps {
  /** Fetch the current list. */
  load: () => Promise<Attachment[]>
  sleep?: (ms: number) => Promise<void>
  now?: () => number
  cancelled?: () => boolean
  onList: (list: Attachment[]) => void
  giveUpMs?: number
}

const realSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

/**
 * Reload the list with backoff until nothing is pending or extracting. Blips are retried; a 404
 * (note gone) or a 401/403 stops it. Resolves why it stopped.
 */
export async function pollAttachments(deps: ListPollDeps): Promise<'settled' | 'cancelled' | 'timeout' | 'lost'> {
  const sleep = deps.sleep ?? realSleep
  const now = deps.now ?? Date.now
  const deadline = now() + (deps.giveUpMs ?? LIST_POLL_GIVE_UP_MS)
  let delay = LIST_POLL_FIRST_MS
  while (now() < deadline) {
    await sleep(delay)
    if (deps.cancelled?.()) return 'cancelled'
    try {
      const list = await deps.load()
      if (deps.cancelled?.()) return 'cancelled'
      deps.onList(list)
      if (!anyWorking(list)) return 'settled'
    } catch (e) {
      if (e instanceof ApiError && [401, 403, 404].includes(e.status)) return 'lost'
    }
    delay = Math.min(LIST_POLL_MAX_MS, Math.round(delay * 1.5))
  }
  return 'timeout'
}

// -------------------------------------------------------------- uploading --

export type UploadResult =
  | { kind: 'ok'; attachment: Attachment; existing: boolean }
  | { kind: 'refused'; code: string; message: string; skipped: boolean }

export interface UploadDeps {
  upload: (
    noteId: number,
    file: File,
    onProgress: (fraction: number) => void,
  ) => Promise<{ status: number; body: Attachment }>
}

/** Validate, then send one file. A refusal says why, in words; nothing throws. */
export async function uploadOne(
  deps: UploadDeps,
  noteId: number,
  file: File,
  onProgress: (fraction: number) => void = () => {},
): Promise<UploadResult> {
  const problem = validateFile(file)
  if (problem) return { kind: 'refused', code: 'invalid_file', message: problem, skipped: true }
  try {
    const { status, body } = await deps.upload(noteId, file, onProgress)
    return { kind: 'ok', attachment: body, existing: status === 200 }
  } catch (e) {
    const { code, message } = describeUploadError(e, file.name)
    return { kind: 'refused', code, message, skipped: false }
  }
}
