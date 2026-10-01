/**
 * Reminder writes. The reminders themselves live on their notes in the notes
 * store (kept in step by `notes/changes/`, which carries them); these actions
 * call the API and apply its answer to the note right away.
 */
import { defineStore } from 'pinia'
import { remindersApi } from '../api/endpoints'
import type { Id, Reminder, ReminderWriteRequest } from '../api/types'
import { useNotesStore } from './notes'

export const useRemindersStore = defineStore('reminders', () => {
  const notes = useNotesStore()

  async function add(noteId: Id, body: ReminderWriteRequest & { due_at: string }): Promise<Reminder> {
    const reminder = await remindersApi.create(noteId, body)
    notes.applyReminder(noteId, reminder.id, reminder)
    return reminder
  }

  async function edit(reminder: Reminder, body: ReminderWriteRequest): Promise<Reminder> {
    const updated = await remindersApi.update(reminder.id, body)
    notes.applyReminder(updated.note, updated.id, updated)
    return updated
  }

  async function markDone(reminder: Reminder): Promise<Reminder> {
    const updated = await remindersApi.done(reminder.id)
    notes.applyReminder(updated.note, updated.id, updated)
    return updated
  }

  async function remove(reminder: Reminder): Promise<void> {
    await remindersApi.remove(reminder.id)
    notes.applyReminder(reminder.note, reminder.id, null)
  }

  return { add, edit, markDone, remove }
})
