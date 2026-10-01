<script setup lang="ts">
import { useLifecycleStore } from '../stores/lifecycle'

const lifecycle = useLifecycleStore()
</script>

<template>
  <aside
    v-if="lifecycle.memoryNotice"
    class="memory-banner"
    :class="lifecycle.memoryNotice.style"
    role="status"
  >
    <p v-if="lifecycle.memoryNotice.state === 'off'">
      Memory is off. Turn it on for more relevant answers.
      <router-link to="/settings" @click="lifecycle.markMemoryNoticeSeen()">Review</router-link>
    </p>
    <p v-else>
      We build a lasting memory from your chats to make answers more relevant.
      <router-link to="/settings" @click="lifecycle.markMemoryNoticeSeen()">Review / turn off</router-link>
    </p>
    <button type="button" class="secondary" @click="lifecycle.markMemoryNoticeSeen()">Dismiss</button>
  </aside>
</template>
