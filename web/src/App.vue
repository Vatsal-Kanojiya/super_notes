<script setup lang="ts">
import { onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppHeader from './components/AppHeader.vue'
import MemoryBanner from './components/MemoryBanner.vue'
import UpdateBar from './components/UpdateBar.vue'
import { useAppVersionStore } from './stores/appVersion'
import { useAskStore } from './stores/ask'
import { useAuthStore } from './stores/auth'
import { useLifecycleStore } from './stores/lifecycle'
import { useNotesStore } from './stores/notes'
import { safeNext } from './lib/safeNext'

const auth = useAuthStore()
const notes = useNotesStore()
const ask = useAskStore()
const route = useRoute()
const router = useRouter()
const appVersion = useAppVersionStore()
const lifecycle = useLifecycleStore()

onMounted(() => appVersion.start())

// Signing in starts sync; signing out (by hand, from another tab, or because
// the session ended elsewhere) drops every trace of the account.
watch(
  () => auth.signedIn,
  (signedIn) => {
    if (signedIn) {
      notes.startAutoSync()
      lifecycle.begin()
    } else {
      notes.clear()
      lifecycle.end()
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
  <UpdateBar />
  <div v-if="!auth.ready" class="splash" aria-busy="true">Super Notes</div>
  <router-view v-else-if="!auth.signedIn || route.meta.public" />
  <div v-else class="shell">
    <AppHeader />
    <MemoryBanner />
    <router-view />
  </div>
</template>
