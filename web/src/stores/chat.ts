/**
 * Conversations: the list, and the thread being read.
 *
 * A turn is an ask in a conversation. Sending returns it pending (202) and it
 * is polled at `GET ask/<id>/` until done or failed, like a plain ask. Turns
 * are sequential: a 409 `turn_in_progress` means the previous one is still
 * running, so we wait for it and then send. Every question gets an
 * `Idempotency-Key`; if the POST got no answer (a dropped connection) the same
 * question retried reuses its key, so it cannot count twice.
 *
 * A new conversation is created lazily, by its first question
 * (`POST conversations/` with the question), so a visit to /chat/new that goes
 * nowhere leaves no empty conversation behind (D300).
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError, cursorFrom, errorMessage } from '../api/client'
import { askApi, conversationsApi } from '../api/endpoints'
import type { AskQuery, Conversation, ConversationDetail, Id, TurnInProgressBody } from '../api/types'
import {
  describeTurnFailure,
  failedLastTurn,
  isUnfinished,
  mergeTurn,
  mergeTurns,
  overLimit,
  pendingTurn,
  threadPhase,
  upsertConversation,
  type TurnError,
} from '../lib/thread'
import { uuid4 } from '../lib/uuid'
import { useAuthStore } from './auth'

const POLL_FIRST_MS = 800
const POLL_MAX_MS = 5_000
const POLL_FACTOR = 1.5
/** Stop polling after this long; the turn stays in the thread and is picked up on the next visit. */
const POLL_GIVE_UP_MS = 3 * 60_000
/** How many times a 409 is waited out before giving up (another tab may keep asking). */
const MAX_WAITS = 3

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

