import { describe, expect, it } from 'vitest'
import type { Reminder } from '../api/types'
import {
  addDays,
  buildReminderRequest,
  dayOf,
  defaultDueInput,
  describeReminder,
  diffReminderRequest,
  fromLocalInput,
  isDay,
  occurrenceTimes,
  safeTimeZone,
  startOfDay,
  toLocalInput,
  wallToInstant,
  weekdayOf,
} from './schedule'

const LONDON = 'Europe/London'
const KOLKATA = 'Asia/Kolkata'
const iso = (ms: number) => new Date(ms).toISOString()

const reminder = (over: Partial<Reminder> = {}): Reminder => ({
  id: 1,
  note: 10,
  due_at: '2026-10-27T09:00:00Z', // 09:00 GMT in London (clocks went back on 25 Oct)
  lead_days: 7,
  channels: ['email', 'push'],
  status: 'scheduled',
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
  ...over,
})

describe('days', () => {
  it('adds days across month and year ends', () => {
    expect(addDays('2026-10-31', 1)).toBe('2026-11-01')
    expect(addDays('2026-01-01', -1)).toBe('2025-12-31')
    expect(addDays('2028-02-28', 1)).toBe('2028-02-29')
  })
  it('knows the weekday', () => {
    expect(weekdayOf('2026-10-27')).toBe(2) // Tuesday
  })
  it('validates a day', () => {
    expect(isDay('2026-02-29')).toBe(false)
    expect(isDay('2026-10-01')).toBe(true)
    expect(isDay('2026-1-1')).toBe(false)
    expect(isDay(undefined)).toBe(false)
  })
  it('names the day an instant falls on in a zone, not in UTC', () => {
    // 22:00 UTC on the 1st is already the 2nd in Kolkata (+05:30), still the 1st in London.
    expect(dayOf('2026-10-01T22:00:00Z', KOLKATA)).toBe('2026-10-02')
    expect(dayOf('2026-10-01T22:00:00Z', LONDON)).toBe('2026-10-01')
  })
})

describe('wallToInstant', () => {
  it('reads a wall time in a zone with no DST', () => {
    expect(iso(wallToInstant({ year: 2026, month: 10, day: 1, hour: 9, minute: 0, second: 0 }, KOLKATA))).toBe(
      '2026-10-01T03:30:00.000Z',
    )
  })
  it('uses BST before the October change and GMT after it', () => {
    const at = (day: number) => iso(wallToInstant({ year: 2026, month: 10, day, hour: 9, minute: 0, second: 0 }, LONDON))
    expect(at(24)).toBe('2026-10-24T08:00:00.000Z')
    expect(at(26)).toBe('2026-10-26T09:00:00.000Z')
  })
  it('takes the first of a repeated time (clocks go back, 01:30 twice on 25 Oct 2026)', () => {
    expect(iso(wallToInstant({ year: 2026, month: 10, day: 25, hour: 1, minute: 30, second: 0 }, LONDON))).toBe(
      '2026-10-25T00:30:00.000Z', // the BST one
    )
  })
  it('reads a skipped time with the offset before the change (01:30 on 29 Mar 2026 lands as 02:30 BST)', () => {
    expect(iso(wallToInstant({ year: 2026, month: 3, day: 29, hour: 1, minute: 30, second: 0 }, LONDON))).toBe(
      '2026-03-29T01:30:00.000Z',
    )
  })
  it('starts a day at local midnight', () => {
    expect(iso(startOfDay('2026-10-25', LONDON))).toBe('2026-10-24T23:00:00.000Z') // BST until 02:00
    expect(iso(startOfDay('2026-10-26', LONDON))).toBe('2026-10-26T00:00:00.000Z')
  })
})

