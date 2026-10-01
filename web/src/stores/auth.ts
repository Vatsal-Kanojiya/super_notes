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
import type { AskUsage, LimitUsage, Me, MeUpdateRequest } from '../api/types'
import { disableGoogleAutoSelect } from '../lib/gis'
import { usePushStore } from './push'

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
    // The token is gone, so the server call will likely fail; the local subscription is still dropped.
    if (user.value) void usePushStore().release()
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
    // A different account on this browser must not inherit the last one's push subscription.
    if (user.value && user.value.id !== response.user.id) await releasePush()
    setTokens(response)
    user.value = response.user
  }

  /** Save account settings (`PATCH me/`) and keep the local copy in step. */
  async function updateMe(body: MeUpdateRequest) {
    const updated = await authApi.updateMe(body)
    if (user.value) user.value = { ...user.value, ...updated }
    return updated
  }

  /** Keep the usage line in step without a round trip (e.g. from a 429 body). */
  function setAskUsage(usage: AskUsage) {
    if (!user.value) return
    // `ask_usage` and `limits.chat_turns` are the same numbers (D84).
    const limits = user.value.limits ? { ...user.value.limits, chat_turns: usage } : user.value.limits
    user.value = { ...user.value, ask_usage: usage, limits }
  }

  /** The same for the `format` limit (from a 429 body). */
  function setFormatUsage(usage: LimitUsage) {
    if (user.value?.limits) user.value = { ...user.value, limits: { ...user.value.limits, format: usage } }
  }

  function releasePush() {
    return Promise.race([usePushStore().release(), new Promise((resolve) => setTimeout(resolve, 3000))])
  }

  async function signOut() {
    const refresh = getRefresh()
    // This browser must stop getting the account's push reminders; it needs the
    // token, so it goes before the reset. Best effort, and not worth a long wait.
    await releasePush()
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

  return { user, ready, notice, signedIn, init, loadMe, signIn, signOut, setAskUsage, setFormatUsage, updateMe }
})
