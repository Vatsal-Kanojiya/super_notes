/** A same-site path only: never follow a `next` that points off the app. */
export function safeNext(next: unknown): string {
  // '//host' and '/\host' are protocol-relative to a browser; refuse both.
  return typeof next === 'string' && /^\/(?![/\\])/.test(next) ? next : '/notes'
}
