<script setup lang="ts">
/**
 * "Format note": a button, the wait, and a before/after preview to Apply or Discard.
 *
 * The note editor owns saving; this panel asks it (`ensureSaved`) to get everything saved before
 * the job starts, and while a job is running or being looked at it reports a phase so the editor
 * can lock itself (an edit now would move the note past the job's `base_version` and the Apply
 * would meet a 409). The rules and the state of a job live in lib/formatJob.ts; this is the
 * screen around them. Apply is the ordinary note PATCH (through the notes store, so the list
 * stays in step) and the editor takes the saved note via `applied`.
 */
import { computed, onBeforeUnmount, ref, shallowRef } from 'vue'
import { formatApi } from '../api/endpoints'
import type { DocNode, FormatJob, Note } from '../api/types'
import { formatDate } from '../lib/format'
import {
  applyFormat,
  isOutOfFormats,
  runFormat,
  usageText,
  whyDisabled,
  type FormatDeps,
  type FormatProblem,
} from '../lib/formatJob'
import { uuid4 } from '../lib/uuid'
import { useAuthStore } from '../stores/auth'
import { useNotesStore } from '../stores/notes'
import DocPreview from './DocPreview.vue'

export type FormatPhase = 'idle' | 'saving' | 'working' | 'ready' | 'applying'

const props = defineProps<{
  noteId: number
  /** The note has no text. */
  empty: boolean
  /** A reason the note cannot be formatted right now (deleted elsewhere, a save conflict). */
  unavailable: string
  /** Resolves true once everything typed is saved; false if it could not be. */
  ensureSaved: () => Promise<boolean>
  /** The note's content as saved (what the job is made from). */
  beforeDoc: () => DocNode
}>()

const emit = defineEmits<{
  phase: [phase: FormatPhase]
  applied: [note: Note]
  /** Apply met a 409: the note changed since. Carries the server's copy, if it sent one. */
  stale: [current: Note | null]
}>()

const auth = useAuthStore()
const notes = useNotesStore()

const phase = ref<FormatPhase>('idle')
const problem = shallowRef<FormatProblem | null>(null)
const applyError = ref('')
const notice = ref('')
const stale = ref(false)
const job = shallowRef<FormatJob | null>(null)
const before = shallowRef<DocNode | null>(null)

let run = 0
let key = ''
let reuseKey = false
// The note's content when the key was used: an edit since makes it a different request.
let keyedDoc = ''

function setPhase(next: FormatPhase) {
  phase.value = next
  emit('phase', next)
}

const usage = computed(() => auth.user?.limits?.format ?? null)
const disabledReason = computed(() => whyDisabled({ empty: props.empty, usage: usage.value, unavailable: props.unavailable }))
const usageLine = computed(() => {
  const text = usageText(usage.value)
  const resets = usage.value?.limit !== null ? formatDate(usage.value?.resets_at) : ''
  return resets ? `${text} · resets ${resets}` : text
})
const proposed = computed(() => job.value?.proposed_content ?? null)

const deps: FormatDeps = {
  create: formatApi.create,
  get: formatApi.get,
  patch: (id, body) => notes.update(id, body),
}

async function refreshUsage() {
  try {
    await auth.loadMe()
  } catch {
    // The usage line is a nicety; keep what we have.
  }
}

async function start() {
  if (phase.value !== 'idle' || disabledReason.value) return
  const mine = ++run
  problem.value = null
  applyError.value = ''
  notice.value = ''
  stale.value = false
  setPhase('saving')

  const saved = await props.ensureSaved()
  if (mine !== run) return
  if (!saved) {
    problem.value = {
      code: 'not_saved',
      stage: 'start',
      retryable: true,
      reuseKey: false,
      message: 'Your note could not be saved yet, so it was not formatted. Try again once it is saved.',
    }
    setPhase('idle')
    return
  }

  before.value = props.beforeDoc()
  const docNow = JSON.stringify(before.value)
  if (docNow !== keyedDoc) reuseKey = false
  keyedDoc = docNow
  setPhase('working')
  // A POST that got no answer may have made the job: its retry reuses the key.
  const attempt = reuseKey && key ? key : uuid4()
  key = attempt
  const outcome = await runFormat(deps, props.noteId, attempt, { cancelled: () => mine !== run })
  if (mine !== run || outcome.kind === 'cancelled') return
  void refreshUsage()
  if (outcome.kind === 'ready') {
    reuseKey = false
    job.value = outcome.job
    setPhase('ready')
    return
  }
  reuseKey = outcome.problem.reuseKey
  if (outcome.problem.usage) auth.setFormatUsage(outcome.problem.usage)
  problem.value = outcome.problem
  setPhase('idle')
}

