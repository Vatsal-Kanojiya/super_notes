/**
 * Reminder schedule and wall-clock logic, in pure functions (D95, D200).
 *
 * The server owns the schedule (`notes/schedule.py`): one notification a day
 * at the due time's local time of day, from `lead_days` before the due date
 * to the due date itself. The calendar asks the server for its occurrences;
 * this module only needs the same rule to *describe* a reminder (the next
 * notification, how many there are) and to turn a time typed in the user's
 * timezone into an instant. It follows the server's two edge cases, so the
 * two never disagree: a repeated wall time (clocks go back) is the first of
 * the two, a skipped one (clocks go forward) is read with the offset in force
 * before the change, so it lands just after it.
 *
 * Nothing here reads the clock or the browser's timezone: `now` and `tz` are
 * always arguments.
 */
import type { Reminder, ReminderChannel, ReminderWriteRequest } from '../api/types'

/** A calendar day, `YYYY-MM-DD`, with no timezone attached. */
export type Day = string

export const LEAD_DAYS_MAX = 30
export const LEAD_DAYS_DEFAULT = 7
const DAY_MS = 86_400_000

interface Wall {
  year: number
  month: number // 1-12
  day: number
  hour: number
  minute: number
  second: number
}

const formatters = new Map<string, Intl.DateTimeFormat>()

function formatter(tz: string): Intl.DateTimeFormat {
  let f = formatters.get(tz)
  if (!f) {
    f = new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      hourCycle: 'h23',
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
      hour: 'numeric',
      minute: 'numeric',
      second: 'numeric',
    })
    formatters.set(tz, f)
  }
  return f
}

/** `tz` if the browser knows it, else the browser's own zone (a name can drop out of tz data). */
export function safeTimeZone(tz: string | null | undefined): string {
  if (tz) {
    try {
      formatter(tz)
      return tz
    } catch {
      // unknown name: fall through
    }
  }
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
}

/** The wall clock in `tz` at an instant (ms since the epoch). */
export function wallAt(ms: number, tz: string): Wall {
  const parts: Record<string, number> = {}
  for (const p of formatter(tz).formatToParts(new Date(ms))) {
    if (p.type !== 'literal') parts[p.type] = Number(p.value)
  }
  return {
    year: parts.year!,
    month: parts.month!,
    day: parts.day!,
    hour: parts.hour! % 24,
    minute: parts.minute!,
    second: parts.second!,
  }
}

function wallAsUtc(w: Wall): number {
  return Date.UTC(w.year, w.month - 1, w.day, w.hour, w.minute, w.second)
}

/** The zone's offset from UTC at an instant, in ms (positive east of Greenwich). */
function offsetAt(ms: number, tz: string): number {
  return wallAsUtc(wallAt(ms, tz)) - Math.floor(ms / 1000) * 1000
}

/** The instant a wall-clock time names in `tz` (see the module note for the edge cases). */
export function wallToInstant(w: Wall, tz: string): number {
  const guess = wallAsUtc(w)
  const before = offsetAt(guess - DAY_MS, tz)
  const after = offsetAt(guess + DAY_MS, tz)
  const valid = [guess - before, guess - after].filter((t) => wallAsUtc(wallAt(t, tz)) === guess)
  // Neither offset gives that wall time: it was skipped. Use the earlier offset.
  return valid.length > 0 ? Math.min(...valid) : guess - before
}

// ------------------------------------------------------------------ days --

const pad = (n: number, width = 2) => String(n).padStart(width, '0')

export function makeDay(year: number, month: number, day: number): Day {
  return `${pad(year, 4)}-${pad(month)}-${pad(day)}`
}

export function parseDay(day: Day): { year: number; month: number; day: number } {
  const [year, month, d] = day.split('-').map(Number)
  return { year: year!, month: month!, day: d! }
}

export function isDay(value: unknown): value is Day {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const { year, month, day } = parseDay(value)
  const date = new Date(Date.UTC(year, month - 1, day))
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day
}

