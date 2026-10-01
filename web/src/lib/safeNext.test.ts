import { describe, expect, it } from 'vitest'
import { safeNext } from './safeNext'

describe('safeNext', () => {
  it('keeps same-site paths, with query and hash', () => {
    expect(safeNext('/notes/12')).toBe('/notes/12')
    expect(safeNext('/ask?x=1#y')).toBe('/ask?x=1#y')
  })

  it.each([
    ['//evil.example'],
    ['/\\evil.example'],
    ['https://evil.example'],
    ['javascript:alert(1)'],
    ['notes'],
    [''],
    [undefined],
    [null],
    [['/notes']],
  ])('falls back to /notes for %j', (value) => {
    expect(safeNext(value)).toBe('/notes')
  })
})
