/**
 * The calendar's grid and range logic, pure (D201).
 *
 * Days are plain `YYYY-MM-DD` strings in the user's timezone (see
 * schedule.ts): a month or week is a list of them, the API range is the
 * instant the first day begins to the instant the day after the last begins
 * (half-open, as `GET reminders/?from=&to=` reads it), and each occurrence
 * the API returns is filed under the day it falls on in that same zone.
 * Weeks start on Monday.
 */
import type { Id, ReminderInRange, ReminderStatus } from '../api/types'
import { addDays, dayOf, formatTime, isDay, makeDay, parseDay, startOfDay, weekdayOf, type Day } from './schedule'

export type CalendarView = 'month' | 'week'

/** The API refuses a longer range. */
export const RANGE_MAX_DAYS = 62

/** Monday of the week holding `day`. */
export function weekStart(day: Day): Day {
  return addDays(day, -((weekdayOf(day) + 6) % 7))
}

/** The seven days of the week holding `day`, Monday first. */
export function weekDays(day: Day): Day[] {
  const start = weekStart(day)
  return Array.from({ length: 7 }, (_, i) => addDays(start, i))
}

/** The first day of `day`'s month. */
export function monthStart(day: Day): Day {
  const { year, month } = parseDay(day)
  return makeDay(year, month, 1)
}

/** Every day of the weeks that touch `day`'s month: 28 to 42 days, Monday first. */
export function monthGrid(day: Day): Day[] {
  const { year, month } = parseDay(day)
  const first = makeDay(year, month, 1)
  const last = makeDay(year, month, new Date(Date.UTC(year, month, 0)).getUTCDate())
  const start = weekStart(first)
  const end = addDays(weekStart(last), 6)
  const days: Day[] = []
  for (let d = start; d <= end; d = addDays(d, 1)) days.push(d)
  return days
}

/** The days a view shows, anchored on `anchor`. */
export function visibleDays(view: CalendarView, anchor: Day): Day[] {
  return view === 'month' ? monthGrid(anchor) : weekDays(anchor)
}

/** Previous (-1) or next (1) month or week. A month steps to the 1st, so it never skips one. */
export function shiftAnchor(view: CalendarView, anchor: Day, direction: -1 | 1): Day {
  if (view === 'week') return addDays(anchor, 7 * direction)
  const { year, month } = parseDay(anchor)
  const date = new Date(Date.UTC(year, month - 1 + direction, 1))
  return makeDay(date.getUTCFullYear(), date.getUTCMonth() + 1, 1)
}

/** The half-open range to ask the API for, as ISO instants. */
export function rangeFor(days: Day[], tz: string): { from: string; to: string } {
  const first = days[0]!
  const last = days[days.length - 1]!
  return {
    from: new Date(startOfDay(first, tz)).toISOString(),
    to: new Date(startOfDay(addDays(last, 1), tz)).toISOString(),
  }
}

/** Whole days only; one day of slack covers a DST change making a day 23 or 25 hours long. */
export function rangeIsAllowed(days: Day[]): boolean {
  return days.length > 0 && days.length < RANGE_MAX_DAYS
}

/** One notification on the calendar. */
export interface CalendarEntry {
  /** Unique on the page. */
  key: string
  reminderId: Id
  noteId: Id
  title: string
  /** The instant, ISO. */
  at: string
  /** `09:00` in the user's zone. */
  time: string
  /** The last notification, on the due date itself. */
  isDue: boolean
  status: ReminderStatus
}

/** File every occurrence under its day, each day oldest first. Only days in `days` are kept. */
export function entriesByDay(
  results: ReminderInRange[],
  days: Day[],
  tz: string,
  locale?: string,
): Record<Day, CalendarEntry[]> {
  const shown = new Set(days)
  const byDay: Record<Day, CalendarEntry[]> = {}
  for (const item of results) {
    const dueMs = new Date(item.reminder.due_at).getTime()
    for (const at of item.occurrences) {
      const ms = new Date(at).getTime()
      const day = dayOf(ms, tz)
      if (!shown.has(day)) continue
      ;(byDay[day] ??= []).push({
        key: `${item.reminder.id}@${at}`,
        reminderId: item.reminder.id,
        noteId: item.reminder.note,
        title: item.note_title.trim() || 'Untitled note',
        at,
        time: formatTime(ms, tz, locale),
        isDue: ms === dueMs,
        status: item.reminder.status,
      })
    }
  }
  for (const entries of Object.values(byDay)) {
    entries.sort((a, b) => new Date(a.at).getTime() - new Date(b.at).getTime() || a.reminderId - b.reminderId)
  }
  return byDay
}

/** The heading: `October 2026`, or `28 Sep – 4 Oct 2026` for a week. */
export function titleFor(view: CalendarView, anchor: Day, locale?: string): string {
  const utc = (day: Day) => {
    const { year, month, day: d } = parseDay(day)
    return new Date(Date.UTC(year, month - 1, d))
  }
  const fmt = (options: Intl.DateTimeFormatOptions) =>
    new Intl.DateTimeFormat(locale, { timeZone: 'UTC', ...options })
  if (view === 'month') return fmt({ month: 'long', year: 'numeric' }).format(utc(anchor))
  const days = weekDays(anchor)
  const first = utc(days[0]!)
  const last = utc(days[6]!)
  const sameYear = first.getUTCFullYear() === last.getUTCFullYear()
  const start = fmt(sameYear ? { day: 'numeric', month: 'short' } : { day: 'numeric', month: 'short', year: 'numeric' })
  const end = fmt({ day: 'numeric', month: 'short', year: 'numeric' })
  return `${start.format(first)} – ${end.format(last)}`
}

/** `Mon` ... `Sun`, for the column heads. */
export function weekdayLabels(locale?: string): string[] {
  const monday = Date.UTC(2024, 0, 1) // a Monday
  return Array.from({ length: 7 }, (_, i) =>
    new Intl.DateTimeFormat(locale, { timeZone: 'UTC', weekday: 'short' }).format(new Date(monday + i * 86_400_000)),
  )
}

/** A route query value as a day, or `fallback` when it is missing or malformed. */
export function dayFromQuery(value: unknown, fallback: Day): Day {
  const v = Array.isArray(value) ? value[0] : value
  return isDay(v) ? v : fallback
}

export function viewFromQuery(value: unknown): CalendarView {
  const v = Array.isArray(value) ? value[0] : value
  return v === 'week' ? 'week' : 'month'
}
