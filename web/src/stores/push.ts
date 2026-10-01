/**
 * Web push on this device (D87, D89, D202).
 *
 * Three questions, kept apart: does the *server* have push on (the VAPID key
 * endpoint answers 404 when it does not, and then nothing push-related is
 * shown), can this *browser* do it (service worker, Push API, Notifications),
 * and is *this device* subscribed. `enable()` asks the browser's own
 * permission, subscribes with the server's key and registers the subscription;
 * `disable()` undoes both. The service worker (public/sw.js) is for push
 * only and caches nothing.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError, errorMessage } from '../api/client'
import { pushApi } from '../api/endpoints'
import { pushSupported, toSubscriptionRequest, urlBase64ToBytes } from '../lib/push'

const SW_URL = '/sw.js'

export const usePushStore = defineStore('push', () => {
  /** null: not asked yet. false: push is off on the server. */
  const serverOn = ref<boolean | null>(null)
  const publicKey = ref('')
  const browserOk = pushSupported({
    serviceWorker: typeof navigator !== 'undefined' && 'serviceWorker' in navigator ? navigator.serviceWorker : undefined,
    PushManager: typeof window !== 'undefined' ? window.PushManager : undefined,
    Notification: typeof window !== 'undefined' ? window.Notification : undefined,
  })
  const permission = ref<NotificationPermission>(browserOk ? Notification.permission : 'denied')
  const subscribed = ref(false)
  const busy = ref(false)
  const error = ref('')

  /** Show push controls at all: the server has it on. (A browser that cannot do it gets an explanation.) */
  const available = computed(() => serverOn.value === true)
  const usable = computed(() => available.value && browserOk)

  let started: Promise<void> | null = null
  let listening = false

  async function registration(): Promise<ServiceWorkerRegistration> {
    // updateViaCache none: the browser never serves sw.js from its HTTP cache.
    await navigator.serviceWorker.register(SW_URL, { scope: '/', updateViaCache: 'none' })
    return navigator.serviceWorker.ready
  }

  async function currentSubscription(): Promise<PushSubscription | null> {
    if (!browserOk) return null
    const reg = await navigator.serviceWorker.getRegistration(SW_URL)
    return (await reg?.pushManager.getSubscription()) ?? null
  }

  /** A notification clicked while the app is open comes here from the worker. */
  function listenForClicks(open: (path: string) => void) {
    if (!browserOk || listening) return
    listening = true
    navigator.serviceWorker.addEventListener('message', (event: MessageEvent) => {
      const data = event.data as { type?: unknown; path?: unknown } | null
      if (data?.type === 'open-path' && typeof data.path === 'string' && /^\/(notes\/\d+|calendar)$/.test(data.path)) {
        open(data.path)
      }
    })
  }

  /** Learn what the server and this browser offer; register the worker when push can work. */
  function start(): Promise<void> {
    if (!started) {
      started = (async () => {
        try {
          const key = await pushApi.vapidKey()
          publicKey.value = key.public_key
          serverOn.value = true
        } catch (e) {
          // 404: push is off on this server. Anything else: stay quiet and try again next start.
          if (e instanceof ApiError && e.status === 404) serverOn.value = false
          else started = null
          return
        }
        if (browserOk) {
          permission.value = Notification.permission
          try {
            await registration()
            subscribed.value = (await currentSubscription()) !== null
          } catch {
            // No worker (private window, blocked): push just stays off here.
          }
        }
      })()
    }
    return started
  }

  /** Ask for permission, subscribe, and tell the server. */
  async function enable(): Promise<boolean> {
    if (!usable.value || busy.value) return false
    busy.value = true
    error.value = ''
    try {
      permission.value = await Notification.requestPermission()
      if (permission.value !== 'granted') {
        error.value =
          permission.value === 'denied'
            ? 'Notifications are blocked for this site. Allow them in the browser settings, then try again.'
            : 'Notifications were not allowed.'
        return false
      }
      const reg = await registration()
      let subscription = await reg.pushManager.getSubscription()
      subscription ??= await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToBytes(publicKey.value),
      })
      const body = toSubscriptionRequest(subscription.toJSON())
      if (!body) throw new Error('The browser gave an incomplete push subscription.')
      await pushApi.subscribe(body)
      subscribed.value = true
      return true
    } catch (e) {
      error.value = e instanceof ApiError ? errorMessage(e) : `Could not turn on notifications: ${e instanceof Error ? e.message : 'unknown error'}`
      return false
    } finally {
      busy.value = false
    }
  }

  /** Remove this device's subscription (the browser's and the server's). */
  async function disable(): Promise<void> {
    if (busy.value) return
    busy.value = true
    error.value = ''
    try {
      const subscription = await currentSubscription()
      if (subscription) {
        const endpoint = subscription.endpoint
        await subscription.unsubscribe()
        await pushApi.unsubscribe(endpoint)
      }
      subscribed.value = false
    } catch (e) {
      error.value = errorMessage(e)
    } finally {
      busy.value = false
    }
  }

  /**
   * Sign-out: this browser must stop getting the account's reminders. Needs
   * the access token, so it runs before the tokens are cleared; best effort.
   */
  async function release(): Promise<void> {
    try {
      const subscription = await currentSubscription()
      if (!subscription) return
      const endpoint = subscription.endpoint
      await pushApi.unsubscribe(endpoint)
      await subscription.unsubscribe()
    } catch {
      // Offline or already signed out: the server drops the subscription when push to it fails.
    }
    subscribed.value = false
  }

  function clear() {
    started = null
    serverOn.value = null
    publicKey.value = ''
    subscribed.value = false
    error.value = ''
  }

  return { serverOn, available, usable, browserOk, permission, subscribed, busy, error, start, enable, disable, release, listenForClicks, clear }
})
