/**
 * "Format my note", without Vue: start a job, poll it, apply it, and say what went wrong in words.
 *
 * `POST notes/<id>/format/` answers 202 with a pending job; the client polls
 * `GET format-jobs/<id>/` until it is done or failed. There is no apply
 * endpoint: Apply is the ordinary note PATCH with `content = proposed_content`
 * and `version = base_version`, so a note edited meanwhile gets the usual 409
 * (the server never writes the note from a job). Everything here takes its
 * API and its clock as arguments, so the tests run without a network or timers.
 */
import { ApiError, errorMessage } from '../api/client'
import type { DocNode, FormatJob, FormatQuotaBody, LimitUsage, Note, NoteUpdateRequest, VersionConflictBody } from '../api/types'

export interface FormatDeps {
  /** `POST notes/<id>/format/` with the Idempotency-Key. */
  create: (noteId: number, idempotencyKey: string) => Promise<FormatJob>
  /** `GET format-jobs/<id>/`. */
  get: (jobId: number) => Promise<FormatJob>
  /** The ordinary note PATCH. */
  patch: (noteId: number, body: NoteUpdateRequest) => Promise<Note>
}

export const POLL_FIRST_MS = 800
export const POLL_MAX_MS = 5_000
export const POLL_FACTOR = 1.5
/** Stop watching after this long; the server sweeps a stuck job and refunds it. */
export const POLL_GIVE_UP_MS = 3 * 60_000

export function isFinished(job: { status: string }): boolean {
  return job.status === 'done' || job.status === 'failed'
}

// ------------------------------------------------------------- the note --

/** True when the document holds no text at all (an empty paragraph, an empty list item). */
export function isDocEmpty(doc: DocNode | null | undefined): boolean {
  if (!doc) return true
  if (typeof doc.text === 'string' && doc.text.trim() !== '') return false
  return (doc.content ?? []).every(isDocEmpty)
}

// --------------------------------------------------------------- usage --

/** "3 / 20 formats used · resets 1 Nov" style text, without the date (the caller formats it). */
export function usageText(usage: LimitUsage | null | undefined): string {
  if (!usage) return ''
  if (usage.limit === null) return `${usage.used} formats this month · unlimited`
  return `${usage.used} / ${usage.limit} formats used`
}

export function isOutOfFormats(usage: LimitUsage | null | undefined): boolean {
  return !!usage && usage.limit !== null && usage.used >= usage.limit
}

/** Why the Format button is off, in words; empty when it is on. */
export function whyDisabled(state: {
  empty: boolean
  usage: LimitUsage | null | undefined
  unavailable?: string
}): string {
  if (state.unavailable) return state.unavailable
  if (state.empty) return 'Write something first: there is nothing to format yet.'
  if (isOutOfFormats(state.usage)) return 'You have used all your formats for this month.'
  return ''
}

// -------------------------------------------------------------- problems --

/** Something that stopped a format, ready to show. */
export interface FormatProblem {
  /** A server code (`note_empty`, `quota_exceeded`, `format_changed_content`...) or one made here. */
  code: string
  message: string
  /** Where it went wrong: the POST, the job itself, or the polling. */
  stage: 'start' | 'job' | 'poll'
  /** Whether trying again can help. */
  retryable: boolean
  /** The POST got no answer (network, 5xx): a retry should reuse the Idempotency-Key. */
  reuseKey: boolean
  /** From a 429 `quota_exceeded`: the new numbers. */
  usage?: LimitUsage
}

const problem = (p: Partial<FormatProblem> & Pick<FormatProblem, 'code' | 'message' | 'stage'>): FormatProblem => ({
  retryable: true,
  reuseKey: false,
  ...p,
})

