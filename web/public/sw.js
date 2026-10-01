/*
 * Super Notes service worker: web push and nothing else (D89, D202).
 *
 * It must never cache app code, so there is deliberately no `fetch` handler:
 * every request goes to the network as if it were not here. That is also what
 * keeps a new deploy from being masked by an old worker. The server serves
 * this file `Cache-Control: no-cache`, and the page registers it with
 * `updateViaCache: 'none'`, so a changed worker is picked up at once;
 * skipWaiting + clients.claim make it take over without waiting for a close.
 *
 * The push payload is `{type: "reminder", reminder_id, note_id, title}`. The
 * title is untrusted text: showNotification renders it as text only.
 */
self.addEventListener('install', () => {
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim())
})

function readPayload(event) {
  try {
    const data = event.data ? event.data.json() : null
    return data && typeof data === 'object' ? data : {}
  } catch (e) {
    return {}
  }
}

function notePath(data) {
  const id = data && data.note_id
  return Number.isInteger(id) && id > 0 ? '/notes/' + id : '/calendar'
}

function pathOf(data) {
  const path = data && data.path
  return typeof path === 'string' && /^\/(notes\/\d+|calendar)$/.test(path) ? path : '/calendar'
}

self.addEventListener('push', (event) => {
  const data = readPayload(event)
  const title = typeof data.title === 'string' && data.title.trim() ? data.title.trim().slice(0, 200) : 'Untitled note'
  const reminderId = Number.isInteger(data.reminder_id) ? data.reminder_id : null
  event.waitUntil(
    self.registration.showNotification(title, {
      body: 'Reminder',
      icon: '/favicon.svg',
      // One notification per reminder: today's replaces yesterday's heads-up.
      renotify: true,
      tag: reminderId === null ? 'reminder' : 'reminder-' + reminderId,
      data: { path: notePath(data) },
    }),
  )
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const path = pathOf(event.notification.data)
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      const open = windows.find((client) => new URL(client.url).origin === self.location.origin)
      if (open) {
        // An open tab goes to the note through the app's router (no reload, so
        // a half-typed note elsewhere is not thrown away); the page listens
        // for this message (stores/push.ts).
        open.postMessage({ type: 'open-path', path })
        await open.focus()
        return
      }
      await self.clients.openWindow(new URL(path, self.location.origin).href)
    })(),
  )
})
