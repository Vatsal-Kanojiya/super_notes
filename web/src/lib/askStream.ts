/**
 * The state of one streamed answer (events as assistant/stream.py sends them,
 * D370-D377), apart from Vue and the network.
 *
 * `snapshot` replaces the text (the catch-up from the row), `delta` appends
 * (the server sends them contiguous, D371, so no offset check is needed),
 * `reset` clears it (a retry started over). `done`/`failed` carry the row as
 * `GET ask/<id>/` returns it and end the stream. `timeout`/`unavailable`
 * (and anything unreadable) mean: carry on by polling.
 */
import type { AskQuery } from '../api/types'
import type { SseEvent } from './sse'

export interface StreamState {
  text: string
  /** The finished turn, once `done` or `failed` arrived. */
  final: AskQuery | null
  /** The stream ended without a final turn: poll instead. */
  fallback: boolean
}

export const initialStream = (): StreamState => ({ text: '', final: null, fallback: false })

export function isEnded(state: StreamState): boolean {
  return state.final !== null || state.fallback
}

export function applyStreamEvent(state: StreamState, event: SseEvent): StreamState {
  if (isEnded(state)) return state
  let body: Record<string, unknown>
  try {
    const parsed: unknown = JSON.parse(event.data)
    if (!parsed || typeof parsed !== 'object') return state
    body = parsed as Record<string, unknown>
  } catch {
    return state
  }
  const kind = typeof body.type === 'string' ? body.type : event.event
  switch (kind) {
    case 'snapshot':
      return typeof body.text === 'string' ? { ...state, text: body.text } : state
    case 'delta':
      return typeof body.text === 'string' ? { ...state, text: state.text + body.text } : state
    case 'reset':
      return { ...state, text: '' }
    case 'done':
    case 'failed': {
      const ask = body.ask as AskQuery | undefined
      return ask && typeof ask === 'object' && typeof ask.id === 'number'
        ? { ...state, final: ask }
        : { ...state, fallback: true }
    }
    case 'timeout':
    case 'unavailable':
      return { ...state, fallback: true }
    default:
      return state
  }
}