/** A failed POST (or anything it threw), as a message. */
export function describeStartError(e: unknown): FormatProblem {
  if (!(e instanceof ApiError)) return problem({ code: 'unknown', message: errorMessage(e), stage: 'start' })
  switch (e.code) {
    case 'note_empty':
      return problem({
        code: e.code,
        stage: 'start',
        message: 'This note is empty: there is nothing to format yet.',
        retryable: false,
      })
    case 'note_too_long':
      return problem({
        code: e.code,
        stage: 'start',
        message: 'This note is too long to format in one go. Split it into shorter notes.',
        retryable: false,
      })
    case 'quota_exceeded': {
      const body = e.body as unknown as Partial<FormatQuotaBody> | null
      const usage: LimitUsage | undefined =
        body && typeof body.used === 'number' && typeof body.limit === 'number'
          ? { used: body.used, limit: body.limit, resets_at: body.resets_at ?? null }
          : undefined
      return problem({
        code: e.code,
        stage: 'start',
        message: usage?.limit
          ? `You have used all ${usage.limit} formats for this month.`
          : 'You have used all your formats for this month.',
        retryable: false,
        usage,
      })
    }
    case 'system_limit_reached':
      // The whole service is at its monthly cap (D84, D130): nothing was created.
      return problem({
        code: e.code,
        stage: 'start',
        message: 'Formatting is paused for everyone right now. Your notes still work; try again later.',
      })
    case 'throttled':
      return problem({
        code: e.code,
        stage: 'start',
        message: 'You are formatting too fast. Wait a moment and try again.',
      })
    case 'idempotency_key_reused':
      return problem({
        code: e.code,
        stage: 'start',
        message: 'That request clashed with an earlier one. Try again to send it as a new request.',
      })
  }
  if (e.status === 404) {
    return problem({ code: e.code, stage: 'start', message: 'This note no longer exists.', retryable: false })
  }
  // A dropped connection or a 5xx may still have created the job: retry with the same key.
  const maybeCreated = e.status === 0 || e.status >= 500
  return problem({
    code: e.code,
    stage: 'start',
    message: e.status === 0 ? 'Could not reach the server. Try again.' : errorMessage(e),
    reuseKey: maybeCreated,
  })
}

const JOB_FAILURE_TEXT: Record<string, string> = {
  format_changed_content: 'The formatted version changed what your note says, so it was not used. Nothing was changed.',
  format_failed: "The assistant couldn't format this note. Try again in a little while.",
  format_busy: 'The assistant is busy right now. Try again in a few minutes.',
  format_stuck: 'This took too long. Please try again.',
  format_note_changed: 'This note was edited before it could be formatted. Try again.',
  format_note_gone: 'This note no longer exists.',
}
const JOB_FAILURE_DEFAULT = 'This note could not be formatted. Please try again.'

/** A failed job as a message: the server's own words first, our wording if it sent none. */
export function describeJobFailure(job: FormatJob): FormatProblem {
  const code = job.error_code || 'format_failed'
  return problem({
    code,
    stage: 'job',
    message: job.error || JOB_FAILURE_TEXT[code] || JOB_FAILURE_DEFAULT,
    // Only a deleted note cannot be tried again; a failed job was not counted against the limit.
    retryable: code !== 'format_note_gone',
  })
}

// --------------------------------------------------------------- polling --

export interface PollOptions<J extends { status: string } = FormatJob> {
  sleep?: (ms: number) => Promise<void>
  now?: () => number
  /** Checked after every wait: stop quietly (the panel was closed, the account changed). */
  cancelled?: () => boolean
  /** Called with every state of the job that came back. */
  onJob?: (job: J) => void
  giveUpMs?: number
}

export type PollResult<J extends { status: string } = FormatJob> =
  | { kind: 'finished'; job: J }
  | { kind: 'cancelled' }
  | { kind: 'timeout' }
  /** The job is gone or the session ended (401, 403, 404): polling cannot help. */
  | { kind: 'lost'; status: number }

const realSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

