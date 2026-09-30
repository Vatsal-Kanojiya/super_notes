<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { errorMessage } from '../api/client'
import type { Note, NoteType } from '../api/types'
import { formatRelative } from '../lib/format'
import { useNotesStore } from '../stores/notes'
import { useViewStore } from '../stores/view'

const notes = useNotesStore()
const view = useViewStore()
const error = ref('')
const creating = ref(false)

// Debounce typing in the search box; a filter change runs at once.
let searchTimer: number | undefined
function runFilter(more = false) {
  error.value = ''
  notes.runFilter(more).catch((e) => (error.value = errorMessage(e)))
}
watch(
  () => notes.query,
  () => {
    window.clearTimeout(searchTimer)
    searchTimer = window.setTimeout(() => runFilter(), 300)
  },
)
watch(() => notes.typeFilter, () => runFilter())
onBeforeUnmount(() => window.clearTimeout(searchTimer))

async function create(type: NoteType) {
  creating.value = true
  error.value = ''
  try {
    const note = await notes.create(type)
    view.openNote(note.id)
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    creating.value = false
  }
}

function heading(note: Note): string {
  return note.title.trim() || note.content_text.trim().split('\n')[0] || 'Untitled'
}

function snippet(note: Note): string {
  const text = note.content_text.trim()
  return note.title.trim() ? text.slice(0, 140) : text.split('\n').slice(1).join(' ').slice(0, 140)
}
</script>

<template>
  <main class="page">
    <div class="toolbar">
      <input
        v-model="notes.query"
        type="search"
        class="search"
        placeholder="Search notes"
        aria-label="Search notes"
      />
      <select v-model="notes.typeFilter" aria-label="Note type">
        <option value="">All</option>
        <option value="text">Text</option>
        <option value="checklist">Checklists</option>
      </select>
    </div>
    <div class="actions">
      <button type="button" :disabled="creating" @click="create('text')">New note</button>
      <button type="button" class="secondary" :disabled="creating" @click="create('checklist')">New checklist</button>
    </div>

    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notes.syncError" class="notice small">{{ notes.syncError }}</p>

    <p v-if="!notes.loaded && !notes.isFiltered" class="muted">Loading your notes…</p>
    <p v-else-if="notes.isFiltered && notes.filtering && notes.visibleNotes.length === 0" class="muted">Searching…</p>
    <p v-else-if="notes.visibleNotes.length === 0" class="muted empty">
      {{ notes.isFiltered ? 'No notes match.' : 'No notes yet. Start one above.' }}
    </p>

    <ul class="note-list">
      <li v-for="note in notes.visibleNotes" :key="note.id">
        <button type="button" class="note-card" @click="view.openNote(note.id)">
          <span class="note-card-head">
            <span class="note-title">{{ heading(note) }}</span>
            <span v-if="note.type === 'checklist'" class="badge">Checklist</span>
          </span>
          <span v-if="snippet(note)" class="note-snippet">{{ snippet(note) }}</span>
          <span class="muted small">{{ formatRelative(note.updated_at) }}</span>
        </button>
      </li>
    </ul>

    <button
      v-if="notes.isFiltered && notes.filteredCursor"
      type="button"
      class="secondary full"
      :disabled="notes.filtering"
      @click="runFilter(true)"
    >
      Load more
    </button>
  </main>
</template>
