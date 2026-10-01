<script setup lang="ts">
import { watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppHeader from './components/AppHeader.vue'
import { useAskStore } from './stores/ask'
import { useAuthStore } from './stores/auth'
import { useNotesStore } from './stores/notes'
import { safeNext } from './lib/safeNext'

const auth = useAuthStore()
const notes = useNotesStore()
const ask = useAskStore()
const route = useRoute()
const router = useRouter()

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
      // Back to sign-in, remembering where they were so signing in returns there.
      if (!route.meta.public) void router.replace({ name: 'signin', query: { next: route.fullPath } })
    }
    // Signed in (here or in another tab) while on the sign-in page: go on.
    if (signedIn && route.meta.public) void router.replace(safeNext(route.query.next))
  },
)
</script>

<template>
  <div v-if="!auth.ready" class="splash" aria-busy="true">Super Notes</div>
  <router-view v-else-if="!auth.signedIn || route.meta.public" />
  <div v-else class="shell">
    <AppHeader />
    <router-view />
  </div>
</template>