/** Poll one job with backoff until it is done or failed. Offline blips and 5xx are retried. */
export async function pollFormatJob<J extends { status: string } = FormatJob>(
  get: (jobId: number) => Promise<J>,
  jobId: number,
  options: PollOptions<J> = {},
): Promise<PollResult<J>> {
  const sleep = options.sleep ?? realSleep
  const now = options.now ?? Date.now
  const deadline = now() + (options.giveUpMs ?? POLL_GIVE_UP_MS)
  let delay = POLL_FIRST_MS
  while (now() < deadline) {
    await sleep(delay)
    if (options.cancelled?.()) return { kind: 'cancelled' }
    try {
      const job = await get(jobId)
      if (options.cancelled?.()) return { kind: 'cancelled' }
      options.onJob?.(job)
      if (isFinished(job)) return { kind: 'finished', job }
    } catch (e) {
      if (e instanceof ApiError && [401, 403, 404].includes(e.status)) return { kind: 'lost', status: e.status }
      // Offline, a 429 or a 5xx: worth another try.
    }
    delay = Math.min(POLL_MAX_MS, delay * POLL_FACTOR)
  }
  return { kind: 'timeout' }
}

// ------------------------------------------------------------ the whole run --

export type FormatOutcome =
  /** A proposal to show: `job.status` is `done` and `proposed_content` is a document. */
  | { kind: 'ready'; job: FormatJob }
  | { kind: 'failed'; problem: FormatProblem }
  | { kind: 'cancelled' }

function hasProposal(job: FormatJob): boolean {
  const doc = job.proposed_content
  return !!doc && typeof doc === 'object' && doc.type === 'doc'
}

/**
 * Start a job for the note and follow it to the end. `onJob` hears every state of the job (the
 * 202 included), so a screen can show "working" as soon as it exists.
 */
export async function runFormat(
  deps: Pick<FormatDeps, 'create' | 'get'>,
  noteId: number,
  idempotencyKey: string,
  options: PollOptions = {},
): Promise<FormatOutcome> {
  let job: FormatJob
  try {
    // 202 (new) and 200 (a replayed key) are the same to us: a job to show and, if unfinished, poll.
    job = await deps.create(noteId, idempotencyKey)
  } catch (e) {
    return { kind: 'failed', problem: describeStartError(e) }
  }
  options.onJob?.(job)
  if (!isFinished(job)) {
    const result = await pollFormatJob(deps.get, job.id, options)
    switch (result.kind) {
      case 'cancelled':
        return { kind: 'cancelled' }
      case 'timeout':
        return {
          kind: 'failed',
          problem: problem({
            code: 'poll_timeout',
            stage: 'poll',
            message: 'This is taking longer than expected. Try again in a little while.',
          }),
        }
      case 'lost':
        return {
          kind: 'failed',
          problem: problem({
            code: 'job_lost',
            stage: 'poll',
            message:
              result.status === 404
                ? 'This format request is gone. Try again.'
                : 'You were signed out before the note was formatted.',
            retryable: result.status === 404,
          }),
        }
      case 'finished':
        job = result.job
    }
  }
  if (job.status === 'failed') return { kind: 'failed', problem: describeJobFailure(job) }
  if (!hasProposal(job)) {
    return {
      kind: 'failed',
      problem: problem({ code: 'format_failed', stage: 'job', message: JOB_FAILURE_TEXT.format_failed! }),
    }
  }
  return { kind: 'ready', job }
}

// ----------------------------------------------------------------- apply --

export type ApplyOutcome =
  | { kind: 'applied'; note: Note }
  /** The note was edited since (409): `current` is the server's copy; nothing was written. */
  | { kind: 'conflict'; current: Note | null }
  | { kind: 'gone' }
  | { kind: 'error'; message: string }

/** Apply a finished job: `PATCH` the note with the proposal at the version it was made from. */
export async function applyFormat(deps: Pick<FormatDeps, 'patch'>, job: FormatJob): Promise<ApplyOutcome> {
  if (job.status !== 'done' || !hasProposal(job)) {
    return { kind: 'error', message: 'There is nothing to apply.' }
  }
  try {
    const note = await deps.patch(job.note_id, { version: job.base_version, content: job.proposed_content as DocNode })
    return { kind: 'applied', note }
  } catch (e) {
    if (e instanceof ApiError && e.status === 409 && e.code === 'version_conflict') {
      const current = (e.body as unknown as Partial<VersionConflictBody> | null)?.current ?? null
      return { kind: 'conflict', current }
    }
    if (e instanceof ApiError && e.status === 404) return { kind: 'gone' }
    return { kind: 'error', message: errorMessage(e) }
  }
}
