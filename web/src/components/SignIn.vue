<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { errorMessage } from '../api/client'
import { GOOGLE_CLIENT_ID, renderGoogleButton } from '../lib/gis'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const buttonEl = ref<HTMLElement | null>(null)
const error = ref('')
const busy = ref(false)

async function onToken(idToken: string) {
  busy.value = true
  error.value = ''
  try {
    await auth.signIn(idToken)
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  if (!GOOGLE_CLIENT_ID) {
    error.value = 'VITE_GOOGLE_CLIENT_ID is not set. See web/README.md.'
    return
  }
  try {
    if (buttonEl.value) await renderGoogleButton(buttonEl.value, onToken)
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not load Google sign-in.'
  }
})
</script>

<template>
  <main class="signin">
    <div class="signin-card">
      <h1>Super Notes</h1>
      <p class="muted">Your notes, and answers from them.</p>
      <p v-if="auth.notice" class="notice">{{ auth.notice }}</p>
      <div ref="buttonEl" class="gis-button" :aria-busy="busy"></div>
      <p v-if="busy" class="muted">Signing in…</p>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </div>
  </main>
</template>
