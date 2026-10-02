/**
 * "Summarize", without Vue: start a job for a note or a file, poll it, and say what went wrong.
 *
 * Both endpoints answer 202 (or 200 for a replayed key or a job still running) with a job, and
 * the client polls `GET summary-jobs/<id>/`. The summary is already stored on the note or the
 * attachment when the job is done; the job carries the text too, so a screen can show it at once.
 * Polling reuses the format job's backoff (lib/formatJob.ts).
 */
import { ApiError, errorMessage } from '../api/client'
import type { LimitUsage, SummaryJob } from '../api/types'
import { isFinished, pollFormatJob, type PollOptions } from './formatJob'

export interface SummaryDeps {
  /** `POST notes/<id>/summarize/` or `POST attachments/<id>/summarize/`, with the Idempotency-Key. */
  create: (idempotencyKey: string) => Promise<SummaryJob>
  /** `GET summary-jobs/<id>/`. */
  get: (jobId: number) => Promise<SummaryJob>
}

export interface SummaryProblem {
  code: string
  message: string
  stage: 'start' | 'job' | 'poll'
  retryable: boolean
  /** The POST got no answer (network, 5xx): a retry should reuse the Idempotency-Key. */
  reuseKey: boolean
  usage?: LimitUsage
}

const problem = (p: Partial<SummaryProblem> & Pick<SummaryProblem, 'code' | 'message' | 'stage'>): SummaryProblem => ({
  retryable: true,
  reuseKey: false,
  ...p,
})

export function describeSummaryStartError(e: unknown): SummaryProblem {
  if (!(e instanceof ApiError)) return problem({ code: 'unknown', message: errorMessage(e), stage: 'start' })
  switch (e.code) {
    case 'nothing_to_summarize':
      return problem({
        code: e.code,
        stage: 'start',
        message: 'There is nothing to summarize yet.',
        retryable: false,
      })
    case 'attachment_not_ready':
      return problem({
        code: e.code,
        stage: 'start',
        message: 'This file has not been read yet. Try again when it says Ready.',
        retryable: false,
      })
    case 'quota_exceeded': {
      const body = e.body as { used?: number; limit?: number | null; resets_at?: string | null } | null
      const usage: LimitUsage | undefined =
        body && typeof body.used === 'number' && typeof body.limit === 'number'
          ? { used: body.used, limit: body.limit, resets_at: body.resets_at ?? null }
          : undefined
      return problem({
        code: e.code,
        stage: 'start',
        message: usage?.limit
          ? `You have used all ${usage.limit} summaries for this month.`
          : 'You have used all your summaries for this month.',
        retryable: false,
        usage,
      })
    }
    case 'system_limit_reached':
      return problem({
        code: e.code,
        stage: 'start',
        message: 'Summaries are paused for everyone right now. Your notes still work; try again later.',
      })
    case 'throttled':
      return problem({
        code: e.code,
        stage: 'start',
        message: 'You are asking for summaries too fast. Wait a moment and try again.',
      })
    case 'idempotency_key_reused':
      return problem({
        code: e.code,
        stage: 'start',
        message: 'That request clashed with an earlier one. Try again to send it as a new request.',
      })
  }
  if (e.status === 503) {
    return problem({
      code: e.code,
      stage: 'start',
      message: 'Summaries are unavailable right now. Try again later.',
    })
  }
  if (e.status === 404) {
    return problem({ code: e.code, stage: 'start', message: 'This note or file no longer exists.', retryable: false })
  }
  const maybeCreated = e.status === 0 || e.status >= 500
  return problem({
    code: e.code,
    stage: 'start',
    message: e.status === 0 ? 'Could not reach the server. Try again.' : errorMessage(e),
    reuseKey: maybeCreated,
  })
}

const JOB_FAILURE_TEXT: Record<string, string> = {
  summary_failed: "The assistant couldn't summarize this. Try again in a little while.",
  summary_busy: 'The assistant is busy right now. Try again in a few minutes.',
  summary_stuck: 'This took too long. Please try again.',
  summary_empty: 'The assistant sent nothing back. Try again.',
  summary_note_changed: 'This note was edited before it could be summarized. Try again.',
  summary_gone: 'This note or file no longer exists.',
  summary_not_stored: 'A newer summary already exists.',
}

export function describeSummaryFailure(job: SummaryJob): SummaryProblem {
  const code = job.error_code || 'summary_failed'
  return problem({
    code,
    stage: 'job',
    message: job.error || JOB_FAILURE_TEXT[code] || 'This could not be summarized. Please try again.',
    retryable: code !== 'summary_gone',
  })
}

export type SummaryOutcome =
  | { kind: 'done'; job: SummaryJob }
  | { kind: 'failed'; problem: SummaryProblem }
  | { kind: 'cancelled' }

/** Start a job and follow it to the end. `onJob` hears every state (the 202 included). */
export async function runSummary(
  deps: SummaryDeps,
  idempotencyKey: string,
  options: PollOptions<SummaryJob> = {},
): Promise<SummaryOutcome> {
  let job: SummaryJob
  try {
    job = await deps.create(idempotencyKey)
  } catch (e) {
    return { kind: 'failed', problem: describeSummaryStartError(e) }
  }
  options.onJob?.(job)
  if (!isFinished(job)) {
    const result = await pollFormatJob<SummaryJob>(deps.get, job.id, options)
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
                ? 'This summary request is gone. Try again.'
                : 'You were signed out before the summary was made.',
            retryable: result.status === 404,
          }),
        }
      case 'finished':
        job = result.job
    }
  }
  if (job.status === 'failed') return { kind: 'failed', problem: describeSummaryFailure(job) }
  if (!job.summary) {
    return {
      kind: 'failed',
      problem: problem({ code: 'summary_empty', stage: 'job', message: JOB_FAILURE_TEXT.summary_empty! }),
    }
  }
  return { kind: 'done', job }
}
