/**
 * Tells the server the app was opened (D90, D93) and shows what it answers.
 *
 * `begin()` runs once a session is valid: it reports a `launch`, sets the
 * browser's timezone if the user never chose one, and from then on reports a
 * `resume` on the first interaction after 5 idle hours. The reply's notices
 * are the memory banner (here) and an update notice (handed to the version
 * store).
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { authApi, sessionApi } from '../api/endpoints'
import { createInteractionTracker, splitNotices, timezoneToSend, type SplitNotices } from '../lib/lifecycle'
import { APP_VERSION, useAppVersionStore } from './appVersion'
import { useAuthStore } from './auth'

const EVENTS = ['click', 'keydown', 'scroll', 'touchstart', 'pointerdown'] as const

export const useLifecycleStore = defineStore('lifecycle', () => {
  const auth = useAuthStore()
  const appVersion = useAppVersionStore()
  const memoryNotice = ref<SplitNotices['memory']>(null)
  let listening = false
  let active = false

  async function open(reason: 'launch' | 'resume') {
    if (!active) return
    try {
      const { notices } = await sessionApi.open(APP_VERSION, reason)
      const { update, memory } = splitNotices(notices)
      if (update) appVersion.setUpdateNotice(update.required)
      if (memory) memoryNotice.value = memory
    } catch {
      // Best effort: an open that fails to report is not worth bothering anyone about.
    }
  }

  async function syncTimezone() {
    const browser = Intl.DateTimeFormat().resolvedOptions().timeZone
    const send = auth.user ? timezoneToSend(auth.user.timezone, browser) : null
    if (!send) return
    try {
      await auth.updateMe({ timezone: send })
    } catch {
      // Not critical; the next launch tries again.
    }
  }

  function listen() {
    if (listening) return
    listening = true
    let storage: Storage | null = null
    try {
      storage = window.localStorage
    } catch {
      // blocked: the tracker keeps its memory copy
    }
    const tracker = createInteractionTracker({ storage, onResume: () => void open('resume') })
    // Counting from now: the launch itself is an interaction.
    tracker.touch()
    for (const name of EVENTS) window.addEventListener(name, () => active && tracker.touch(), { capture: true, passive: true })
  }

  /** A session is valid (page load with a stored session, or a fresh sign-in). */
  function begin() {
    active = true
    void open('launch')
    void syncTimezone()
    listen()
  }

  /** Signed out: drop what belonged to the account. */
  function end() {
    active = false
    memoryNotice.value = null
  }

  /** The banner's dismiss (and "Review"): the notice has been seen. */
  async function markMemoryNoticeSeen() {
    memoryNotice.value = null
    try {
      await authApi.memoryNoticeSeen()
    } catch {
      // It will simply come back at the next open.
    }
  }

  return { memoryNotice, open, begin, end, markMemoryNoticeSeen }
})