describe('occurrenceTimes (mirrors notes/schedule.py)', () => {
  it('lead 7 gives 8 daily notifications ending at the due time', () => {
    const times = occurrenceTimes('2026-10-27T09:00:00Z', 7, LONDON)
    expect(times).toHaveLength(8)
    expect(iso(times[7]!)).toBe('2026-10-27T09:00:00.000Z')
  })
  it('keeps 09:00 local across the October change: the UTC time moves by an hour', () => {
    const times = occurrenceTimes('2026-10-27T09:00:00Z', 7, LONDON).map(iso)
    expect(times[0]).toBe('2026-10-20T08:00:00.000Z') // 09:00 BST
    expect(times[4]).toBe('2026-10-24T08:00:00.000Z') // 09:00 BST, the day before the change
    expect(times[5]).toBe('2026-10-25T09:00:00.000Z') // 09:00 GMT
  })
  it('lead 0 is the one due-time notification', () => {
    expect(occurrenceTimes('2026-10-27T09:00:00Z', 0, LONDON).map(iso)).toEqual(['2026-10-27T09:00:00.000Z'])
  })
  it('is empty for an unreadable date', () => {
    expect(occurrenceTimes('nope', 3, LONDON)).toEqual([])
  })
})

describe('describeReminder', () => {
  const now = new Date('2026-10-22T12:00:00Z').getTime()

  it('summarises a scheduled reminder with its next notification', () => {
    const s = describeReminder(reminder(), LONDON, now, 'en-GB')
    expect(s.state).toBe('upcoming')
    expect(s.count).toBe(8)
    expect(s.dueText).toContain('27 Oct 2026')
    expect(s.dueText).toContain('09:00')
    expect(s.scheduleText).toContain('8 notifications')
    expect(s.scheduleText).toContain('20 Oct 2026')
    // 22 Oct 12:00 UTC is past that day's 09:00 BST, so the next is the 23rd.
    expect(s.nextAt).toBe('2026-10-23T08:00:00.000Z')
    expect(s.nextText).toContain('23 Oct 2026')
    expect(s.channelsText).toBe('Email, Push')
  })
  it('lead 0 says one notification', () => {
    const s = describeReminder(reminder({ lead_days: 0 }), LONDON, now, 'en-GB')
    expect(s.count).toBe(1)
    expect(s.scheduleText).toBe('One notification, at the due time')
  })
  it('is "past" once every notification is behind us', () => {
    const s = describeReminder(reminder(), LONDON, new Date('2026-10-28T00:00:00Z').getTime(), 'en-GB')
    expect(s.state).toBe('past')
    expect(s.nextAt).toBeNull()
    expect(s.nextText).toBe('')
  })
  it('a done or cancelled reminder has no next notification', () => {
    expect(describeReminder(reminder({ status: 'done' }), LONDON, now, 'en-GB')).toMatchObject({ state: 'done', nextAt: null })
    expect(describeReminder(reminder({ status: 'cancelled' }), LONDON, now, 'en-GB').state).toBe('cancelled')
  })
  it('shows times in the account zone, not the browser zone', () => {
    const s = describeReminder(reminder({ due_at: '2026-10-27T03:30:00Z', lead_days: 0 }), KOLKATA, now, 'en-GB')
    expect(s.dueText).toContain('09:00')
  })
})

describe('the due-time input', () => {
  it('round-trips through the account zone', () => {
    const at = fromLocalInput('2026-10-27T09:00', LONDON)
    expect(at).toBe('2026-10-27T09:00:00.000Z')
    expect(toLocalInput(at!, LONDON)).toBe('2026-10-27T09:00')
    expect(fromLocalInput('2026-10-27T09:00', KOLKATA)).toBe('2026-10-27T03:30:00.000Z')
  })
  it('refuses malformed values', () => {
    expect(fromLocalInput('', LONDON)).toBeNull()
    expect(fromLocalInput('2026-02-30T09:00', LONDON)).toBeNull()
    expect(fromLocalInput('2026-10-27T25:00', LONDON)).toBeNull()
    expect(fromLocalInput('27/10/2026 09:00', LONDON)).toBeNull()
  })
  it('defaults to tomorrow 09:00 in the zone', () => {
    // 22:30 UTC on 1 Oct is 04:00 on the 2nd in Kolkata (tomorrow: the 3rd) but 23:30 BST on the 1st in London.
    const now = new Date('2026-10-01T22:30:00Z').getTime()
    expect(defaultDueInput(now, KOLKATA)).toBe('2026-10-03T09:00')
    expect(defaultDueInput(now, LONDON)).toBe('2026-10-02T09:00')
  })
})

