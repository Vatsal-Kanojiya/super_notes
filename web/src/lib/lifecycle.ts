/**
 * App-open detection and notice handling, apart from the browser (D93, D88).
 *
 * An app "open" is a launch (page load with a session) or the first
 * interaction after 5 hours with none. `createInteractionTracker` decides
 * that; the store wires it to real events and to the API.
 */
import type { Notice } from '../api/types'

export const IDLE_MS = 5 * 60 * 60 * 1000
export const LAST_INTERACTION_KEY = 'superNotes.lastInteractionAt'
/** The server's default timezone: a user still on it has probably never set one. */
export const DEFAULT_TIMEZONE = 'Asia/Kolkata'

/** True when the gap since the last interaction is at least 5 hours. No history, no resume. */
export function shouldResume(lastInteractionAt: number | null, now: number): boolean {
  return lastInteractionAt !== null && now - lastInteractionAt >= IDLE_MS
}

type Store = Pick<Storage, 'getItem' | 'setItem'>

function readStored(storage: Store | null): number | null {
  try {
    const value = Number(storage?.getItem(LAST_INTERACTION_KEY))
    return Number.isFinite(value) && value > 0 ? value : null
  } catch {
    return null
  }
}

/** Storage writes are rare on purpose: scrolling fires hundreds of events a second. */
const WRITE_EVERY_MS = 30_000

export interface Tracker {
  /** Record an interaction at `now`; calls `onResume` if it ends a 5 hour silence. */
  touch(): void
  /** The most recent interaction known (this tab's or another tab's). */
  last(): number | null
}

export function createInteractionTracker(options: {
  onResume: () => void
  storage: Store | null
  now?: () => number
}): Tracker {
  const now = options.now ?? Date.now
  let lastInMemory: number | null = null
  let lastWritten = 0

  const last = () => {
    // Another tab's interaction counts too: the person is still here.
    const stored = readStored(options.storage)
    return stored === null ? lastInMemory : Math.max(lastInMemory ?? 0, stored)
  }

  return {
    last,
    touch() {
      const at = now()
      const previous = last()
      lastInMemory = at
      if (at - lastWritten >= WRITE_EVERY_MS || shouldResume(previous, at)) {
        lastWritten = at
        try {
          options.storage?.setItem(LAST_INTERACTION_KEY, String(at))
        } catch {
          // storage blocked: memory alone still works within this tab
        }
      }
      if (shouldResume(previous, at)) options.onResume()
    },
  }
}

export interface SplitNotices {
  update: { required: boolean } | null
  memory: { style: 'prominent' | 'subtle'; state: 'on' | 'off' } | null
}

/** Pick the notices we know how to show out of a `session/open/` reply; ignore the rest. */
export function splitNotices(notices: Notice[] | undefined): SplitNotices {
  const result: SplitNotices = { update: null, memory: null }
  for (const notice of notices ?? []) {
    if (notice.kind === 'update') result.update = { required: notice.required === true }
    else if (notice.kind === 'memory' && notice.style && notice.state)
      result.memory = { style: notice.style, state: notice.state }
  }
  return result
}

/** The browser's timezone is worth sending only if the user is still on the default and it differs. */
export function timezoneToSend(userTimezone: string, browserTimezone: string | undefined): string | null {
  if (!browserTimezone || userTimezone !== DEFAULT_TIMEZONE || browserTimezone === userTimezone) return null
  return browserTimezone
}
