<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { errorMessage } from '../api/client'
import type { UserFact } from '../api/types'
import { formatDate } from '../lib/format'
import { useMemoryStore } from '../stores/memory'

const memory = useMemoryStore()
const loading = ref(true)
const busy = ref(false)
const error = ref('')

async function run(action: () => Promise<void>) {
  busy.value = true
  error.value = ''
  try {
    await action()
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    busy.value = false
  }
}

function kindLabel(fact: UserFact): string {
  return fact.kind === 'static'
    ? 'Lasting'
    : fact.valid_until
      ? `Temporary, until ${formatDate(fact.valid_until)}`
      : 'Temporary'
}

function forget(fact: UserFact) {
  void run(() => memory.forget(fact.id))
}

function forgetAll() {
  if (!confirm('Forget everything the assistant remembers about you? This cannot be undone.')) return
  void run(() => memory.forgetAll())
}

onMounted(async () => {
  memory.reset() // never show another account's facts if the load fails
  try {
    await memory.load()
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <section class="memory-facts" aria-labelledby="memory-facts-title">
    <h3 id="memory-facts-title">What I remember about you</h3>
    <p class="muted small">
      Facts come only from what you say in your questions to the assistant, never from your notes.
    </p>
    <p v-if="loading" class="muted">Loading…</p>
    <template v-else>
      <p v-if="memory.facts.length === 0 && !error" class="muted">Nothing remembered yet.</p>
      <ul v-else class="fact-list">
        <li v-for="fact in memory.facts" :key="fact.id">
          <div class="fact-text">
            <!-- Facts derive from user text through a model: untrusted, rendered as text only. -->
            <span class="fact-body">{{ fact.text }}</span>
            <span class="muted small">{{ kindLabel(fact) }}</span>
          </div>
          <button type="button" class="secondary" :disabled="busy" @click="forget(fact)">Delete</button>
        </li>
      </ul>
      <button v-if="memory.cursor" type="button" class="secondary" :disabled="busy" @click="run(memory.loadMore)">
        Show more
      </button>
      <button v-if="memory.facts.length" type="button" class="secondary" :disabled="busy" @click="forgetAll">
        Forget everything
      </button>
    </template>
    <p v-if="error" class="error small" role="alert">{{ error }}</p>
  </section>
</template>