describe('buildReminderRequest', () => {
  const now = new Date('2026-10-01T12:00:00Z').getTime()
  const form = { due: '2026-10-05T09:00', leadDays: 7, channels: ['email'] as ('email' | 'push')[] }

  it('builds the body with an instant carrying its offset', () => {
    expect(buildReminderRequest(form, KOLKATA, now)).toEqual({
      ok: true,
      body: { due_at: '2026-10-05T03:30:00.000Z', lead_days: 7, channels: ['email'] },
    })
  })
  it('accepts a lead typed as text', () => {
    expect(buildReminderRequest({ ...form, leadDays: '3' }, KOLKATA, now)).toMatchObject({ ok: true, body: { lead_days: 3 } })
  })
  it('refuses a past due time, but not an unchanged one when editing', () => {
    const past = { ...form, due: '2026-09-30T09:00' }
    expect(buildReminderRequest(past, KOLKATA, now)).toMatchObject({ ok: false })
    expect(buildReminderRequest(past, KOLKATA, now, '2026-09-30T03:30:00Z')).toMatchObject({ ok: true })
  })
  it('refuses a bad lead and no channel', () => {
    for (const leadDays of [-1, 31, 1.5, '', 'x']) {
      expect(buildReminderRequest({ ...form, leadDays }, KOLKATA, now)).toMatchObject({ ok: false })
    }
    expect(buildReminderRequest({ ...form, channels: [] }, KOLKATA, now)).toMatchObject({ ok: false })
    expect(buildReminderRequest({ ...form, due: '' }, KOLKATA, now)).toMatchObject({ ok: false })
  })
  it('allows the lead bounds 0 and 30', () => {
    expect(buildReminderRequest({ ...form, leadDays: 0 }, KOLKATA, now).ok).toBe(true)
    expect(buildReminderRequest({ ...form, leadDays: 30 }, KOLKATA, now).ok).toBe(true)
  })
})

describe('an unchanged due time with seconds', () => {
  it('is not sent in the PATCH', () => {
    const current = reminder({ due_at: '2026-10-05T03:30:25Z', lead_days: 7, channels: ['email'] })
    const now = new Date('2026-10-01T12:00:00Z').getTime()
    const built = buildReminderRequest(
      { due: '2026-10-05T09:00', leadDays: 3, channels: ['email'] },
      KOLKATA,
      now,
      current.due_at,
    )
    expect(built.ok).toBe(true)
    if (built.ok) expect(diffReminderRequest(current, built.body)).toEqual({ lead_days: 3 })
  })
})

describe('diffReminderRequest', () => {
  const current = reminder({ due_at: '2026-10-27T09:00:00Z', lead_days: 7, channels: ['email', 'push'] })
  it('is null when nothing changed (channels in any order)', () => {
    expect(
      diffReminderRequest(current, { due_at: '2026-10-27T09:00:00.000Z', lead_days: 7, channels: ['push', 'email'] }),
    ).toBeNull()
  })
  it('sends only what changed', () => {
    expect(diffReminderRequest(current, { due_at: '2026-10-27T09:00:00.000Z', lead_days: 3, channels: ['email', 'push'] })).toEqual({
      lead_days: 3,
    })
    expect(diffReminderRequest(current, { due_at: '2026-10-28T09:00:00.000Z', lead_days: 7, channels: ['email'] })).toEqual({
      due_at: '2026-10-28T09:00:00.000Z',
      channels: ['email'],
    })
  })
})

describe('safeTimeZone', () => {
  it('keeps a known zone and replaces an unknown one', () => {
    expect(safeTimeZone(LONDON)).toBe(LONDON)
    expect(safeTimeZone('Mars/Olympus')).not.toBe('Mars/Olympus')
    expect(safeTimeZone(null)).toBeTruthy()
  })
})
