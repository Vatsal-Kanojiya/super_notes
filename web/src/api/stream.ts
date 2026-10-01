/**
 * Open an ask's answer stream: `GET ask/<id>/stream/` with `fetch`, so the
 * bearer token goes in the Authorization header and never in the URL (which
 * `EventSource` cannot do). Resolves to the response only when it is a 200
 * with a readable body; anything else (404, 429 `too_many_streams`, a
 * network failure, a 401) resolves to null, and the caller polls instead.
 * Aborting `signal` closes the connection, which frees the server's slot.
 */
import { API_BASE_URL } from './client'
import { getAccess } from './tokens'
import type { Id } from './types'

export async function openAskStream(id: Id, signal: AbortSignal): Promise<ReadableStream<Uint8Array> | null> {
  const token = getAccess()
  if (!token) return null
  try {
    const response = await fetch(`${API_BASE_URL}/ask/${id}/stream/`, {
      headers: { Accept: 'text/event-stream', Authorization: `Bearer ${token}` },
      signal,
    })
    return response.ok && response.body ? response.body : null
  } catch {
    return null
  }
}
