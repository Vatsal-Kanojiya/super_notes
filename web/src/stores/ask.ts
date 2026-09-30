/**
 * Asking questions of your notes.
 *
 * `POST ask/` answers 202 with a pending query; the client polls
 * `GET ask/<id>/` until it is done or failed (the job pattern, plan §6.5).
 * Every new question gets a fresh `Idempotency-Key`. If the POST never got an
 * answer (network drop), retrying the *same* question reuses the key, so the
 * server returns the query it may already have created instead of counting
 * a second ask against the quota.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError, cursorFrom, errorMessage } from '../api/client'
import { askApi } from '../api/endpoints'
import type { AskQuery, AskUsage, Id, QuotaExceededBody } from '../api/types'
import { uuid4 } from '../lib/uuid'
import { useAuthStore } from './auth'

const POLL_FIRST_MS = 800
const POLL_MAX_MS = 5_000
const POLL_FACTOR = 1.5
/** Stop polling after this long; the query stays in history for later. */
const POLL_GIVE_UP_MS = 3 * 60_000

function isFinished(query: AskQuery): boolean {
  return query.status === 'done' || query.status === 'failed'
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

export const useAskStore = defineStore('ask', () => {
  const auth = useAuthStore()

  /** Newest first. The current question is `history[0]` while it runs. */
  const history = ref<AskQuery[]>([])
  const historyCursor = ref<string | null>(null)
  const historyLoaded = ref(false)
  const selectedId = ref<Id | null>(null)
  const submitting = ref(false)
  const error = ref('')
  /** Why the last submit failed, so the panel can style the message. */
  const errorKind = ref<'' | 'quota' | 'throttled' | 'reused' | 'network' | 'other'>('')

  const usage = computed<AskUsage | null>(() => auth.user?.ask_usage ?? null)
  const overQuota = computed(() => (usage.value ? usage.value.used >= usage.value.limit : false))
  const selected = computed(() => history.value.find((q) => q.id === selectedId.value) ?? null)

  // A key for a POST that got no answer, kept for a retry of the same question.
  let unanswered: { question: string; key: string } | null = null
  // Bumped on clear(), so polls from a previous account stop.
  let epoch = 0
  const polling = new Set<Id>()

  function put(query: AskQuery) {
    const at = history.value.findIndex((q) => q.id === query.id)
    if (at >= 0) history.value[at] = query
    else history.value.unshift(query)
  }

  async function submit(question: string) {
    question = question.trim()
    if (!question || submitting.value) return
    error.value = ''
    errorKind.value = ''
    submitting.value = true
    const key = unanswered?.question === question ? unanswered.key : uuid4()
    try {
      const query = await askApi.create({ question }, key)
      // 202 (new) and 200 (a replayed key) are the same to us: an ask to show and, if unfinished, poll.
      unanswered = null
      put(query)
      selectedId.value = query.id
      // The ask counts once accepted: refresh the usage line (again when it finishes, as a failure uncounts it).
      void refreshUsage()
      if (!isFinished(query)) void poll(query.id)
    } catch (e) {
      // A dropped connection or a 5xx may still have created the ask: retry with the same key.
      const maybeCreated = e instanceof ApiError && (e.status === 0 || e.status >= 500)
      unanswered = maybeCreated ? { question, key } : null
      if (e instanceof ApiError && e.status === 0) {
        errorKind.value = 'network'
        error.value = 'Could not reach the server. Ask again to retry.'
      } else if (e instanceof ApiError && e.code === 'quota_exceeded') {
        const body = e.body as unknown as Partial<QuotaExceededBody> | null
        if (body && typeof body.used === 'number' && typeof body.limit === 'number' && body.resets_at) {
          auth.setAskUsage({ used: body.used, limit: body.limit, resets_at: body.resets_at })
        }
        errorKind.value = 'quota'
        error.value = body?.limit ? `You have used all ${body.limit} asks for this month.` : 'You have used all your asks for this month.'
      } else if (e instanceof ApiError && e.code === 'throttled') {
        errorKind.value = 'throttled'
        error.value = 'You are asking too fast. Wait a moment and try again.'
      } else if (e instanceof ApiError && e.code === 'idempotency_key_reused') {
        errorKind.value = 'reused'
        error.value = 'That request clashed with an earlier one. Ask again to send it as a new question.'
      } else {
        errorKind.value = 'other'
        error.value = errorMessage(e)
      }
    } finally {
      submitting.value = false
    }
  }

  /** Re-read usage from `me/` (failed asks are not counted, so it can go down). */
  async function refreshUsage() {
    try {
      await auth.loadMe()
    } catch {
      // The usage line is a nicety; keep what we have.
    }
  }

  /** Poll one query with backoff until it finishes (or we give up). */
  async function poll(id: Id) {
    if (polling.has(id)) return
    polling.add(id)
    try {
      if (await pollLoop(id)) await refreshUsage()
    } finally {
      polling.delete(id)
    }
  }

  /** Resolves true if the query finished (so usage may have changed). */
  async function pollLoop(id: Id): Promise<boolean> {
    const started = epoch
    const deadline = Date.now() + POLL_GIVE_UP_MS
    let delay = POLL_FIRST_MS
    while (Date.now() < deadline) {
      await sleep(delay)
      if (started !== epoch) return false
      try {
        const query = await askApi.get(id)
        if (started !== epoch) return false
        put(query)
        if (isFinished(query)) return true
      } catch (e) {
        // Gone (404) or signed out (401/403): stop. Anything else (offline, a 429) is worth retrying.
        if (e instanceof ApiError && [401, 403, 404].includes(e.status)) return false
      }
      delay = Math.min(POLL_MAX_MS, delay * POLL_FACTOR)
    }
    return false
  }

  async function loadHistory(more = false) {
    const page = await askApi.list(more ? (historyCursor.value ?? undefined) : undefined)
    for (const query of page.results) {
      const at = history.value.findIndex((q) => q.id === query.id)
      if (at >= 0) history.value[at] = query
      else history.value.push(query)
    }
    history.value.sort((a, b) => b.id - a.id)
    historyCursor.value = cursorFrom(page.next)
    historyLoaded.value = true
    // Resume polling anything left unfinished (say, after a reload).
    for (const query of page.results) if (!isFinished(query)) void poll(query.id)
  }

  function select(id: Id | null) {
    selectedId.value = id
  }

  function clear() {
    epoch++
    history.value = []
    historyCursor.value = null
    historyLoaded.value = false
    selectedId.value = null
    error.value = ''
    errorKind.value = ''
    unanswered = null
  }

  return {
    history,
    historyCursor,
    historyLoaded,
    selected,
    selectedId,
    submitting,
    error,
    errorKind,
    usage,
    overQuota,
    submit,
    loadHistory,
    select,
    clear,
  }
})
