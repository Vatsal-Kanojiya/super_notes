/**
 * Web push helpers that need no browser: the VAPID key and subscription
 * shapes (D202). The browser calls live in stores/push.ts.
 */
import type { PushSubscriptionRequest } from '../api/types'

/** A base64url string (the VAPID public key) as the bytes `pushManager.subscribe` wants. */
export function urlBase64ToBytes(value: string): Uint8Array<ArrayBuffer> {
  const padded = value.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(value.length / 4) * 4, '=')
  const binary = atob(padded)
  const bytes = new Uint8Array(new ArrayBuffer(binary.length))
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
  return bytes
}

/** `PushSubscription.toJSON()` flattened for `POST me/push-subscriptions/`; null if it lacks a part. */
export function toSubscriptionRequest(json: {
  endpoint?: string
  keys?: Record<string, string>
}): PushSubscriptionRequest | null {
  const endpoint = json.endpoint
  const p256dh = json.keys?.p256dh
  const auth = json.keys?.auth
  if (!endpoint || !p256dh || !auth) return null
  return { endpoint, p256dh, auth }
}

/** What this browser can do for push, as far as can be told without asking. */
export function pushSupported(env: {
  serviceWorker?: unknown
  PushManager?: unknown
  Notification?: unknown
}): boolean {
  return Boolean(env.serviceWorker && env.PushManager && env.Notification)
}
