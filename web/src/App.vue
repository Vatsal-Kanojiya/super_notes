<script setup lang="ts">
import { onMounted, watch } from 'vue'
import AppHeader from './components/AppHeader.vue'
import AskPanel from './components/AskPanel.vue'
import NotePage from './components/NotePage.vue'
import NotesList from './components/NotesList.vue'
import SignIn from './components/SignIn.vue'
import { useAskStore } from './stores/ask'
import { useAuthStore } from './stores/auth'
import { useNotesStore } from './stores/notes'
import { useViewStore } from './stores/view'

const auth = useAuthStore()
const notes = useNotesStore()
const ask = useAskStore()
const view = useViewStore()

// Signing in starts sync; signing out (by hand, from another tab, or because
// the session ended elsewhere) drops every trace of the account.
watch(
  () => auth.signedIn,
  (signedIn) => {
    if (signedIn) {
      notes.startAutoSync()
    } else {
      notes.clear()
      ask.clear()
      view.reset()
    }
  },
)

onMounted(() => auth.init())
</script>

<template>
  <div v-if="!auth.ready" class="splash" aria-busy="true">Super Notes</div>
  <SignIn v-else-if="!auth.signedIn" />
  <div v-else class="shell">
    <AppHeader />
    <NotePage v-if="view.screen === 'note' && view.noteId !== null" :note-id="view.noteId" />
    <AskPanel v-else-if="view.screen === 'ask'" />
    <NotesList v-else />
  </div>
</template>