export const useChatStore = defineStore('chat', () => {
  const auth = useAuthStore()

  // ------------------------------------------------------------- the list --
  const conversations = ref<Conversation[]>([])
  const listCursor = ref<string | null>(null)
  const listLoaded = ref(false)

  // ----------------------------------------------------------- the thread --
  /** The open conversation; null on /chat/new (not created yet). */
  const currentId = ref<Id | null>(null)
  const title = ref('')
  const turns = ref<AskQuery[]>([])
  const loading = ref(false)
  /** The open conversation does not exist (any more). */
  const missing = ref(false)
  const sending = ref(false)
  const waiting = ref(false)
  const sendError = ref<TurnError | null>(null)

  const phase = computed(() => threadPhase(turns.value, { sending: sending.value, waiting: waiting.value }))
  const pending = computed(() => pendingTurn(turns.value))
  const failedLast = computed(() => failedLastTurn(turns.value))
  const chatTurns = computed(() => auth.user?.limits?.chat_turns ?? auth.user?.ask_usage ?? null)
  const overQuota = computed(() => overLimit(chatTurns.value))

  // A key for a POST that got no answer, kept for a retry of the same question.
  let unanswered: { scope: string; question: string; key: string } | null = null
  // Bumped when the thread changes or on clear(), so stale polls and loads stop.
  let epoch = 0
  // The id of a conversation `send` just created and adopted: the view moving to its URL must not reload it.
  let fresh: Id | null = null
  const polling = new Set<string>() // `${id}:${epoch}`

  function reset() {
    epoch++
    currentId.value = null
    title.value = ''
    turns.value = []
    loading.value = false
    missing.value = false
    sending.value = false
    waiting.value = false
    sendError.value = null
  }

  // ---------------------------------------------------------------- list --

  async function loadList(more = false) {
    const page = await conversationsApi.list(more ? (listCursor.value ?? undefined) : undefined)
    conversations.value = more ? [...conversations.value] : []
    for (const c of page.results) conversations.value = upsertConversation(conversations.value, c)
    listCursor.value = cursorFrom(page.next)
    listLoaded.value = true
  }

  async function rename(id: Id, newTitle: string) {
    const updated = await conversationsApi.rename(id, newTitle.trim())
    // A rename does not move it in the list (the server orders by the last turn).
    conversations.value = conversations.value.map((c) => (c.id === id ? { ...c, title: updated.title } : c))
    if (currentId.value === id) title.value = updated.title
  }

  async function remove(id: Id) {
    await conversationsApi.remove(id)
    conversations.value = conversations.value.filter((c) => c.id !== id)
    if (currentId.value === id) reset()
  }

  // -------------------------------------------------------------- thread --

  /** Open a conversation (null: the not-yet-created new one) and resume polling anything unfinished. */
  async function open(id: Id | null) {
    if (id !== null && fresh === id) {
      fresh = null
      return
    }
    fresh = null
    reset()
    currentId.value = id
    if (id === null) return
    const started = epoch
    loading.value = true
    try {
      const detail = await conversationsApi.get(id)
      if (started !== epoch) return
      adopt(detail)
    } catch (e) {
      if (started !== epoch) return
      if (e instanceof ApiError && e.status === 404) missing.value = true
      else {
        sendError.value = describeTurnFailure(
          e instanceof ApiError ? e : { status: 0, code: '', detail: errorMessage(e), body: null },
          chatTurns.value,
        )
      }
    } finally {
      if (started === epoch) loading.value = false
    }
  }

  function adopt(detail: ConversationDetail) {
    currentId.value = detail.id
    title.value = detail.title
    turns.value = mergeTurns([], detail.turns)
    conversations.value = upsertConversation(conversations.value, detail)
    const unfinished = pendingTurn(turns.value)
    if (unfinished) void poll(unfinished.id)
  }

  function putTurn(turn: AskQuery) {
    // Only into the thread it belongs to (the person may have moved on).
    if (turn.conversation !== currentId.value) return
    turns.value = mergeTurn(turns.value, turn)
  }

  /** Re-read usage from `me/` (a failed turn is not counted, so it can go down). */
  async function refreshUsage() {
    try {
      await auth.loadMe()
    } catch {
      // The usage line is a nicety; keep what we have.
    }
  }

  /** Poll one turn with backoff until it finishes. Resolves true if it did (usage may have changed). */
  async function pollLoop(id: Id, started: number): Promise<boolean> {
    const deadline = Date.now() + POLL_GIVE_UP_MS
    let delay = POLL_FIRST_MS
    while (Date.now() < deadline) {
      await sleep(delay)
      if (started !== epoch) return false
      try {
        const turn = await askApi.get(id)
        if (started !== epoch) return false
        putTurn(turn)
        if (!isUnfinished(turn)) return true
      } catch (e) {
        // Gone (404) or signed out (401/403): stop. Anything else (offline, a 429) is worth retrying.
        if (e instanceof ApiError && [401, 403, 404].includes(e.status)) return false
      }
      delay = Math.min(POLL_MAX_MS, delay * POLL_FACTOR)
    }
    return false
  }

  async function poll(id: Id) {
    const started = epoch
    const key = `${id}:${started}`
    if (polling.has(key)) return
    polling.add(key)
    try {
      if (await pollLoop(id, started)) await refreshUsage()
    } finally {
      polling.delete(key)
    }
  }

  /** The key to send `question` with: a retry of an unanswered POST reuses it. */
  function keyFor(scope: string, question: string): string {
    return unanswered && unanswered.scope === scope && unanswered.question === question ? unanswered.key : uuid4()
  }

  /**
   * Ask `question` (in the open conversation, or as the first turn of a new
   * one). Resolves to the id of a conversation just created, so the view can
   * move to its URL; otherwise undefined.
   */
  async function send(question: string): Promise<Id | undefined> {
    question = question.trim()
    if (!question || sending.value || waiting.value) return undefined
    sendError.value = null
    sending.value = true
    const started = epoch
    const scope = String(currentId.value ?? 'new')
    const key = keyFor(scope, question)
    try {
      if (currentId.value === null) {
        const detail = await conversationsApi.create(question, key)
        unanswered = null
        if (started !== epoch) return undefined
        adopt(detail)
        fresh = detail.id
        void refreshUsage()
        return detail.id
      }
      const id = currentId.value
      for (let waits = 0; ; waits++) {
        try {
          const turn = await conversationsApi.ask(id, question, key)
          unanswered = null
          if (started !== epoch) return undefined
          putTurn(turn)
          void refreshUsage()
          if (isUnfinished(turn)) void poll(turn.id)
          return undefined
        } catch (e) {
          if (!(e instanceof ApiError) || e.code !== 'turn_in_progress' || waits >= MAX_WAITS) throw e
          // The previous turn is still running (another tab, or a reload): wait for it, then send.
          const busy = (e.body as unknown as Partial<TurnInProgressBody> | null)?.turn
          sending.value = false
          waiting.value = true
          if (typeof busy === 'number') {
            try {
              putTurn(await askApi.get(busy))
            } catch {
              // Shown on the next poll.
            }
            await pollLoop(busy, started)
          } else {
            await sleep(POLL_FIRST_MS * 2)
          }
          if (started !== epoch) return undefined
          waiting.value = false
          sending.value = true
        }
      }
    } catch (e) {
      const failure = e instanceof ApiError ? e : { status: 0, code: '', detail: errorMessage(e), body: null }
      const error = describeTurnFailure(failure, chatTurns.value)
      if (failure.code === 'quota_exceeded') {
        const body = failure.body as { used?: unknown; limit?: unknown; resets_at?: unknown } | null
        if (body && typeof body.used === 'number' && typeof body.limit === 'number' && typeof body.resets_at === 'string') {
          auth.setAskUsage({ used: body.used, limit: body.limit, resets_at: body.resets_at })
        }
      }
      // A dropped connection or a 5xx may still have created the turn: retry with the same key.
      unanswered = error.retrySameKey ? { scope, question, key } : null
      if (started === epoch) sendError.value = error
      if (error.kind === 'missing' && started === epoch) missing.value = true
    } finally {
      if (started === epoch) {
        sending.value = false
        waiting.value = false
      }
    }
    return undefined
  }

  /** Ask a failed turn's question again, as a new turn (the failed one stays, showing what happened). */
  function retry(turn: AskQuery): Promise<Id | undefined> {
    return send(turn.question)
  }

  /** Leave the thread (its view is going away): stop its polls and drop it. */
  function close() {
    fresh = null
    reset()
  }

  /** Forget everything: signed out. */
  function clear() {
    reset()
    conversations.value = []
    listCursor.value = null
    listLoaded.value = false
    unanswered = null
    fresh = null
  }

  return {
    conversations,
    listCursor,
    listLoaded,
    currentId,
    title,
    turns,
    loading,
    missing,
    sending,
    waiting,
    sendError,
    phase,
    pending,
    failedLast,
    chatTurns,
    overQuota,
    loadList,
    rename,
    remove,
    open,
    send,
    retry,
    close,
    clear,
  }
})
