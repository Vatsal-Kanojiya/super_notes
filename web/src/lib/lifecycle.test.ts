import { describe, expect, it, vi } from 'vitest'
import {
  createInteractionTracker,
  IDLE_MS,
  LAST_INTERACTION_KEY,
  shouldResume,
  splitNotices,
  timezoneToSend,
} from './lifecycle'

const HOUR = 60 * 60 * 1000

describe('shouldResume', () => {
  it('is false with no history (the launch covers that)', () => {
    expect(shouldResume(null, 1_000_000)).toBe(false)
  })
  it('is false just under 5 hours, true at 5 hours', () => {
    expect(shouldResume(0, IDLE_MS - 1)).toBe(false)
    expect(shouldResume(0, IDLE_MS)).toBe(true)
  })
})

function memory(initial?: string) {
  const data = new Map<string, string>(initial ? [[LAST_INTERACTION_KEY, initial]] : [])
  return { data, getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => void data.set(k, v) }
}

describe('interaction tracker', () => {
  it('does not resume on steady use', () => {
    let t = 1_000_000
    const onResume = vi.fn()
    const tracker = createInteractionTracker({ onResume, storage: memory(), now: () => t })
    for (let i = 0; i < 10; i++) {
      tracker.touch()
      t += HOUR
    }
    expect(onResume).not.toHaveBeenCalled()
  })

  it('resumes once on the first interaction after 5 idle hours, not on the next', () => {
    let t = 1_000_000
    const onResume = vi.fn()
    const tracker = createInteractionTracker({ onResume, storage: memory(), now: () => t })
    tracker.touch()
    t += 5 * HOUR
    tracker.touch()
    tracker.touch()
    expect(onResume).toHaveBeenCalledTimes(1)
  })

  it('a window left open for months reports each new day of use', () => {
    let t = 1_000_000
    const onResume = vi.fn()
    const tracker = createInteractionTracker({ onResume, storage: memory(), now: () => t })
    tracker.touch()
    for (let day = 0; day < 3; day++) {
      t += 24 * HOUR
      tracker.touch()
      t += 1000
      tracker.touch()
    }
    expect(onResume).toHaveBeenCalledTimes(3)
  })

  it('uses the stored time, so a reload does not forget a long silence', () => {
    const onResume = vi.fn()
    const tracker = createInteractionTracker({
      onResume,
      storage: memory(String(1_000_000)),
      now: () => 1_000_000 + 6 * HOUR,
    })
    tracker.touch()
    expect(onResume).toHaveBeenCalledTimes(1)
  })

  it("another tab's recent interaction counts as activity", () => {
    let t = 1_000_000
    const storage = memory()
    const onResume = vi.fn()
    const tracker = createInteractionTracker({ onResume, storage, now: () => t })
    tracker.touch()
    t += 6 * HOUR
    storage.data.set(LAST_INTERACTION_KEY, String(t - 1000)) // other tab, a second ago
    tracker.touch()
    expect(onResume).not.toHaveBeenCalled()
  })

  it('writes to storage sparingly but records the resume moment', () => {
    let t = 1_000_000
    const storage = memory()
    const setItem = vi.spyOn(storage, 'setItem')
    const tracker = createInteractionTracker({ onResume: () => undefined, storage, now: () => t })
    for (let i = 0; i < 50; i++) {
      tracker.touch()
      t += 100
    }
    expect(setItem.mock.calls.length).toBeLessThanOrEqual(2)
    t += 6 * HOUR
    tracker.touch()
    expect(storage.data.get(LAST_INTERACTION_KEY)).toBe(String(t))
  })

  it('works with storage blocked', () => {
    let t = 1_000_000
    const broken = {
      getItem: () => {
        throw new Error('blocked')
      },
      setItem: () => {
        throw new Error('blocked')
      },
    }
    const onResume = vi.fn()
    const tracker = createInteractionTracker({ onResume, storage: broken, now: () => t })
    tracker.touch()
    t += 6 * HOUR
    tracker.touch()
    expect(onResume).toHaveBeenCalledTimes(1)
  })
})

describe('splitNotices', () => {
  it('picks out the update and memory notices', () => {
    expect(
      splitNotices([
        { kind: 'update', required: true },
        { kind: 'memory', style: 'prominent', state: 'on' },
      ]),
    ).toEqual({ update: { required: true }, memory: { style: 'prominent', state: 'on' } })
  })
  it('an update without `required` is not required', () => {
    expect(splitNotices([{ kind: 'update' }]).update).toEqual({ required: false })
  })
  it('ignores a malformed memory notice and handles none', () => {
    expect(splitNotices([{ kind: 'memory' }]).memory).toBeNull()
    expect(splitNotices([])).toEqual({ update: null, memory: null })
    expect(splitNotices(undefined)).toEqual({ update: null, memory: null })
  })
})

describe('timezoneToSend', () => {
  it('sends the browser zone only to a user still on the default', () => {
    expect(timezoneToSend('Asia/Kolkata', 'Europe/Berlin')).toBe('Europe/Berlin')
    expect(timezoneToSend('Asia/Kolkata', 'Asia/Kolkata')).toBeNull()
    expect(timezoneToSend('America/New_York', 'Europe/Berlin')).toBeNull()
    expect(timezoneToSend('Asia/Kolkata', undefined)).toBeNull()
  })
})
