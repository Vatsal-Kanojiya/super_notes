<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useViewStore } from '../stores/view'
import DevicesList from './DevicesList.vue'

const auth = useAuthStore()
const view = useViewStore()
const menuOpen = ref(false)
const menuEl = ref<HTMLElement | null>(null)

function onDocumentClick(event: MouseEvent) {
  if (menuOpen.value && menuEl.value && !menuEl.value.contains(event.target as Node)) menuOpen.value = false
}

function onKey(event: KeyboardEvent) {
  if (event.key === 'Escape') menuOpen.value = false
}

onMounted(() => {
  document.addEventListener('click', onDocumentClick)
  document.addEventListener('keydown', onKey)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocumentClick)
  document.removeEventListener('keydown', onKey)
})
</script>

<template>
  <header class="app-header">
    <span class="brand">Super Notes</span>
    <nav class="tabs" aria-label="Sections">
      <button
        type="button"
        :class="{ active: view.screen !== 'ask' }"
        :aria-current="view.screen !== 'ask' ? 'page' : undefined"
        @click="view.showList()"
      >
        Notes
      </button>
      <button
        type="button"
        :class="{ active: view.screen === 'ask' }"
        :aria-current="view.screen === 'ask' ? 'page' : undefined"
        @click="view.showAsk()"
      >
        Ask
      </button>
    </nav>
    <div ref="menuEl" class="account">
      <button
        type="button"
        class="avatar-button"
        :aria-expanded="menuOpen"
        aria-haspopup="true"
        aria-label="Account"
        @click="menuOpen = !menuOpen"
      >
        <img
          v-if="auth.user?.avatar_url"
          :src="auth.user.avatar_url"
          alt=""
          class="avatar"
          referrerpolicy="no-referrer"
        />
        <span v-else class="avatar avatar-initial">{{ (auth.user?.name || auth.user?.email || '?')[0] }}</span>
      </button>
      <div v-if="menuOpen" class="menu" role="dialog" aria-label="Account">
        <div class="menu-who">
          <strong>{{ auth.user?.name || auth.user?.email }}</strong>
          <span class="muted small">{{ auth.user?.email }}</span>
          <span class="badge">{{ auth.user?.plan }}</span>
        </div>
        <DevicesList />
        <button type="button" class="secondary full" @click="auth.signOut()">Sign out</button>
      </div>
    </div>
  </header>
</template>