export function addDays(day: Day, n: number): Day {
  const { year, month, day: d } = parseDay(day)
  const date = new Date(Date.UTC(year, month - 1, d + n))
  return makeDay(date.getUTCFullYear(), date.getUTCMonth() + 1, date.getUTCDate())
}

/** 0 = Sunday ... 6 = Saturday. */
export function weekdayOf(day: Day): number {
  const { year, month, day: d } = parseDay(day)
  return new Date(Date.UTC(year, month - 1, d)).getUTCDay()
}

/** The calendar day an instant falls on in `tz`. */
export function dayOf(iso: string | number, tz: string): Day {
  const w = wallAt(typeof iso === 'number' ? iso : new Date(iso).getTime(), tz)
  return makeDay(w.year, w.month, w.day)
}

/** The instant a day begins in `tz` (its first moment, as the server would read 00:00). */
export function startOfDay(day: Day, tz: string): number {
  const { year, month, day: d } = parseDay(day)
  return wallToInstant({ year, month, day: d, hour: 0, minute: 0, second: 0 }, tz)
}

// ----------------------------------------------------------- occurrences --

/** Every notification instant (ms, oldest first), as `notes/schedule.py: occurrences`. */
export function occurrenceTimes(dueAt: string, leadDays: number, tz: string): number[] {
  const due = new Date(dueAt).getTime()
  if (Number.isNaN(due)) return []
  const local = wallAt(due, tz)
  const dueDay = makeDay(local.year, local.month, local.day)
  const times: number[] = []
  for (let before = Math.max(0, leadDays); before > 0; before--) {
    const { year, month, day } = parseDay(addDays(dueDay, -before))
    times.push(wallToInstant({ ...local, year, month, day }, tz))
  }
  times.push(due)
  return times
}

// ------------------------------------------------------------ formatting --

/** `Tue 27 Oct 2026`, in `tz`. */
export function formatDay(ms: number, tz: string, locale?: string): string {
  return new Intl.DateTimeFormat(locale, {
    timeZone: tz,
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  }).format(new Date(ms))
}

/** `09:00`, in `tz`. */
export function formatTime(ms: number, tz: string, locale?: string): string {
  return new Intl.DateTimeFormat(locale, { timeZone: tz, hour: '2-digit', minute: '2-digit' }).format(new Date(ms))
}

export function formatDateTime(iso: string | number, tz: string, locale?: string): string {
  const ms = typeof iso === 'number' ? iso : new Date(iso).getTime()
  if (Number.isNaN(ms)) return ''
  return `${formatDay(ms, tz, locale)}, ${formatTime(ms, tz, locale)}`
}

// ----------------------------------------------------------- description --

export type ReminderState = 'upcoming' | 'past' | 'done' | 'cancelled'

export interface ReminderSummary {
  state: ReminderState
  /** `Tue 27 Oct 2026, 09:00` */
  dueText: string
  /** `8 notifications, daily from Tue 20 Oct 2026 at 09:00` or `One notification, at the due time`. */
  scheduleText: string
  /** How many notifications the schedule has in all. */
  count: number
  /** The next notification still to come (ISO), only for a scheduled reminder that has one. */
  nextAt: string | null
  /** `Next: Fri 2 Oct 2026, 09:00`, or ''. */
  nextText: string
  /** `Email, Push` */
  channelsText: string
}

const CHANNEL_LABEL: Record<ReminderChannel, string> = { email: 'Email', push: 'Push' }

export function describeReminder(
  reminder: Reminder,
  tz: string,
  now: number,
  locale?: string,
): ReminderSummary {
  const times = occurrenceTimes(reminder.due_at, reminder.lead_days, tz)
  const due = new Date(reminder.due_at).getTime()
  const count = times.length
  const active = reminder.status === 'scheduled'
  const upcoming = active ? times.find((t) => t > now) : undefined
  const state: ReminderState =
    reminder.status === 'done' ? 'done' : reminder.status === 'cancelled' ? 'cancelled' : upcoming === undefined ? 'past' : 'upcoming'
  const first = times[0]
  const scheduleText =
    count <= 1 || first === undefined
      ? 'One notification, at the due time'
      : `${count} notifications, daily from ${formatDay(first, tz, locale)} at ${formatTime(first, tz, locale)}`
  return {
    state,
    dueText: formatDateTime(due, tz, locale),
    scheduleText,
    count,
    nextAt: upcoming === undefined ? null : new Date(upcoming).toISOString(),
    nextText: upcoming === undefined ? '' : `Next: ${formatDateTime(upcoming, tz, locale)}`,
    channelsText: reminder.channels.map((c) => CHANNEL_LABEL[c] ?? c).join(', '),
  }
}

