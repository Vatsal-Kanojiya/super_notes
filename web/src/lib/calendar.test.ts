import { describe, expect, it } from 'vitest'
import type { ReminderInRange } from '../api/types'
import {
  dayFromQuery,
  entriesByDay,
  monthGrid,
  rangeFor,
  rangeIsAllowed,
  RANGE_MAX_DAYS,
  shiftAnchor,
  titleFor,
  viewFromQuery,
  visibleDays,
  weekDays,
  weekStart,
  weekdayLabels,
} from './calendar'
import { weekdayOf } from './schedule'

const LONDON = 'Europe/London'
const KOLKATA = 'Asia/Kolkata'

describe('weeks', () => {
  it('start on Monday', () => {
    expect(weekStart('2026-10-27')).toBe('2026-10-26') // Tuesday -> Monday
    expect(weekStart('2026-10-26')).toBe('2026-10-26')
    expect(weekStart('2026-11-01')).toBe('2026-10-26') // Sunday belongs to the week before
  })
  it('have seven days', () => {
    expect(weekDays('2026-10-27')).toEqual([
      '2026-10-26', '2026-10-27', '2026-10-28', '2026-10-29', '2026-10-30', '2026-10-31', '2026-11-01',
    ])
  })
})

describe('monthGrid', () => {
  it('covers whole Monday-to-Sunday weeks around the month', () => {
    const grid = monthGrid('2026-10-15')
    expect(grid[0]).toBe('2026-09-28') // 1 Oct 2026 is a Thursday
    expect(grid[grid.length - 1]).toBe('2026-11-01')
    expect(grid).toHaveLength(35)
    expect(weekdayOf(grid[0]!)).toBe(1)
    expect(weekdayOf(grid[grid.length - 1]!)).toBe(0)
  })
  it('is four weeks when February starts on a Monday (Feb 2027)', () => {
    expect(monthGrid('2027-02-10')).toHaveLength(28)
  })
  it('is six weeks when the month needs it (Aug 2026 starts on Saturday)', () => {
    expect(monthGrid('2026-08-10')).toHaveLength(42)
  })
  it('has every day once, in order', () => {
    const grid = monthGrid('2028-02-01')
    expect(new Set(grid).size).toBe(grid.length)
    expect([...grid].sort()).toEqual(grid)
    expect(grid).toContain('2028-02-29')
  })
  it('never exceeds the API range limit', () => {
    for (let m = 1; m <= 12; m++) {
      expect(rangeIsAllowed(monthGrid(`2026-${String(m).padStart(2, '0')}-01`))).toBe(true)
    }
  })
})

describe('shiftAnchor', () => {
  it('steps weeks by 7 days', () => {
    expect(shiftAnchor('week', '2026-10-27', 1)).toBe('2026-11-03')
    expect(shiftAnchor('week', '2026-01-03', -1)).toBe('2025-12-27')
  })
  it('steps months to the 1st, never skipping one (31 Jan -> Feb, not March)', () => {
    expect(shiftAnchor('month', '2026-01-31', 1)).toBe('2026-02-01')
    expect(shiftAnchor('month', '2026-03-31', -1)).toBe('2026-02-01')
    expect(shiftAnchor('month', '2026-12-15', 1)).toBe('2027-01-01')
    expect(shiftAnchor('month', '2026-01-15', -1)).toBe('2025-12-01')
  })
})

describe('rangeFor', () => {
  it('is half-open: the start of the first day to the start of the day after the last, in the zone', () => {
    expect(rangeFor(weekDays('2026-10-27'), KOLKATA)).toEqual({
      from: '2026-10-25T18:30:00.000Z',
      to: '2026-11-01T18:30:00.000Z',
    })
  })
  it('follows DST: the week of the October change is 7 days and an hour', () => {
    const { from, to } = rangeFor(weekDays('2026-10-22'), LONDON) // Mon 19 - Sun 25 Oct
    expect(from).toBe('2026-10-18T23:00:00.000Z')
    expect(to).toBe('2026-10-26T00:00:00.000Z')
  })
  it('stays under the 62-day limit for any month view', () => {
    for (const d of ['2026-03-01', '2026-10-01', '2026-08-01']) {
      const { from, to } = rangeFor(monthGrid(d), LONDON)
      const days = (new Date(to).getTime() - new Date(from).getTime()) / 86_400_000
      expect(days).toBeLessThanOrEqual(RANGE_MAX_DAYS)
    }
  })
  it('rejects an empty or over-long set of days', () => {
    expect(rangeIsAllowed([])).toBe(false)
    expect(rangeIsAllowed(Array.from({ length: 62 }, (_, i) => String(i)))).toBe(false)
  })
})

