/**
 * Who is signed in, and the ways in and out.
 *
 * Google is the only way in: the GIS button hands over an ID token, which the
 * API exchanges for a JWT pair. There is no password and no development
 * back door.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError, setAuthLostHandler } from '../api/client'
import { authApi } from '../api/endpoints'
import { clearTokens, getAccess, getRefresh, onTokensChangedElsewhere, setTokens } from '../api/tokens'
import type { AskUsage, Me } from '../api/types'
import { disableGoogleAutoSelect } from '../lib/gis'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<Me | null>(null)
  /** Set once the stored session (if any) has been checked. */
  const ready = ref(false)
  /** Shown on the sign-in screen, e.g. after another device ended this one. */
  const notice = ref('')

  const signedIn = computed(() => user.value !== null)

  /** Local sign-out: forget everything, no API call. */
  function reset(message = '') {
    clearTokens()
    user.value = null
    notice.value = message
  }

  // The API client calls this when a refresh is refused: the session ended
  // elsewhere (device limit, signed out from another device, account gone).
  setAuthLostHandler(() => {
    if (user.value) reset('You were signed out. Please sign in again.')
  })

  // Another tab signed out (tokens removed) or in (new pair): follow it.
  onTokensChangedElsewhere(() => {
    if (!getRefresh()) reset()
    else if (!user.value) void loadMe()
  })

  async function loadMe(): Promise<Me | null> {
    if (!getAccess() && !getRefresh()) return null
    try {
      user.value = await authApi.me()
    } catch (error) {
      // A network failure keeps the tokens: the next attempt may work.
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) reset()
      else throw error
    }
    return user.value
  }

  /** On start: resume a stored session, if there is one. */
  async function init() {
    try {
      await loadMe()
    } catch {
      notice.value = 'Could not reach the server. Reload the page to try again.'
    } finally {
      ready.value = true
    }
  }

  async function signIn(idToken: string) {
    notice.value = ''
    const response = await authApi.google(idToken)
    setTokens(response)
    user.value = response.user
  }

  /** Keep the usage line in step without a round trip (e.g. from a 429 body). */
  function setAskUsage(usage: AskUsage) {
    if (user.value) user.value = { ...user.value, ask_usage: usage }
  }

  async function signOut() {
    const refresh = getRefresh()
    reset()
    disableGoogleAutoSelect()
    if (refresh) {
      try {
        await authApi.logout(refresh)
      } catch {
        // Best effort: the tokens are gone locally either way, and the
        // refresh token expires on its own.
      }
    }
  }

  return { user, ready, notice, signedIn, init, loadMe, signIn, signOut, setAskUsage }
})
