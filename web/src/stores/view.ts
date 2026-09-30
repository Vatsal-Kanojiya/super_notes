/**
 * Which screen is showing: the notes list, one note, or the Ask panel.
 *
 * Three screens do not need a router (D46). This store is the router: one
 * string, the open note's id, and where "back" from a note goes (a citation
 * opened from an answer goes back to the answer).
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { Id } from '../api/types'

export type Screen = 'list' | 'note' | 'ask'

export const useViewStore = defineStore('view', () => {
  const screen = ref<Screen>('list')
  const noteId = ref<Id | null>(null)
  const returnTo = ref<'list' | 'ask'>('list')

  function showList() {
    screen.value = 'list'
    noteId.value = null
  }

  function showAsk() {
    screen.value = 'ask'
    noteId.value = null
  }

  function openNote(id: Id) {
    returnTo.value = screen.value === 'ask' ? 'ask' : 'list'
    noteId.value = id
    screen.value = 'note'
  }

  function back() {
    if (returnTo.value === 'ask') showAsk()
    else showList()
  }

  function reset() {
    returnTo.value = 'list'
    showList()
  }

  return { screen, noteId, returnTo, showList, showAsk, openNote, back, reset }
})