/** Stop waiting. The job finishes on the server on its own; a failed one is not counted. */
function cancel() {
  run++
  setPhase('idle')
  void refreshUsage()
}

function discard() {
  run++
  job.value = null
  before.value = null
  applyError.value = ''
  setPhase('idle')
}

async function apply() {
  const current = job.value
  if (!current || phase.value !== 'ready') return
  applyError.value = ''
  setPhase('applying')
  const outcome = await applyFormat(deps, current)
  switch (outcome.kind) {
    case 'applied':
      job.value = null
      before.value = null
      notice.value = 'Formatted. The note has been saved.'
      emit('applied', outcome.note)
      setPhase('idle')
      break
    case 'conflict':
      job.value = null
      before.value = null
      stale.value = true
      emit('stale', outcome.current)
      setPhase('idle')
      break
    case 'gone':
      job.value = null
      before.value = null
      problem.value = { code: 'note_gone', stage: 'job', retryable: false, reuseKey: false, message: 'This note no longer exists.' }
      setPhase('idle')
      break
    case 'error':
      applyError.value = outcome.message
      setPhase('ready')
      break
  }
}

onBeforeUnmount(() => {
  run++
})
</script>

<template>
  <section class="format-panel">
    <div v-if="phase === 'idle'" class="format-row">
      <button type="button" class="secondary" :disabled="!!disabledReason" @click="start">Format note</button>
      <span class="muted small" data-testid="format-hint" aria-live="polite">{{ disabledReason || usageLine }}</span>
    </div>

    <p v-else-if="phase === 'saving' || phase === 'working'" class="muted format-working" data-testid="format-working" role="status">
      <span class="spinner" aria-hidden="true"></span>
      {{ phase === 'saving' ? 'Saving your note…' : 'Formatting your note…' }}
      <button type="button" class="link" @click="cancel">Cancel</button>
    </p>

    <template v-if="problem">
      <p v-if="problem.code === 'quota_exceeded'" class="notice" role="alert">
        {{ problem.message }}
        <template v-if="problem.usage?.resets_at">They reset on {{ formatDate(problem.usage.resets_at) }}.</template>
      </p>
      <p v-else-if="problem.stage === 'start' && !problem.retryable" class="notice" role="alert">{{ problem.message }}</p>
      <p v-else class="error small" role="alert">{{ problem.message }}</p>
      <div v-if="phase === 'idle' && problem.retryable" class="actions">
        <button type="button" :disabled="!!disabledReason" @click="start">Try again</button>
        <button type="button" class="secondary" @click="problem = null">Dismiss</button>
      </div>
    </template>

    <div v-if="stale" class="conflict" role="alert">
      <strong>This note changed before the formatted version could be applied.</strong>
      <p class="small">It was edited elsewhere, so nothing was overwritten. The latest copy is now shown.</p>
      <div class="actions">
        <button type="button" :disabled="!!disabledReason" @click="start">Format again</button>
        <button type="button" class="secondary" @click="stale = false">Dismiss</button>
      </div>
    </div>

    <p v-if="notice && phase === 'idle' && !problem && !stale" class="muted small" role="status">{{ notice }}</p>

    <div v-if="(phase === 'ready' || phase === 'applying') && before && proposed" class="format-preview">
      <div class="preview-grid">
        <div class="preview-pane">
          <h3>Before</h3>
          <DocPreview :doc="before" />
        </div>
        <div class="preview-pane">
          <h3>After</h3>
          <DocPreview :doc="proposed" />
        </div>
      </div>
      <p v-if="applyError" class="error small" role="alert">{{ applyError }}</p>
      <div class="actions">
        <button type="button" :disabled="phase === 'applying'" @click="apply">
          {{ phase === 'applying' ? 'Applying…' : 'Apply' }}
        </button>
        <button type="button" class="secondary" :disabled="phase === 'applying'" @click="discard">Discard</button>
      </div>
      <p v-if="isOutOfFormats(usage)" class="muted small">That was your last format this month.</p>
    </div>
  </section>
</template>
