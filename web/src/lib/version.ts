/**
 * Build ids and the "should this tab update?" decision (D89).
 *
 * A build id is `YYYYMMDDHHMM-<shortsha>`. Ids compare by their timestamp
 * prefix only: the sha is there to tell builds apart for people, not to order
 * them. Anything that does not parse (a dev server, an empty setting) has no
 * order, so it never triggers an update.
 */

/** The timestamp prefix as a number, or null when the id is not a build id. */
export function buildTime(id: string | null | undefined): number | null {
  const match = /^(\d{12})(?:-|$)/.exec(id ?? '')
  return match ? Number(match[1]) : null
}

/** Negative if `a` is older than `b`, 0 if equal, positive if newer; null if either is unordered. */
export function compareBuilds(a: string | null | undefined, b: string | null | undefined): number | null {
  const x = buildTime(a)
  const y = buildTime(b)
  return x === null || y === null ? null : x - y
}

export type UpdateAction =
  /** Nothing to do. */
  | 'none'
  /** Show the "New version, reload" bar; the person chooses (or an unsaved edit must finish first). */
  | 'prompt'
  /** Reload now. */
  | 'reload'

export interface UpdateInput {
  /** This tab's build id. */
  current: string
  /** The newest build the server has announced. */
  latest?: string | null
  /** The oldest build the server still accepts. */
  minSupported?: string | null
  /** A note edit has not reached the server: reloading now would lose it. */
  unsaved: boolean
  /** We already reloaded for this target and are still behind: do not loop. */
  alreadyReloaded?: boolean
}

/**
 * Newer build -> the bar, and a reload when nothing is unsaved. Below the
 * minimum -> the same, but the bar is not optional (the caller shows it as
 * required). An unsaved edit always waits: it is saved first, then this is
 * asked again. After one reload that did not help, only the bar remains.
 */
export function decideUpdate(input: UpdateInput): UpdateAction {
  const behindLatest = (compareBuilds(input.current, input.latest) ?? 0) < 0
  const belowMin = (compareBuilds(input.current, input.minSupported) ?? 0) < 0
  if (!behindLatest && !belowMin) return 'none'
  if (input.unsaved || input.alreadyReloaded) return 'prompt'
  return 'reload'
}

/** True when this tab is below the minimum the server accepts. */
export function isBelowMinimum(current: string, minSupported: string | null | undefined): boolean {
  return (compareBuilds(current, minSupported) ?? 0) < 0
}

const RELOAD_GUARD_MS = 60_000

/**
 * Reload once for a failed chunk load, never in a loop: a second failure
 * within a minute of the last reload is left to surface as an ordinary error.
 * With storage unavailable it does not reload at all. Returns true if it reloaded.
 */
export function reloadOnce(
  key: string,
  storage: Pick<Storage, 'getItem' | 'setItem'> | null,
  reload: () => void,
  now = Date.now(),
): boolean {
  if (!storage) return false
  try {
    const last = Number(storage.getItem(key) ?? 0)
    if (last && now - last < RELOAD_GUARD_MS) return false
    storage.setItem(key, String(now))
  } catch {
    // Storage blocked: the guard cannot work, so do not risk a reload loop.
    return false
  }
  reload()
  return true
}
