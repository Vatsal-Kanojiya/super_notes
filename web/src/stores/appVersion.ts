/**
 * Keeps this tab on a current build (D89).
 *
 * Asks `GET app/version/` on load, on window focus and every 5 minutes, and
 * also listens for the `X-Client-Min-Version` header on every API response.
 * What to do about it is `decideUpdate`'s call; this store only gathers the
 * facts, shows the bar, and reloads when it says so and no edit is unsaved.
 */
import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { setMinVersionHandler } from '../api/client'
import { appApi } from '../api/endpoints'
import { decideUpdate, isBelowMinimum } from '../lib/version'
import { useNotesStore } from './notes'

const CHECK_EVERY_MS = 5 * 60_000
const RELOADED_FOR = 'superNotes.updateReloadedFor'

/** This build's id; empty when run outside a build. */
export const APP_VERSION: string = import.meta.env.VITE_APP_VERSION || ''

function storage(): Storage | null {
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

export const useAppVersionStore = defineStore('appVersion', () => {
  const notes = useNotesStore()
  const latest = ref('')
  const minSupported = ref('')
  let started = false

  const action = computed(() =>
    decideUpdate({
      current: APP_VERSION,
      latest: latest.value,
      minSupported: minSupported.value,
      unsaved: notes.hasUnsaved,
      alreadyReloaded: storage()?.getItem(RELOADED_FOR) === (latest.value || minSupported.value),
    }),
  )
  /** Show the "New version, reload" bar. */
  const showBar = computed(() => action.value !== 'none')
  /** The bar cannot be ignored: this build is below the minimum. */
  const required = computed(() => isBelowMinimum(APP_VERSION, minSupported.value))

  function reload() {
    // Remember what we reloaded for, so a deploy that is not live everywhere yet cannot loop us.
    try {
      storage()?.setItem(RELOADED_FOR, latest.value || minSupported.value)
    } catch {
      // ignore
    }
    window.location.reload()
  }

  // Re-decide whenever the facts change, including when an unsaved edit finishes saving.
  watch(action, (value) => {
    if (value === 'reload') reload()
  })

  function noteMin(value: string) {
    if (value && value !== minSupported.value) minSupported.value = value
  }

  async function check() {
    try {
      const response = await appApi.version()
      latest.value = response.latest || ''
      minSupported.value = response.min_supported || ''
    } catch {
      // Offline or the server is busy: try again at the next focus or tick.
    }
  }

  function start() {
    if (started || !APP_VERSION) return
    started = true
    setMinVersionHandler(noteMin)
    void check()
    window.addEventListener('focus', () => void check())
    window.setInterval(() => void check(), CHECK_EVERY_MS)
  }

  return { latest, minSupported, action, showBar, required, check, start, reload }
})
