<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { errorMessage } from '../api/client'
import type { AskQuery, Citation } from '../api/types'
import { splitAnswer } from '../lib/citations'
import { formatDate, formatRelative } from '../lib/format'
import { useAskStore } from '../stores/ask'
import { useRouter } from 'vue-router'

const ask = useAskStore()
const router = useRouter()
const question = ref('')
const historyError = ref('')

const current = computed<AskQuery | null>(() => ask.selected)
// Model output is split into text and citation segments and rendered as
// text: never v-html (D50).
const segments = computed(() => (current.value ? splitAnswer(current.value.answer, current.value.citations) : []))

async function submit() {
  const text = question.value.trim()
  if (!text) return
  await ask.submit(text)
  if (!ask.error) question.value = ''
}

function onKeydown(event: KeyboardEvent) {
  // Enter asks; Shift+Enter is a new line.
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    void submit()
  }
}

function openCitation(citation: Citation) {
  void router.push(`/notes/${citation.note_id}`)
}

async function loadHistory(more = false) {
  historyError.value = ''
  try {
    await ask.loadHistory(more)
  } catch (e) {
    historyError.value = errorMessage(e)
  }
}

onMounted(() => {
  if (!ask.historyLoaded) void loadHistory()
})
</script>

<template>
  <main class="page ask-page">
    <form class="ask-form" @submit.prevent="submit">
      <label for="question" class="visually-hidden">Question</label>
      <textarea
        id="question"
        v-model="question"
        rows="3"
        maxlength="1000"
        placeholder="Ask something about your notes…"
        @keydown="onKeydown"
      ></textarea>
      <div class="ask-row">
        <span v-if="ask.usage" class="muted small usage">
          <template v-if="ask.usage.limit === null">{{ ask.usage.used }} asks this month · unlimited</template>
          <template v-else>{{ ask.usage.used }} / {{ ask.usage.limit }} asks used<template v-if="ask.usage.resets_at"> · resets on {{ formatDate(ask.usage.resets_at) }}</template></template>
        </span>
        <button type="submit" :disabled="ask.submitting || !question.trim()">
          {{ ask.submitting ? 'Asking…' : 'Ask' }}
        </button>
      </div>
    </form>

    <p v-if="ask.errorKind === 'quota'" class="notice" role="alert">
      {{ ask.error }}
      <template v-if="ask.usage">They reset on {{ formatDate(ask.usage.resets_at) }}.</template>
    </p>
    <p v-else-if="ask.errorKind === 'throttled'" class="notice" role="alert">{{ ask.error }}</p>
    <p v-else-if="ask.error" class="error" role="alert">{{ ask.error }}</p>
    <p v-else-if="ask.overQuota" class="notice small">No asks left this month.</p>

    <article v-if="current" class="answer" aria-live="polite">
      <p class="answer-question">{{ current.question }}</p>
      <p v-if="current.status === 'pending' || current.status === 'running'" class="muted">
        <span class="spinner" aria-hidden="true"></span> Reading your notes…
      </p>
      <p v-else-if="current.status === 'failed'" class="error">
        {{ current.error || 'This question could not be answered. Please try again.' }}
      </p>
      <template v-else>
        <p class="answer-text">
          <template v-for="(segment, i) in segments" :key="i">
            <template v-if="segment.kind === 'text'">{{ segment.text }}</template>
            <button
              v-else
              type="button"
              class="cite"
              :title="segment.citation.title || 'Open the note'"
              @click="openCitation(segment.citation)"
            >
              {{ segment.citation.n }}
            </button>
          </template>
        </p>
        <ol v-if="current.citations.length" class="sources">
          <li v-for="citation in current.citations" :key="citation.n">
            <button type="button" class="source" @click="openCitation(citation)">
              <span class="cite static">{{ citation.n }}</span>
              <span class="source-text">
                <strong>{{ citation.title || 'Untitled' }}</strong>
                <span class="muted small">{{ citation.snippet }}</span>
              </span>
            </button>
          </li>
        </ol>
      </template>
    </article>

    <section class="history">
      <h3>Earlier questions</h3>
      <p v-if="historyError" class="error small">{{ historyError }}</p>
      <p v-else-if="ask.historyLoaded && ask.history.length === 0" class="muted small">None yet.</p>
      <ul>
        <li v-for="query in ask.history" :key="query.id">
          <button
            type="button"
            class="history-item"
            :class="{ active: query.id === ask.selectedId }"
            @click="ask.select(query.id)"
          >
            <span class="history-q">{{ query.question }}</span>
            <span class="muted small">
              {{ query.status === 'failed' ? 'Failed · ' : '' }}{{ formatRelative(query.created_at) }}
            </span>
          </button>
        </li>
      </ul>
      <button v-if="ask.historyCursor" type="button" class="secondary full" @click="loadHistory(true)">
        Load more
      </button>
    </section>
  </main>
</template>