// ------------------------------------------------------------------ form --

/** A `datetime-local` value, `YYYY-MM-DDTHH:mm`, read as wall-clock time in `tz`. Null if malformed. */
export function fromLocalInput(value: string, tz: string): string | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value.trim())
  if (!m) return null
  const [year, month, day, hour, minute] = m.slice(1).map(Number) as [number, number, number, number, number]
  if (!isDay(makeDay(year, month, day)) || hour > 23 || minute > 59) return null
  return new Date(wallToInstant({ year, month, day, hour, minute, second: 0 }, tz)).toISOString()
}

/** The inverse: an instant as the `datetime-local` value for `tz`. */
export function toLocalInput(iso: string | number, tz: string): string {
  const ms = typeof iso === 'number' ? iso : new Date(iso).getTime()
  if (Number.isNaN(ms)) return ''
  const w = wallAt(ms, tz)
  return `${makeDay(w.year, w.month, w.day)}T${pad(w.hour)}:${pad(w.minute)}`
}

/** A new reminder's starting point: tomorrow at 09:00 in `tz`. */
export function defaultDueInput(now: number, tz: string): string {
  return `${addDays(dayOf(now, tz), 1)}T09:00`
}

export interface ReminderForm {
  /** The `datetime-local` text. */
  due: string
  leadDays: number | string
  channels: ReminderChannel[]
}

export type FormResult = { ok: true; body: ReminderWriteRequest & { due_at: string } } | { ok: false; error: string }

/** Check the form and build the request (due_at is an instant with an offset, as the API demands). */
export function buildReminderRequest(
  form: ReminderForm,
  tz: string,
  now: number,
  /** Editing: the reminder's current due time. Leaving it as it is is fine even once past. */
  currentDue?: string,
): FormResult {
  const dueAt = fromLocalInput(form.due, tz)
  if (!dueAt) return { ok: false, error: 'Pick a date and time.' }
  // The form shows minutes only: a form value equal to the current due time (seconds dropped) is untouched.
  const unchanged =
    currentDue !== undefined &&
    (form.due.trim() === toLocalInput(currentDue, tz) || new Date(currentDue).getTime() === new Date(dueAt).getTime())
  if (!unchanged && new Date(dueAt).getTime() <= now) return { ok: false, error: 'The due time must be in the future.' }
  const lead = typeof form.leadDays === 'number' ? form.leadDays : Number(String(form.leadDays).trim())
  if (!Number.isInteger(lead) || lead < 0 || lead > LEAD_DAYS_MAX || String(form.leadDays).trim() === '') {
    return { ok: false, error: `Days before must be a whole number from 0 to ${LEAD_DAYS_MAX}.` }
  }
  if (form.channels.length === 0) return { ok: false, error: 'Choose at least one way to be notified.' }
  return { ok: true, body: { due_at: unchanged ? currentDue : dueAt, lead_days: lead, channels: [...form.channels] } }
}

/**
 * The body of a PATCH: only what changed, so an untouched past due time is
 * not sent (the API refuses a due time in the past). Null when nothing did.
 */
export function diffReminderRequest(
  current: Reminder,
  next: ReminderWriteRequest & { due_at: string },
): ReminderWriteRequest | null {
  const body: ReminderWriteRequest = {}
  if (new Date(next.due_at).getTime() !== new Date(current.due_at).getTime()) body.due_at = next.due_at
  if (next.lead_days !== current.lead_days) body.lead_days = next.lead_days
  const a = [...(next.channels ?? [])].sort().join()
  const b = [...current.channels].sort().join()
  if (a !== b) body.channels = next.channels
  return Object.keys(body).length > 0 ? body : null
}
