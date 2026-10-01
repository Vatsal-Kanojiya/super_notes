import { describe, expect, it } from 'vitest'
import type { AskQuery } from '../api/types'
import { applyStreamEvent, initialStream, isEnded, type StreamState } from './askStream'
import { SseParser } from './sse'

const ev = (type: string, data: object = {}) => ({ event: type, data: JSON.stringify({ type, ...data }) })
const run = (events: ReturnType<typeof ev>[], from: StreamState = initialStream()) =>
  events.reduce(applyStreamEvent, from)
const turn = { id: 4, status: 'done', answer: 'Hello world [1]', citations: [] } as unknown as AskQuery

describe('applyStreamEvent', () => {
  it('catch-up, then deltas, then done', () => {
    let s = run([ev('snapshot', { text: 'Hel', offset: 3 })])
    expect(s.text).toBe('Hel')
    s = run([ev('delta', { offset: 3, text: 'lo ' }), ev('delta', { offset: 6, text: 'world' })], s)
    expect(s.text).toBe('Hello world')
    expect(isEnded(s)).toBe(false)
    s = run([ev('done', { ask: turn })], s)
    expect(s.final).toEqual(turn)
    expect(isEnded(s)).toBe(true)
  })

  it('a snapshot replaces what we had (a reconnect), a reset clears it', () => {
    let s = run([ev('delta', { offset: 0, text: 'abc' }), ev('snapshot', { text: 'abcd', offset: 4 })])
    expect(s.text).toBe('abcd')
    s = run([ev('reset')], s)
    expect(s.text).toBe('')
  })

  it('failed carries the failed turn', () => {
    const failed = { ...turn, status: 'failed', error: 'x' } as AskQuery
    expect(run([ev('failed', { ask: failed })]).final).toEqual(failed)
  })

  it('timeout and unavailable mean fall back to polling; nothing applies after the end', () => {
    for (const kind of ['timeout', 'unavailable']) {
      const s = run([ev('delta', { offset: 0, text: 'a' }), ev(kind), ev('delta', { offset: 1, text: 'b' })])
      expect(s.fallback).toBe(true)
      expect(s.text).toBe('a')
    }
  })

  it('a done without a usable turn falls back; junk is ignored', () => {
    expect(run([ev('done')]).fallback).toBe(true)
    const s = run([{ event: 'delta', data: 'not json' }, ev('mystery'), ev('delta', { text: 5 })])
    expect(s).toEqual(initialStream())
  })

  it('works end to end through the parser, with heartbeats and split chunks', () => {
    const wire =
      'event: snapshot\ndata: {"type":"snapshot","text":"A","offset":1}\n\n: keep-alive\n\n' +
      'event: delta\ndata: {"type":"delta","offset":1,"text":"é b"}\n\n' +
      `event: done\ndata: ${JSON.stringify({ type: 'done', ask: turn })}\n\n`
    const parser = new SseParser()
    let s = initialStream()
    for (let i = 0; i < wire.length; i += 7) {
      for (const e of parser.feed(wire.slice(i, i + 7))) s = applyStreamEvent(s, e)
    }
    expect(s.final?.id).toBe(4)
    expect(s.text).toBe('Aé b')
  })
})
