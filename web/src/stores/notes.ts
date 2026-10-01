/**
 * The notes this client knows about, kept in step with the server.
 *
 * Sync is revision-based (plan §5, §7): the client remembers the highest
 * `latest_revision` it has applied and asks `notes/changes/?after=` for
 * everything since, updates and tombstones alike. The first call (`after=0`)
 * fetches every note, so the unfiltered list is simply the local map. A
 * search or type filter asks the server instead (`notes/?q=&type=`), because
 * keyword search is Postgres full-text and should not be reimplemented here.
 *
 * The revision lives in memory only: offline-first storage is out of V1
 * (D49), so a reload starts again from 0.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { cursorFrom, errorMessage } from '../api/client'
import { notesApi } from '../api/endpoints'
import { isTombstone } from '../api/types'
import type { DocNode, Id, Note, NoteType, NoteUpdateRequest } from '../api/types'

const SYNC_INTERVAL_MS = 30_000
/** A guard against a server that keeps saying has_more without progress. */
const MAX_SYNC_PAGES = 50

export function emptyDoc(type: NoteType): DocNode {
  if (type === 'checklist') {
    return {
      type: 'doc',
      content: [
        { type: 'taskList', content: [{ type: 'taskItem', attrs: { checked: false }, content: [{ type: 'paragraph' }] }] },
      ],
    }
  }
  return { type: 'doc', content: [{ type: 'paragraph' }] }
}

export const useNotesStore = defineStore('notes', () => {
  const byId = ref<Record<Id, Note>>({})
  const lastRevision = ref(0)
  /** True once the first sync has finished, so "no notes yet" is not shown early. */
  const loaded = ref(false)
  const syncing = ref(false)
  const syncError = ref('')

  // The server-side filtered list (search box and type filter).
  const query = ref('')
  const typeFilter = ref<NoteType | ''>('')
  const filteredIds = ref<Id[]>([])
  const filteredCursor = ref<string | null>(null)
  const filtering = ref(false)

  const isFiltered = computed(() => query.value.trim() !== '' || typeFilter.value !== '')

  /** Newest first, the server's own order (`-id`). */
  const allNotes = computed(() => Object.values(byId.value).sort((a, b) => b.id - a.id))

  const visibleNotes = computed(() =>
    isFiltered.value
      ? filteredIds.value.map((id) => byId.value[id]).filter((n): n is Note => n !== undefined)
      : allNotes.value,
  )

  /** Store a server copy unless we already hold a newer one. */
  function upsert(note: Note) {
    const existing = byId.value[note.id]
    if (!existing || note.revision >= existing.revision) byId.value[note.id] = note
  }

  function forget(id: Id) {
    delete byId.value[id]
    filteredIds.value = filteredIds.value.filter((x) => x !== id)
  }

  // ------------------------------------------------------------------ sync --

  let inFlight: Promise<void> | null = null
  /** Bumped by clear(), so a sync still in flight for the last account is dropped. */
  let epoch = 0

  /** Apply every change since `lastRevision`. Concurrent calls share one run. */
  function sync(): Promise<void> {
    if (!inFlight) {
      inFlight = runSync().finally(() => {
        inFlight = null
      })
    }
    return inFlight
  }

  async function runSync() {
    const started = epoch
    syncing.value = true
    try {
      for (let page = 0; page < MAX_SYNC_PAGES; page++) {
        const after = lastRevision.value
        const response = await notesApi.changes(after)
        if (started !== epoch) return
        if (response.latest_revision < after) {
          // The server is behind what we hold (a restored database, D25): our
          // copy is not trustworthy. Drop it and fetch everything again.
          byId.value = {}
          filteredIds.value = []
          lastRevision.value = 0
          continue
        }
        for (const item of response.results) {
          if (isTombstone(item)) forget(item.id)
          else upsert(item)
        }
        lastRevision.value = Math.max(lastRevision.value, response.latest_revision)
        if (!response.has_more || lastRevision.value <= after) break
      }
      syncError.value = ''
      loaded.value = true
    } catch (error) {
      if (started !== epoch) return
      syncError.value = `Could not sync: ${errorMessage(error)}`
      throw error
    } finally {
      syncing.value = false
    }
  }

  let stopAutoSync: (() => void) | null = null

  /** Sync now, on window focus, when the tab becomes visible, and every 30 s. */
  function startAutoSync() {
    if (stopAutoSync) return
    const quiet = () => void sync().catch(() => undefined)
    const onVisible = () => {
      if (document.visibilityState === 'visible') quiet()
    }
    window.addEventListener('focus', quiet)
    document.addEventListener('visibilitychange', onVisible)
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') quiet()
    }, SYNC_INTERVAL_MS)
    stopAutoSync = () => {
      window.removeEventListener('focus', quiet)
      document.removeEventListener('visibilitychange', onVisible)
      window.clearInterval(timer)
      stopAutoSync = null
    }
    quiet()
  }

  // ------------------------------------------------------------ filtering --

  let filterSeq = 0

  /** Run the server-side list for the current `query` / `typeFilter`. */
  async function runFilter(more = false) {
    if (!isFiltered.value) return
    const seq = ++filterSeq
    filtering.value = true
    try {
      const page = await notesApi.list({
        q: query.value.trim() || undefined,
        type: typeFilter.value || undefined,
        cursor: more ? (filteredCursor.value ?? undefined) : undefined,
      })
      // A newer search started while this one was in flight: drop this one.
      if (seq !== filterSeq) return
      page.results.forEach(upsert)
      const ids = page.results.map((n) => n.id)
      filteredIds.value = more ? [...filteredIds.value, ...ids] : ids
      filteredCursor.value = cursorFrom(page.next)
    } finally {
      if (seq === filterSeq) filtering.value = false
    }
  }

  // ------------------------------------------------------------ one note --

  /** The note from the map, fetched if this client has not seen it yet. */
  async function ensure(id: Id): Promise<Note> {
    const known = byId.value[id]
    if (known) return known
    const note = await notesApi.get(id)
    upsert(note)
    return note
  }

  async function create(type: NoteType): Promise<Note> {
    const note = await notesApi.create({ type, title: '', content: emptyDoc(type) })
    upsert(note)
    return note
  }

  async function update(
    id: Id,
    body: NoteUpdateRequest,
    options: { keepalive?: boolean } = {},
  ): Promise<Note> {
    const note = await notesApi.update(id, body, options)
    upsert(note)
    return note
  }

  async function remove(id: Id) {
    await notesApi.remove(id)
    forget(id)
  }

  /** On sign-out: nothing of the last account may linger. */
  function clear() {
    epoch++
    filterSeq++
    stopAutoSync?.()
    byId.value = {}
    lastRevision.value = 0
    loaded.value = false
    syncError.value = ''
    query.value = ''
    typeFilter.value = ''
    filteredIds.value = []
    filteredCursor.value = null
  }

  return {
    byId,
    lastRevision,
    loaded,
    syncing,
    syncError,
    query,
    typeFilter,
    filteredCursor,
    filtering,
    isFiltered,
    visibleNotes,
    upsert,
    sync,
    startAutoSync,
    runFilter,
    ensure,
    create,
    update,
    remove,
    clear,
  }
})