describe('visibleDays', () => {
  it('picks the grid by view', () => {
    expect(visibleDays('week', '2026-10-27')).toHaveLength(7)
    expect(visibleDays('month', '2026-10-27')).toHaveLength(35)
  })
})

const item = (id: number, note: number, title: string, due: string, occurrences: string[], status = 'scheduled') =>
  ({
    reminder: { id, note, due_at: due, lead_days: 2, channels: ['email'], status, created_at: due, updated_at: due },
    note_title: title,
    occurrences,
  }) as unknown as ReminderInRange

describe('entriesByDay', () => {
  const days = weekDays('2026-10-27')
  const results = [
    item(2, 20, 'Passport', '2026-10-28T09:00:00Z', ['2026-10-26T09:00:00Z', '2026-10-27T09:00:00Z', '2026-10-28T09:00:00Z']),
    item(1, 10, '', '2026-10-27T08:00:00Z', ['2026-10-27T08:00:00Z'], 'done'),
  ]

  it('files each notification under its day in the zone, earliest first', () => {
    const byDay = entriesByDay(results, days, LONDON, 'en-GB')
    expect(Object.keys(byDay).sort()).toEqual(['2026-10-26', '2026-10-27', '2026-10-28'])
    expect(byDay['2026-10-27']!.map((e) => e.reminderId)).toEqual([1, 2])
    expect(byDay['2026-10-27']![0]).toMatchObject({ noteId: 10, title: 'Untitled note', time: '08:00', isDue: true, status: 'done' })
  })
  it('marks only the due-date notification as due', () => {
    const byDay = entriesByDay(results, days, LONDON, 'en-GB')
    expect(byDay['2026-10-26']![0]!.isDue).toBe(false)
    expect(byDay['2026-10-28']![0]!.isDue).toBe(true)
  })
  it('uses the zone for the day: 22:30 UTC is the next day in Kolkata', () => {
    const r = [item(3, 30, 'Late', '2026-10-27T22:30:00Z', ['2026-10-27T22:30:00Z'])]
    expect(Object.keys(entriesByDay(r, days, KOLKATA, 'en-GB'))).toEqual(['2026-10-28'])
    expect(Object.keys(entriesByDay(r, days, LONDON, 'en-GB'))).toEqual(['2026-10-27'])
  })
  it('drops notifications outside the shown days', () => {
    const r = [item(4, 40, 'Far', '2026-12-01T09:00:00Z', ['2026-12-01T09:00:00Z'])]
    expect(entriesByDay(r, days, LONDON)).toEqual({})
  })
  it('keeps two notifications of one reminder on one day apart (unique keys)', () => {
    const r = [item(5, 50, 'Twice', '2026-10-27T10:00:00Z', ['2026-10-27T08:00:00Z', '2026-10-27T10:00:00Z'])]
    const entries = entriesByDay(r, days, LONDON)['2026-10-27']!
    expect(new Set(entries.map((e) => e.key)).size).toBe(2)
  })
})

describe('titles and labels', () => {
  it('titles a month', () => {
    expect(titleFor('month', '2026-10-27', 'en-GB')).toBe('October 2026')
  })
  it('titles a week, with the year only where it changes', () => {
    expect(titleFor('week', '2026-10-27', 'en-GB')).toBe('26 Oct – 1 Nov 2026')
    expect(titleFor('week', '2026-12-30', 'en-GB')).toBe('28 Dec 2026 – 3 Jan 2027')
  })
  it('lists Monday first', () => {
    const labels = weekdayLabels('en-GB')
    expect(labels).toHaveLength(7)
    expect(labels[0]).toMatch(/^Mon/)
    expect(labels[6]).toMatch(/^Sun/)
  })
})

describe('route query', () => {
  it('reads a day or falls back', () => {
    expect(dayFromQuery('2026-10-27', '2026-01-01')).toBe('2026-10-27')
    expect(dayFromQuery(['2026-10-27'], '2026-01-01')).toBe('2026-10-27')
    expect(dayFromQuery('2026-13-45', '2026-01-01')).toBe('2026-01-01')
    expect(dayFromQuery(undefined, '2026-01-01')).toBe('2026-01-01')
  })
  it('reads a view, month by default', () => {
    expect(viewFromQuery('week')).toBe('week')
    expect(viewFromQuery('year')).toBe('month')
    expect(viewFromQuery(undefined)).toBe('month')
  })
})
