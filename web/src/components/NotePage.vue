<script setup lang="ts">
/**
 * Loads the note (it may not be local yet, say from a citation) and then
 * hands a fixed first copy to the editor, which follows the store from there.
 */
import { ref, watch } from 'vue'
import { ApiError, errorMessage } from '../api/client'
import type { Id, Note } from '../api/types'
import { useNotesStore } from '../stores/notes'
import { useViewStore } from '../stores/view'
import NoteEditor from './NoteEditor.vue'

const props = defineProps<{ noteId: Id }>()
const notes = useNotesStore()
const view = useViewStore()
const initial = ref<Note | null>(null)
const error = ref('')

watch(
  () => props.noteId,
  async (id) => {
    initial.value = null
    error.value = ''
    try {
      const note = await notes.ensure(id)
      if (id === props.noteId) initial.value = note
    } catch (e) {
      error.value =
        e instanceof ApiError && e.status === 404 ? 'This note no longer exists.' : errorMessage(e)
    }
  },
  { immediate: true },
)
</script>

<template>
  <NoteEditor v-if="initial" :key="initial.id" :initial="initial" />
  <main v-else class="page">
    <button type="button" class="link" @click="view.back()">← Back</button>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-else class="muted">Opening…</p>
  </main>
</template>
