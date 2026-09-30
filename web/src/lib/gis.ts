/**
 * Google Identity Services, loaded at runtime.
 *
 * The script is Google's own and is not bundled: it must come from
 * accounts.google.com. It is loaded on the sign-in screen only, so a
 * signed-in page load never fetches third-party script (D51).
 */

const SCRIPT_SRC = 'https://accounts.google.com/gsi/client'

interface CredentialResponse {
  credential: string
}

interface GoogleAccountsId {
  initialize(config: {
    client_id: string
    callback: (response: CredentialResponse) => void
    auto_select?: boolean
    cancel_on_tap_outside?: boolean
  }): void
  renderButton(parent: HTMLElement, options: Record<string, string | number>): void
  disableAutoSelect(): void
}

declare global {
  interface Window {
    google?: { accounts: { id: GoogleAccountsId } }
  }
}

export const GOOGLE_CLIENT_ID: string = import.meta.env.VITE_GOOGLE_CLIENT_ID ?? ''

let loading: Promise<GoogleAccountsId> | null = null

export function loadGis(): Promise<GoogleAccountsId> {
  if (window.google?.accounts?.id) return Promise.resolve(window.google.accounts.id)
  if (!loading) {
    loading = new Promise((resolve, reject) => {
      const script = document.createElement('script')
      script.src = SCRIPT_SRC
      script.async = true
      script.onload = () => {
        const gis = window.google?.accounts?.id
        if (gis) resolve(gis)
        else reject(new Error('Google sign-in did not start.'))
      }
      script.onerror = () => {
        // Let a later attempt (say, after coming back online) try again.
        loading = null
        script.remove()
        reject(new Error('Could not load Google sign-in.'))
      }
      document.head.appendChild(script)
    })
  }
  return loading
}

/** Render Google's button into `parent`; `onToken` gets the ID token. */
export async function renderGoogleButton(parent: HTMLElement, onToken: (idToken: string) => void): Promise<void> {
  const gis = await loadGis()
  gis.initialize({
    client_id: GOOGLE_CLIENT_ID,
    callback: (response) => onToken(response.credential),
    auto_select: false,
    cancel_on_tap_outside: true,
  })
  const dark = window.matchMedia?.('(prefers-color-scheme: dark)').matches
  gis.renderButton(parent, {
    type: 'standard',
    theme: dark ? 'filled_black' : 'outline',
    size: 'large',
    text: 'signin_with',
    shape: 'pill',
    width: 280,
  })
}

/** After signing out, stop Google from silently picking the same account. */
export function disableGoogleAutoSelect(): void {
  window.google?.accounts?.id?.disableAutoSelect()
}
