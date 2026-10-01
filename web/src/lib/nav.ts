/**
 * "Back" from a note: the browser's own back when the user came from inside
 * the app (a citation goes back to the answer), else the notes list (a deep
 * link or a refreshed page has nowhere to go back to).
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'

export function useGoBack() {
  const router = useRouter()
  const previous = (): string | null => {
    const back = (window.history.state as { back?: unknown } | null)?.back
    return typeof back === 'string' ? back : null
  }

  function goBack() {
    if (previous() !== null) router.back()
    else void router.push('/notes')
  }

  const backLabel = computed(() => {
    // Read the route so the label re-evaluates on navigation.
    void router.currentRoute.value.fullPath
    return previous()?.startsWith('/ask') ? 'Answer' : 'Notes'
  })

  return { goBack, backLabel }
}
