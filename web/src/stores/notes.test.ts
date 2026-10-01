import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ChangesResponse, Note, Reminder, Tombstone } from '../api/types'

const changes = vi.fn<(after: number) => Promise<ChangesResponse>>()
vi.mock('../api/endpoints', () => ({ notesApi: { changes: (after: number) => changes(after) } }))

import { useNotesStore } from './notes'

const note = (id: number, revision: number): Note =>
  ({ id, type: 'text', title: `n${id}`, version: 1, revision, deleted_at: null, content_text: '' }) as unknown as Note
const tomb = (id: number, revision: number): Tombstone => ({
  id,
  type: 'text',
  version: 2,
  revision,
  updated_at: '2026-01-01T00:00:00Z',
  deleted_at: '2026-01-01T00:00:00Z',
})

beforeEach(() => {
  setActivePinia(createPinia())
  changes.mockReset()
})

describe('notes sync', () => {
  it('first sync fetches from 0 and marks the store loaded', async () => {
    changes.mockResolvedValueOnce({ results: [note(1, 1), note(2, 2)], latest_revision: 2, has_more: false })
    const store = useNotesStore()
    await store.sync()
    expect(changes).toHaveBeenCalledWith(0)
    expect(store.visibleNotes.map((n) => n.id)).toEqual([2, 1]) // newest first
    expect(store.lastRevision).toBe(2)
    expect(store.loaded).toBe(true)
  })

  it('follows has_more straight away, resuming at latest_revision', async () => {
    changes
      .mockResolvedValueOnce({ results: [note(1, 1)], latest_revision: 1, has_more: true })
      .mockResolvedValueOnce({ results: [note(2, 2)], latest_revision: 5, has_more: false })
    const store = useNotesStore()
    await store.sync()
    expect(changes.mock.calls.map((c) => c[0])).toEqual([0, 1])
    expect(store.visibleNotes).toHaveLength(2)
    expect(store.lastRevision).toBe(5)
  })

  it('stops if the server says has_more without progress', async () => {
    changes.mockResolvedValue({ results: [], latest_revision: 0, has_more: true })
    const store = useNotesStore()
    await store.sync()
    expect(changes).toHaveBeenCalledTimes(1)
  })

  it('a tombstone removes the note', async () => {
    changes.mockResolvedValueOnce({ results: [note(1, 1), note(2, 2)], latest_revision: 2, has_more: false })
    const store = useNotesStore()
    await store.sync()
    changes.mockResolvedValueOnce({ results: [tomb(1, 3)], latest_revision: 3, has_more: false })
    await store.sync()
    expect(store.visibleNotes.map((n) => n.id)).toEqual([2])
  })

  it('a revision going backwards drops local notes and resyncs from 0', async () => {
    changes.mockResolvedValueOnce({ results: [note(1, 1), note(2, 9)], latest_revision: 9, has_more: false })
    const store = useNotesStore()
    await store.sync()
    // Restored database: the server is at 4, below our 9.
    changes
      .mockResolvedValueOnce({ results: [], latest_revision: 4, has_more: false })
      .mockResolvedValueOnce({ results: [note(1, 1), note(3, 4)], latest_revision: 4, has_more: false })
    await store.sync()
    expect(changes.mock.calls.map((c) => c[0])).toEqual([0, 9, 0])
    expect(store.visibleNotes.map((n) => n.id)).toEqual([3, 1]) // note 2 is gone
    expect(store.lastRevision).toBe(4)
  })

  it('concurrent syncs share one run', async () => {
    changes.mockResolvedValue({ results: [], latest_revision: 0, has_more: false })
    const store = useNotesStore()
    await Promise.all([store.sync(), store.sync(), store.sync()])
    expect(changes).toHaveBeenCalledTimes(1)
  })

  it('a failed sync sets the error and rethrows', async () => {
    changes.mockRejectedValueOnce(new Error('boom'))
    const store = useNotesStore()
    await expect(store.sync()).rejects.toThrow('boom')
    expect(store.syncError).toMatch(/Could not sync/)
    expect(store.loaded).toBe(false)
  })
})

describe('reminders carried by sync', () => {
  const rem = (id: number, noteId: number, due: string, status = 'scheduled') =>
    ({ id, note: noteId, due_at: due, lead_days: 7, channels: ['email'], status }) as unknown as Reminder
  const withReminders = (id: number, revision: number, reminders: Reminder[]) => ({ ...note(id, revision), reminders }) as Note

  it('sync stores the note with its reminders, and replaces them on the next change', async () => {
    changes.mockResolvedValueOnce({
      results: [withReminders(1, 1, [rem(5, 1, '2026-11-01T09:00:00Z')])],
      latest_revision: 1,
      has_more: false,
    })
    const store = useNotesStore()
    await store.sync()
    expect(store.byId[1]!.reminders!.map((r) => r.id)).toEqual([5])

    // The reminder was deleted elsewhere: the note comes again with none.
    changes.mockResolvedValueOnce({ results: [withReminders(1, 2, [])], latest_revision: 2, has_more: false })
    await store.sync()
    expect(store.byId[1]!.reminders).toEqual([])
  })

  it('a copy without reminders (a save, one note) keeps the ones held', () => {
    const store = useNotesStore()
    store.upsert(withReminders(1, 1, [rem(5, 1, '2026-11-01T09:00:00Z')]))
    store.upsert(note(1, 2)) // e.g. the PATCH response
    expect(store.byId[1]!.revision).toBe(2)
    expect(store.byId[1]!.reminders!.map((r) => r.id)).toEqual([5])
  })

  it('applyReminder adds, changes and drops a reminder on its note, in due order', () => {
    const store = useNotesStore()
    store.upsert(withReminders(1, 1, [rem(5, 1, '2026-11-01T09:00:00Z')]))
    store.applyReminder(1, 6, rem(6, 1, '2026-10-20T09:00:00Z'))
    expect(store.byId[1]!.reminders!.map((r) => r.id)).toEqual([6, 5])
    store.applyReminder(1, 6, rem(6, 1, '2026-10-20T09:00:00Z', 'done'))
    expect(store.byId[1]!.reminders!.find((r) => r.id === 6)!.status).toBe('done')
    store.applyReminder(1, 5, null)
    expect(store.byId[1]!.reminders!.map((r) => r.id)).toEqual([6])
  })

  it('a sync sent before a reminder write does not undo it', async () => {
    const store = useNotesStore()
    store.upsert(withReminders(1, 1, []))
    let release!: (r: ChangesResponse) => void
    changes.mockReturnValueOnce(new Promise((resolve) => (release = resolve)))
    const syncing = store.sync()
    store.applyReminder(1, 5, rem(5, 1, '2026-11-01T09:00:00Z'))
    release({ results: [withReminders(1, 2, [])], latest_revision: 2, has_more: false })
    await syncing
    expect(store.byId[1]!.reminders!.map((r) => r.id)).toEqual([5])
  })

  it('applyReminder on an unknown note does nothing', () => {
    const store = useNotesStore()
    store.applyReminder(99, 1, rem(1, 99, '2026-11-01T09:00:00Z'))
    expect(store.byId[99]).toBeUndefined()
  })
})
