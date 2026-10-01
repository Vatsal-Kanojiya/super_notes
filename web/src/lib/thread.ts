/**
 * The logic of a chat thread, apart from Vue and the network (so it is tested
 * on its own): merging polled turns into the thread, what the thread is doing,
 * the order of the conversation list, and what to tell people when a turn is
 * refused.
 */
import type { AskQuery, Conversation, LimitUsage } from '../api/types'
import { formatDate } from './format'

// ------------------------------------------------------------------ turns --

const RANK: Record<AskQuery['status'], number> = { pending: 0, running: 1, done: 2, failed: 2 }

export function isUnfinished(turn: AskQuery): boolean {
  return turn.status === 'pending' || turn.status === 'running'
}

/** Oldest first: by position, and by id for anything without one. */
function compareTurns(a: AskQuery, b: AskQuery): number {
  if (a.position !== null && b.position !== null && a.position !== b.position) return a.position - b.position
  return a.id - b.id
}

/**
 * Put one turn into the thread: added if new, else replacing the one with its
 * id. A snapshot never moves a turn backwards (a slow poll that overtakes the
 * finished answer must not bring "pending" back). Returns a new array, oldest
 * first.
 */
export function mergeTurn(turns: readonly AskQuery[], incoming: AskQuery): AskQuery[] {
  const at = turns.findIndex((t) => t.id === incoming.id)
  if (at < 0) return [...turns, incoming].sort(compareTurns)
  if (RANK[incoming.status] < RANK[turns[at]!.status]) return [...turns]
  const next = [...turns]
  next[at] = incoming
  return next.sort(compareTurns)
}

export function mergeTurns(turns: readonly AskQuery[], incoming: readonly AskQuery[]): AskQuery[] {
  return incoming.reduce<AskQuery[]>((all, turn) => mergeTurn(all, turn), [...turns].sort(compareTurns))
}

/** The turn being answered, if any (turns are sequential, so at most one). */
export function pendingTurn(turns: readonly AskQuery[]): AskQuery | null {
  for (let i = turns.length - 1; i >= 0; i--) if (isUnfinished(turns[i]!)) return turns[i]!
  return null
}

/** The last turn, when it failed: the one to offer a retry for. */
export function failedLastTurn(turns: readonly AskQuery[]): AskQuery | null {
  const last = turns[turns.length - 1]
  return last && last.status === 'failed' ? last : null
}

export type ThreadPhase =
  | 'empty' // no turns yet
  | 'sending' // our POST is in flight
  | 'waiting' // a 409: waiting for the previous turn before asking
  | 'answering' // a turn is pending or running
  | 'failed' // the last turn failed
  | 'ready'

export function threadPhase(
  turns: readonly AskQuery[],
  flags: { sending?: boolean; waiting?: boolean } = {},
): ThreadPhase {
  if (flags.waiting) return 'waiting'
  if (flags.sending) return 'sending'
  if (pendingTurn(turns)) return 'answering'
  if (turns.length === 0) return 'empty'
  return failedLastTurn(turns) ? 'failed' : 'ready'
}

/** Whether the composer may send: turns are sequential, one at a time. */
export function canCompose(phase: ThreadPhase): boolean {
  return phase === 'empty' || phase === 'ready' || phase === 'failed'
}

// ---------------------------------------------------------- conversations --

/** Most recently active first (the server's order), ids breaking ties. */
export function sortConversations(list: readonly Conversation[]): Conversation[] {
  return [...list].sort((a, b) => {
    const byTime = Date.parse(b.updated_at) - Date.parse(a.updated_at)
    return Number.isNaN(byTime) || byTime === 0 ? b.id - a.id : byTime
  })
}

/** Add or replace one conversation, keeping the list ordered. */
export function upsertConversation(list: readonly Conversation[], conversation: Conversation): Conversation[] {
  const { id, title, created_at, updated_at } = conversation
  return sortConversations([...list.filter((c) => c.id !== id), { id, title, created_at, updated_at }])
}

/** The title to show; a conversation has none until its first turn. */
export function conversationLabel(conversation: Pick<Conversation, 'title'> | null): string {
  return conversation?.title.trim() || 'New conversation'
}

// ----------------------------------------------------------------- errors --

/** The parts of a failed request this module reads (an `ApiError` fits). */
export interface RequestFailure {
  status: number
  code: string
  detail: string
  body: unknown
}

export type TurnErrorKind = 'quota' | 'paused' | 'throttled' | 'reused' | 'network' | 'missing' | 'other'

export interface TurnError {
  kind: TurnErrorKind
  message: string
  /** Worth sending the same question again (with the same key)? */
  retrySameKey: boolean
}

function usageFromBody(body: unknown): LimitUsage | null {
  if (!body || typeof body !== 'object') return null
  const { used, limit, resets_at } = body as Record<string, unknown>
  if (typeof used !== 'number') return null
  return {
    used,
    limit: typeof limit === 'number' ? limit : null,
    resets_at: typeof resets_at === 'string' ? resets_at : null,
  }
}

/**
 * What to tell someone when sending a turn failed. `limit` is `limits.chat_turns`
 * from `me/`; a 429 body's own numbers win, being fresher.
 */
export function describeTurnFailure(failure: RequestFailure, limit: LimitUsage | null): TurnError {
  if (failure.status === 0) {
    return { kind: 'network', message: 'Could not reach the server. Send again to retry.', retrySameKey: true }
  }
  if (failure.code === 'quota_exceeded') {
    const usage = usageFromBody(failure.body) ?? limit
    const total = usage?.limit ?? null
    const resets = usage?.resets_at ? ` They reset on ${formatDate(usage.resets_at)}.` : ''
    return {
      kind: 'quota',
      message: `${total === null ? 'You have used all your chat turns for this month.' : `You have used all ${total} chat turns for this month.`}${resets}`,
      retrySameKey: false,
    }
  }
  if (failure.code === 'system_limit_reached') {
    return {
      kind: 'paused',
      message: 'Chat is paused for everyone right now. Your notes still work; try again later.',
      retrySameKey: false,
    }
  }
  if (failure.status === 429 || failure.code === 'throttled') {
    return { kind: 'throttled', message: 'You are asking too fast. Wait a moment and try again.', retrySameKey: false }
  }
  if (failure.code === 'idempotency_key_reused') {
    return {
      kind: 'reused',
      message: 'That request clashed with an earlier one. Send it again as a new question.',
      retrySameKey: false,
    }
  }
  if (failure.status === 404) {
    return { kind: 'missing', message: 'This conversation no longer exists.', retrySameKey: false }
  }
  return { kind: 'other', message: failure.detail || 'Something went wrong. Please try again.', retrySameKey: failure.status >= 500 }
}

/** A line for the composer: "12 / 100 chat turns used · resets on …", or unlimited. */
export function usageLine(limit: LimitUsage | null): string {
  if (!limit) return ''
  if (limit.limit === null) return `${limit.used} chat turns this month · unlimited`
  const resets = limit.resets_at ? ` · resets on ${formatDate(limit.resets_at)}` : ''
  return `${limit.used} / ${limit.limit} chat turns used${resets}`
}

export function overLimit(limit: LimitUsage | null): boolean {
  return limit !== null && limit.limit !== null && limit.used >= limit.limit
}
