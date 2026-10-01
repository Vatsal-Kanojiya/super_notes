<script setup lang="ts">
/**
 * One note, edited in place and saved as you type.
 *
 * Autosave: every change marks the note dirty and (re)starts a 1 s timer;
 * when it fires, the title and content are PATCHed with the `version` this
 * edit was made on top of. One save runs at a time; edits made during a save
 * are saved right after it. A stale version comes back as 409 with the
 * server's copy, and the person chooses: keep mine (save again on top of the
 * server's version) or take theirs (load the server's copy).
 *
 * A newer copy arriving through sync replaces the editor's content only when
 * there is nothing unsaved; otherwise the next save meets the conflict prompt.
 */
import { EditorContent, useEditor } from '@tiptap/vue-3'
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { ApiError, errorMessage } from '../api/client'
import type { DocNode, Note, VersionConflictBody } from '../api/types'
import { notesApi } from '../api/endpoints'
import { editorExtensions } from '../lib/editorExtensions'
import { formatRelative } from '../lib/format'
import { isDocEmpty } from '../lib/formatJob'
import { emptyDoc, useNotesStore } from '../stores/notes'
import { useGoBack } from '../lib/nav'
import FormatPanel, { type FormatPhase } from './FormatPanel.vue'

const props = defineProps<{ initial: Note }>()

const AUTOSAVE_MS = 1000
const RETRY_MS = 5000

const notes = useNotesStore()
const { goBack, backLabel } = useGoBack()
const id = props.initial.id

/** The server copy this edit is on top of: its `version` goes with the next PATCH. */
const base = shallowRef<Note>(props.initial)
const title = ref(props.initial.title)
const status = ref<'saved' | 'unsaved' | 'saving' | 'error'>('saved')
const saveError = ref('')
const conflict = shallowRef<Note | null>(null)
const deletedElsewhere = ref(false)

let dirty = false
let saving = false
let saveAgain = false
let timer: number | undefined
// Kept up to date on every change, so a save started while the editor is
// being torn down (leaving the page) still has the content.
let latestContent: DocNode = contentOf(props.initial)

function contentOf(note: Note): DocNode {
  const content = note.content
  return content && typeof content === 'object' && content.type === 'doc' ? content : emptyDoc(note.type)
}

/** Whether the note has any text (the Format button needs some). */
const empty = ref(isDocEmpty(latestContent))

const editor = useEditor({
  extensions: editorExtensions,
  content: latestContent,
  onUpdate: ({ editor }) => {
    latestContent = editor.getJSON() as DocNode
    empty.value = isDocEmpty(latestContent)
    markDirty()
  },
})

// While a format is being made or looked at the note must not move on: an edit would take it past the
// job's base_version and the Apply would meet a 409. The preview replaces the editor on screen.
const formatPhase = ref<FormatPhase>('idle')
const locked = computed(() => formatPhase.value !== 'idle')
const previewing = computed(() => formatPhase.value === 'ready' || formatPhase.value === 'applying')
watch(locked, (value) => editor.value?.setEditable(!value, false))

function markDirty() {
  dirty = true
  status.value = 'unsaved'
  window.clearTimeout(timer)
  timer = window.setTimeout(() => save(), AUTOSAVE_MS)
}

watch(title, (value) => {
  if (value !== base.value.title || dirty) markDirty()
})

/**
 * `keepalive` is for the save as the tab is hidden: the request then survives
 * the page being closed (api/client.ts falls back to a normal request for a
 * body over 60 KiB, which a browser would refuse as keepalive).
 */
async function save(keepalive = false) {
  window.clearTimeout(timer)
  if (saving) {
    saveAgain = true
    return
  }
  if (!dirty || conflict.value || deletedElsewhere.value) return
  saving = true
  dirty = false
  status.value = 'saving'
  const body = { version: base.value.version, title: title.value, content: latestContent }
  try {
    base.value = await notes.update(id, body, { keepalive })
    saveError.value = ''
    status.value = dirty ? 'unsaved' : 'saved'
  } catch (e) {
    dirty = true
    if (e instanceof ApiError && e.status === 409 && e.code === 'version_conflict') {
      conflict.value = (e.body as unknown as VersionConflictBody).current
      status.value = 'unsaved'
    } else if (e instanceof ApiError && e.status === 404) {
      deletedElsewhere.value = true
      status.value = 'error'
    } else {
      saveError.value = errorMessage(e)
      status.value = 'error'
      // Offline or a server hiccup: try again shortly, keeping the edits.
      timer = window.setTimeout(() => save(), RETRY_MS)
    }
  } finally {
    saving = false
    if (saveAgain) {
      saveAgain = false
      void save()
    }
  }
}

/** Replace what is on screen with a server copy (no autosave triggered). */
function load(note: Note) {
  base.value = note
  title.value = note.title
  latestContent = contentOf(note)
  empty.value = isDocEmpty(latestContent)
  editor.value?.commands.setContent(latestContent, { emitUpdate: false })
  dirty = false
  status.value = 'saved'
}

/**
 * Resolves true once nothing typed is waiting to be saved (saving it now if need be); false if the
 * note cannot be saved (an error, a conflict, deleted). The Format button waits on this.
 */
async function ensureSaved(): Promise<boolean> {
  for (let i = 0; i < 600; i++) {
    if (conflict.value || deletedElsewhere.value) return false
    if (saving) {
      await new Promise((resolve) => setTimeout(resolve, 50))
      continue
    }
    if (!dirty) return true
    if (status.value === 'error') {
      // The last save failed: one more try now, not at the next timer.
      await save()
      if (dirty) return false
      continue
    }
    await save()
  }
  return false
}

