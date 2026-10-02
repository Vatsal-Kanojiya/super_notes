<script setup lang="ts">
/**
 * The note's summary: a Summarize button, the wait, the text, and a "stale" marker once the note
 * was edited after the summary was made. Summary text is plain text, never HTML (D50, D560).
 *
 * The note's own `version` does not change when a summary is stored (D553), so nothing here
 * locks the editor. The editor owns saving; this asks it (`ensureSaved`) so the summary is of
 * what is on screen.
 */
import { computed } from 'vue'
import { summaryApi } from '../api/endpoints'
import type { SummaryJob } from '../api/types'
import { formatDate } from '../lib/format'
import { useSummaryRun } from '../lib/useSummaryRun'
import { useAuthStore } from '../stores/auth'

const props = defineProps<{
  noteId: number
  summary: string
  stale: boolean
  empty: boolean
  unavailable: string
  ensureSaved: () => Promise<boolean>
}>()
const emit = defineEmits<{ done: [job: SummaryJob] }>()

const auth = useAuthStore()

const run = useSummaryRun(
  {
    create: (key) => summaryApi.summarizeNote(props.noteId, key),
    get: summaryApi.get,
  },
  (job) => emit('done', job),
  () => {
    void auth.loadMe().catch(() => {})
  },
)

const usage = computed(() => auth.user?.limits?.summary ?? null)
const outOfSummaries = computed(() => !!usage.value && usage.value.limit !== null && usage.value.used >= usage.value.limit)
const disabledReason = computed(() => {
  if (props.unavailable) return props.unavailable
  if (props.empty) return 'Write something first: there is nothing to summarize yet.'
  if (outOfSummaries.value) return 'You have used all your summaries for this month.'
  return ''
})

async function start() {
  if (run.working.value || disabledReason.value) return
  if (!(await props.ensureSaved())) {
    run.problem.value = {
      code: 'not_saved',
      stage: 'start',
      retryable: true,
      reuseKey: false,
      message: 'Your note could not be saved yet, so it was not summarized. Try again once it is saved.',
    }
    return
  }
  await run.start()
}
</script>

<template>
  <section class="summary-panel" aria-label="Summary">
    <div v-if="summary" class="summary-box">
      <div class="summary-head">
        <strong>Summary</strong>
        <span v-if="stale" class="stale-tag" data-testid="summary-stale" title="The note was edited after this summary was made">
          Out of date
        </span>
      </div>
      <p class="summary-text" data-testid="summary-text">{{ summary }}</p>
    </div>

    <p v-if="run.working.value" class="muted format-working" data-testid="summary-working" role="status">
      <span class="spinner" aria-hidden="true"></span> Summarizing…
      <button type="button" class="link" @click="run.cancel()">Cancel</button>
    </p>
    <div v-else class="format-row">
      <button type="button" class="secondary" :disabled="!!disabledReason" @click="start">
        {{ summary ? 'Summarize again' : 'Summarize' }}
      </button>
      <span class="muted small" data-testid="summary-hint" aria-live="polite">
        {{ disabledReason || (usage && usage.limit !== null ? `${usage.used} / ${usage.limit} summaries used` : '') }}
      </span>
    </div>

    <template v-if="run.problem.value">
      <p v-if="run.problem.value.code === 'quota_exceeded'" class="notice" role="alert">
        {{ run.problem.value.message }}
        <template v-if="run.problem.value.usage?.resets_at">They reset on {{ formatDate(run.problem.value.usage.resets_at) }}.</template>
      </p>
      <p v-else-if="run.problem.value.stage === 'start' && !run.problem.value.retryable" class="notice" role="alert">
        {{ run.problem.value.message }}
      </p>
      <p v-else class="error small" role="alert">{{ run.problem.value.message }}</p>
    </template>
  </section>
</template>
