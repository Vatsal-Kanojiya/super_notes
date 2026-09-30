/** Date formatting for people, in their locale. */

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

/** "just now", "5 min ago", "3 h ago", else the date. */
export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return ''
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000
  if (Number.isNaN(seconds)) return ''
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`
  return formatDate(iso)
}