/** A format was applied: the server's copy is the note now. */
function onFormatApplied(note: Note) {
  load(note)
}

/** The apply met a 409: show the server's newer copy (everything of ours was saved before formatting). */
async function onFormatStale(current: Note | null) {
  try {
    load(current ?? (await notesApi.get(id)))
  } catch (e) {
    saveError.value = errorMessage(e)
  }
  notes.upsert(base.value)
}

function keepMine() {
  const current = conflict.value
  if (!current) return
  conflict.value = null
  // Save my version on top of theirs: last writer wins, by choice.
  base.value = current
  dirty = true
  void save()
}

function takeTheirs() {
  const current = conflict.value
  if (!current) return
  conflict.value = null
  notes.upsert(current)
  load(current)
}

// Sync brought a newer copy (or a tombstone) for this note.
watch(
  () => notes.byId[id],
  (latest) => {
    if (!latest) {
      if (!saving) deletedElsewhere.value = true
      return
    }
    if (latest.version > base.value.version && !dirty && !saving && !conflict.value) load(latest)
  },
)

async function removeNote() {
  if (!confirm('Delete this note?')) return
  window.clearTimeout(timer)
  dirty = false
  try {
    await notes.remove(id)
    goBack()
  } catch (e) {
    saveError.value = errorMessage(e)
  }
}

// Leaving the tab (or the app, on a phone) is the moment to save, not later.
function onHide() {
  if (document.visibilityState === 'hidden' && dirty) void save(true)
}
onMounted(() => document.addEventListener('visibilitychange', onHide))
onBeforeUnmount(() => {
  document.removeEventListener('visibilitychange', onHide)
  if (dirty) void save().finally(() => notes.setUnsaved(id, false))
  else notes.setUnsaved(id, false)
})

// Tell the store when this note has edits the server lacks, so an app update
// never reloads the page over them. A note deleted elsewhere cannot be saved,
// so it must not block the update for ever.
watch([status, deletedElsewhere], () =>
  notes.setUnsaved(id, status.value !== 'saved' && !deletedElsewhere.value),
)

const formatUnavailable = computed(() =>
  deletedElsewhere.value
    ? 'This note was deleted on another device.'
    : conflict.value
      ? 'Resolve the conflict above first.'
      : '',
)

const statusText = computed(() => {
  if (deletedElsewhere.value) return 'Deleted'
  switch (status.value) {
    case 'saving':
      return 'Saving…'
    case 'unsaved':
      return 'Unsaved'
    case 'error':
      return 'Not saved'
    default:
      return `Saved ${formatRelative(base.value.updated_at)}`
  }
})

function isActive(name: string, attrs?: Record<string, unknown>) {
  return editor.value?.isActive(name, attrs) ?? false
}
</script>

<template>
  <main class="page editor-page">
    <div class="editor-bar">
      <button type="button" class="link" @click="goBack">← {{ backLabel }}</button>
      <span class="muted small" aria-live="polite">{{ statusText }}</span>
      <button type="button" class="link danger" @click="removeNote">Delete</button>
    </div>

    <p v-if="deletedElsewhere" class="notice">
      This note was deleted on another device. Anything typed here since is not saved.
    </p>
    <p v-if="saveError" class="error small">{{ saveError }}</p>

    <div v-if="conflict" class="conflict" role="alertdialog" aria-labelledby="conflict-title">
      <strong id="conflict-title">This note changed on another device.</strong>
      <p class="small">
        The other copy was saved {{ formatRelative(conflict.updated_at) }}<template v-if="conflict.title">
          as “{{ conflict.title }}”</template>.
      </p>
      <div class="actions">
        <button type="button" @click="keepMine">Keep mine</button>
        <button type="button" class="secondary" @click="takeTheirs">Take theirs</button>
      </div>
    </div>

    <input
      v-model="title"
      class="title-input"
      type="text"
      placeholder="Title"
      aria-label="Title"
      maxlength="300"
      :readonly="locked"
    />

    <FormatPanel
      :note-id="id"
      :empty="empty"
      :unavailable="formatUnavailable"
      :ensure-saved="ensureSaved"
      :before-doc="() => latestContent"
      @phase="formatPhase = $event"
      @applied="onFormatApplied"
      @stale="onFormatStale"
    />

    <div v-if="editor && !previewing" class="format-bar" role="toolbar" aria-label="Formatting">
      <button
        type="button"
        :class="{ on: isActive('bold') }"
        aria-label="Bold"
        @click="editor.chain().focus().toggleBold().run()"
      >
        <b>B</b>
      </button>
      <button
        type="button"
        :class="{ on: isActive('italic') }"
        aria-label="Italic"
        @click="editor.chain().focus().toggleItalic().run()"
      >
        <i>I</i>
      </button>
      <button
        type="button"
        :class="{ on: isActive('heading', { level: 2 }) }"
        aria-label="Heading"
        @click="editor.chain().focus().toggleHeading({ level: 2 }).run()"
      >
        H
      </button>
      <button
        type="button"
        :class="{ on: isActive('bulletList') }"
        aria-label="Bulleted list"
        @click="editor.chain().focus().toggleBulletList().run()"
      >
        •
      </button>
      <button
        type="button"
        :class="{ on: isActive('taskList') }"
        aria-label="Checklist"
        @click="editor.chain().focus().toggleTaskList().run()"
      >
        ☑
      </button>
    </div>

    <EditorContent v-show="!previewing" :editor="editor" class="editor" />
  </main>
</template>
