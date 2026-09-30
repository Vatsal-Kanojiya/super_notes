/**
 * Split an answer into text and citation segments.
 *
 * The answer is model output, so it is never rendered as HTML (D50): the
 * component renders each text segment as text and each citation as a button.
 * A marker is `[n]` or `[n, m]`; a number with no matching citation stays as
 * the literal text it was, so nothing the model wrote is silently dropped.
 */
import type { Citation } from '../api/types'

export type AnswerSegment = { kind: 'text'; text: string } | { kind: 'cite'; citation: Citation }

const MARKER = /\[(\d+(?:\s*,\s*\d+)*)\]/g

export function splitAnswer(answer: string, citations: Citation[]): AnswerSegment[] {
  const byN = new Map(citations.map((c) => [c.n, c]))
  const segments: AnswerSegment[] = []
  const pushText = (text: string) => {
    if (!text) return
    const last = segments[segments.length - 1]
    if (last?.kind === 'text') last.text += text
    else segments.push({ kind: 'text', text })
  }

  let at = 0
  for (const match of answer.matchAll(MARKER)) {
    const start = match.index ?? 0
    pushText(answer.slice(at, start))
    const found = match[1]!.split(',').map((s) => byN.get(Number(s.trim())))
    if (found.every((c): c is Citation => c !== undefined)) {
      for (const citation of found) segments.push({ kind: 'cite', citation })
    } else {
      pushText(match[0])
    }
    at = start + match[0].length
  }
  pushText(answer.slice(at))
  return segments
}
