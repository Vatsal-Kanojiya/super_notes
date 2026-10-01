import { describe, expect, it, vi } from 'vitest'
import { buildTime, compareBuilds, decideUpdate, isBelowMinimum, reloadOnce } from './version'

const OLD = '202601010000-aaaaaaa'
const MID = '202602010000-bbbbbbb'
const NEW = '202603010000-ccccccc'

describe('build ids', () => {
  it('reads the timestamp prefix', () => {
    expect(buildTime(OLD)).toBe(202601010000)
    expect(buildTime('202601010000')).toBe(202601010000)
  })

  it.each([[''], [null], [undefined], ['dev'], ['2026-abc'], ['x202601010000-a']])('%j is unordered', (id) => {
    expect(buildTime(id)).toBeNull()
  })

  it('orders by timestamp, ignoring the sha', () => {
    expect(compareBuilds(OLD, NEW)).toBeLessThan(0)
    expect(compareBuilds(NEW, OLD)).toBeGreaterThan(0)
    expect(compareBuilds('202601010000-zzzz', '202601010000-aaaa')).toBe(0)
  })

  it('has no order when either side is not a build id', () => {
    expect(compareBuilds('dev', NEW)).toBeNull()
    expect(compareBuilds(OLD, '')).toBeNull()
  })

  it('isBelowMinimum ignores an empty minimum', () => {
    expect(isBelowMinimum(OLD, '')).toBe(false)
    expect(isBelowMinimum(OLD, MID)).toBe(true)
    expect(isBelowMinimum(MID, MID)).toBe(false)
  })
})

describe('decideUpdate', () => {
  it('same build: nothing', () => {
    expect(decideUpdate({ current: NEW, latest: NEW, minSupported: '', unsaved: false })).toBe('none')
  })

  it('a client newer than the server says (rolled back): nothing', () => {
    expect(decideUpdate({ current: NEW, latest: OLD, minSupported: '', unsaved: false })).toBe('none')
  })

  it('a dev build never updates', () => {
    expect(decideUpdate({ current: 'dev', latest: NEW, minSupported: NEW, unsaved: false })).toBe('none')
  })

  it('newer build, nothing unsaved: reload', () => {
    expect(decideUpdate({ current: OLD, latest: NEW, minSupported: '', unsaved: false })).toBe('reload')
  })

  it('newer build with an unsaved edit: bar only, no reload', () => {
    expect(decideUpdate({ current: OLD, latest: NEW, minSupported: '', unsaved: true })).toBe('prompt')
  })

  it('below the minimum, nothing unsaved: reload', () => {
    expect(decideUpdate({ current: OLD, latest: NEW, minSupported: MID, unsaved: false })).toBe('reload')
  })

  it('below the minimum while unsaved: waits, never drops the edit', () => {
    expect(decideUpdate({ current: OLD, latest: NEW, minSupported: MID, unsaved: true })).toBe('prompt')
  })

  it('below the minimum even when latest is missing', () => {
    expect(decideUpdate({ current: OLD, latest: null, minSupported: MID, unsaved: false })).toBe('reload')
  })

  it('after one reload that did not help: bar, not a loop', () => {
    expect(decideUpdate({ current: OLD, latest: NEW, minSupported: MID, unsaved: false, alreadyReloaded: true })).toBe(
      'prompt',
    )
  })
})

describe('reloadOnce', () => {
  const memory = () => {
    const data = new Map<string, string>()
    return { getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => void data.set(k, v) }
  }

  it('reloads the first time, then refuses within the guard window', () => {
    const storage = memory()
    const reload = vi.fn()
    expect(reloadOnce('k', storage, reload, 1_000_000)).toBe(true)
    expect(reloadOnce('k', storage, reload, 1_010_000)).toBe(false)
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('reloads again once the window has passed', () => {
    const storage = memory()
    const reload = vi.fn()
    reloadOnce('k', storage, reload, 1_000_000)
    expect(reloadOnce('k', storage, reload, 1_000_000 + 61_000)).toBe(true)
    expect(reload).toHaveBeenCalledTimes(2)
  })

  it('does not reload when storage is unavailable (no loop guard possible)', () => {
    const reload = vi.fn()
    const broken = {
      getItem: () => {
        throw new Error('blocked')
      },
      setItem: () => undefined,
    }
    expect(reloadOnce('k', broken, reload)).toBe(false)
    expect(reload).not.toHaveBeenCalled()
  })
})
