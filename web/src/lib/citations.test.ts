import { describe, expect, it } from 'vitest'
import type { Citation } from '../api/types'
import { splitAnswer } from './citations'

const cite = (n: number): Citation => ({ n, note_id: n * 10, chunk_id: n * 100, title: `T${n}`, snippet: '' })

describe('splitAnswer', () => {
  it('returns plain text as one segment', () => {
    expect(splitAnswer('Nothing cited.', [])).toEqual([{ kind: 'text', text: 'Nothing cited.' }])
  })

  it('returns no segments for an empty answer', () => {
    expect(splitAnswer('', [cite(1)])).toEqual([])
  })

  it('turns a known marker into a citation chip between text', () => {
    expect(splitAnswer('Milk is due [1] today.', [cite(1)])).toEqual([
      { kind: 'text', text: 'Milk is due ' },
      { kind: 'cite', citation: cite(1) },
      { kind: 'text', text: ' today.' },
    ])
  })

  it('splits a grouped marker [1, 2] into one chip each', () => {
    const segments = splitAnswer('See [1, 2].', [cite(1), cite(2)])
    expect(segments.map((s) => s.kind)).toEqual(['text', 'cite', 'cite', 'text'])
  })

  it('keeps a marker with no matching citation as literal text, merged with its neighbours', () => {
    expect(splitAnswer('A [3] b', [cite(1)])).toEqual([{ kind: 'text', text: 'A [3] b' }])
  })

  it('keeps a grouped marker literal if any number is unknown', () => {
    expect(splitAnswer('A [1, 9] b', [cite(1)])).toEqual([{ kind: 'text', text: 'A [1, 9] b' }])
  })

  it('never produces markup: html in the answer stays text', () => {
    expect(splitAnswer('<b>x</b> [1]', [cite(1)])[0]).toEqual({ kind: 'text', text: '<b>x</b> ' })
  })
})
