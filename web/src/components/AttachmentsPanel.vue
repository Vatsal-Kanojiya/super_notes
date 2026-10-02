<script setup lang="ts">
/**
 * Files attached to a note: pick or drop to upload, see each one's status, download, summarize
 * and delete. Every name, error and summary is shown as text, never HTML (D50, D560).
 *
 * The list is loaded here (newest first) and polled while any file is still being read. A
 * download is an authenticated fetch turned into a blob, so no token ever sits in a URL (D561).
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { errorMessage } from '../api/client'
import { attachmentsApi, summaryApi } from '../api/endpoints'
import type { Attachment, Id } from '../api/types'
import {
  ACCEPT,
  LIMIT_TEXT,
  anyWorking,
  canSummarize,
  formatSize,
  isStorageFull,
  mergeAttachment,
  pollAttachments,
  statusText,
  storageText,
  uploadOne,
} from '../lib/attachments'
import { saveBlob } from '../lib/download'
import { formatDate } from '../lib/format'
import { describeSummaryStartError, runSummary, type SummaryProblem } from '../lib/summaryJob'
import { uuid4 } from '../lib/uuid'
import { useAuthStore } from '../stores/auth'

const props = defineProps<{ noteId: number }>()

const auth = useAuthStore()
const list = ref<Attachment[]>([])
const loading = ref(true)
const loadError = ref('')
const messages = ref<string[]>([])
const dragging = ref(false)
const picker = ref<HTMLInputElement | null>(null)

interface Uploading {
  key: number
  name: string
  fraction: number
}
const uploading = ref<Uploading[]>([])
let uploadKey = 0

const busy = reactive<Record<Id, string>>({})
const summaryWorking = reactive<Record<Id, boolean>>({})
const summaryProblem = reactive<Record<Id, SummaryProblem | undefined>>({})
const rowError = reactive<Record<Id, string | undefined>>({})

let alive = true
let pollRun = 0

const storage = computed(() => auth.user?.limits?.storage_bytes ?? null)
const storageLine = computed(() => storageText(storage.value))
const full = computed(() => isStorageFull(storage.value))

async function loadAll(): Promise<Attachment[]> {
  const all: Attachment[] = []
  let cursor: string | undefined
  for (let page = 0; page < 10; page++) {
    const result = await attachmentsApi.list(props.noteId, cursor)
    all.push(...result.results)
    if (!result.next) break
    cursor = new URL(result.next).searchParams.get('cursor') ?? undefined
    if (!cursor) break
  }
  return all
}

function keepPolling() {
  if (!alive || !anyWorking(list.value)) return
  const mine = ++pollRun
  void pollAttachments({
    load: loadAll,
    cancelled: () => !alive || mine !== pollRun,
    onList: (next) => {
      list.value = next
    },
  })
}

async function refresh() {
  try {
    list.value = await loadAll()
    loadError.value = ''
  } catch (e) {
    loadError.value = errorMessage(e)
  } finally {
    loading.value = false
  }
  keepPolling()
}

async function refreshStorage() {
  try {
    await auth.loadMe()
  } catch {
    // The storage line is a nicety.
  }
}

async function addFiles(files: File[]) {
  messages.value = []
  for (const file of files) {
    const item: Uploading = reactive({ key: ++uploadKey, name: file.name, fraction: 0 })
    uploading.value.push(item)
    const result = await uploadOne(
      { upload: (id, f, onProgress) => attachmentsApi.upload(id, f, onProgress) },
      props.noteId,
      file,
      (fraction) => {
        item.fraction = fraction
      },
    )
    uploading.value = uploading.value.filter((u) => u.key !== item.key)
    if (!alive) return
    if (result.kind === 'ok') {
      list.value = mergeAttachment(list.value, result.attachment)
      if (result.existing) messages.value.push(`${file.name} is already attached to this note.`)
    } else {
      messages.value.push(result.message)
      // A full store or a refused quota changes the numbers shown.
      if (result.code === 'quota_exceeded') void refreshStorage()
      if (result.code === 'quota_exceeded' || result.code === 'system_limit_reached') break
    }
  }
  void refreshStorage()
  keepPolling()
}

function onPick(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  if (files.length) void addFiles(files)
}

function onDrop(event: DragEvent) {
  dragging.value = false
  const files = Array.from(event.dataTransfer?.files ?? [])
  if (files.length) void addFiles(files)
}

async function download(a: Attachment) {
  rowError[a.id] = undefined
  busy[a.id] = 'Downloading…'
  try {
    saveBlob(await attachmentsApi.file(a.id), a.original_name)
  } catch (e) {
    rowError[a.id] = errorMessage(e)
  } finally {
    delete busy[a.id]
  }
}

async function remove(a: Attachment) {
  if (!confirm(`Delete ${a.original_name}?`)) return
  rowError[a.id] = undefined
  busy[a.id] = 'Deleting…'
  try {
    await attachmentsApi.remove(a.id)
    list.value = list.value.filter((x) => x.id !== a.id)
    void refreshStorage()
  } catch (e) {
    rowError[a.id] = errorMessage(e)
  } finally {
    delete busy[a.id]
  }
}

async function summarize(a: Attachment) {
  if (summaryWorking[a.id]) return
  summaryProblem[a.id] = undefined
  summaryWorking[a.id] = true
  try {
    const outcome = await runSummary(
      {
        create: (key) => attachmentsApi.summarize(a.id, key),
        get: summaryApi.get,
      },
      uuid4(),
      { cancelled: () => !alive },
    )
    if (!alive || outcome.kind === 'cancelled') return
    if (outcome.kind === 'done') {
      list.value = list.value.map((x) => (x.id === a.id ? { ...x, summary: outcome.job.summary } : x))
    } else {
      summaryProblem[a.id] = outcome.problem
    }
    void refreshStorage()
  } catch (e) {
    summaryProblem[a.id] = describeSummaryStartError(e)
  } finally {
    summaryWorking[a.id] = false
  }
}

onMounted(() => void refresh())
onBeforeUnmount(() => {
  alive = false
})
</script>

<template>
  <section
    class="attachments"
    :class="{ dragging }"
    aria-label="Attachments"
    @dragover.prevent="dragging = true"
    @dragleave.prevent="dragging = false"
    @drop.prevent="onDrop"
  >
    <div class="attach-head">
      <strong>Attachments</strong>
      <button type="button" class="secondary" :disabled="full" @click="picker?.click()">Attach a file</button>
      <input ref="picker" type="file" class="visually-hidden" multiple :accept="ACCEPT" aria-label="Choose files to attach" @change="onPick" />
    </div>
    <p class="muted small" data-testid="attach-limit">
      {{ LIMIT_TEXT }}. Or drop files here.
      <template v-if="storageLine"> {{ storageLine }}.</template>
      <template v-if="full"> Your storage is full: delete an attachment to add more.</template>
    </p>

    <p v-for="(m, i) in messages" :key="i" class="error small" role="alert">{{ m }}</p>

    <ul v-if="uploading.length" class="attach-list">
      <li v-for="u in uploading" :key="u.key" class="attach-row" data-testid="uploading">
        <span class="attach-name">{{ u.name }}</span>
        <progress :value="u.fraction" max="1" :aria-label="`Uploading ${u.name}`"></progress>
        <span class="muted small">{{ Math.round(u.fraction * 100) }}%</span>
      </li>
    </ul>

    <p v-if="loadError" class="error small" role="alert">
      {{ loadError }} <button type="button" class="link" @click="refresh">Try again</button>
    </p>
    <p v-else-if="loading" class="muted small">Loading attachments…</p>

    <ul v-if="list.length" class="attach-list" data-testid="attach-list">
      <li v-for="a in list" :key="a.id" class="attach-item">
        <div class="attach-row">
          <span class="attach-name">{{ a.original_name }}</span>
          <span class="muted small">{{ formatSize(a.size) }} · {{ formatDate(a.created_at) }}</span>
        </div>
        <div class="attach-row">
          <span
            class="attach-status small"
            :class="a.status"
            data-testid="attach-status"
            :role="a.status === 'failed' ? 'alert' : undefined"
          >
            <span v-if="a.status === 'pending' || a.status === 'extracting'" class="spinner" aria-hidden="true"></span>
            {{ statusText(a) }}
          </span>
          <span class="attach-actions">
            <button type="button" class="link" :disabled="!!busy[a.id]" @click="download(a)">Download</button>
            <button
              v-if="canSummarize(a)"
              type="button"
              class="link"
              :disabled="summaryWorking[a.id]"
              @click="summarize(a)"
            >
              {{ summaryWorking[a.id] ? 'Summarizing…' : a.summary ? 'Summarize again' : 'Summarize' }}
            </button>
            <button type="button" class="link danger" :disabled="!!busy[a.id]" @click="remove(a)">Delete</button>
          </span>
        </div>
        <p v-if="busy[a.id]" class="muted small" role="status">{{ busy[a.id] }}</p>
        <p v-if="rowError[a.id]" class="error small" role="alert">{{ rowError[a.id] }}</p>
        <p v-if="summaryProblem[a.id]" class="error small" role="alert">{{ summaryProblem[a.id]!.message }}</p>
        <details v-if="a.summary" class="attach-summary">
          <summary class="small">Summary</summary>
          <p class="summary-text">{{ a.summary }}</p>
        </details>
      </li>
    </ul>
    <p v-else-if="!loading && !loadError && !uploading.length" class="muted small">No files attached.</p>
  </section>
</template>
