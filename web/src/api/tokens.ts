/**
 * The JWT pair, kept in localStorage (D47).
 *
 * Read fresh on every call rather than cached in memory: two tabs share one
 * refresh-token chain, and rotation means whichever tab refreshes first
 * invalidates the other's copy. Reading from storage each time lets a tab
 * pick up the pair another tab just stored.
 */
import type { TokenPair } from './types'

const ACCESS = 'superNotes.access'
const REFRESH = 'superNotes.refresh'

function read(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    // Storage can be blocked (private mode, site data off): signed out.
    return null
  }
}

export function getAccess(): string | null {
  return read(ACCESS)
}

export function getRefresh(): string | null {
  return read(REFRESH)
}

export function setTokens(pair: TokenPair): void {
  try {
    localStorage.setItem(ACCESS, pair.access)
    localStorage.setItem(REFRESH, pair.refresh)
  } catch {
    // Nothing to do: the session lasts until the tab closes.
  }
}

export function clearTokens(): void {
  try {
    localStorage.removeItem(ACCESS)
    localStorage.removeItem(REFRESH)
  } catch {
    // As above.
  }
}

/** Fires when another tab signs in or out (the `storage` event). */
export function onTokensChangedElsewhere(handler: () => void): () => void {
  const listener = (event: StorageEvent) => {
    if (event.key === null || event.key === ACCESS || event.key === REFRESH) handler()
  }
  window.addEventListener('storage', listener)
  return () => window.removeEventListener('storage', listener)
}
