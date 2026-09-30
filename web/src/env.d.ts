/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** e.g. http://localhost:8000/api/v1 (a trailing slash is fine). */
  readonly VITE_API_BASE_URL?: string
  /** The OAuth web client id; must be in the backend's GOOGLE_OAUTH_CLIENT_IDS. */
  readonly VITE_GOOGLE_CLIENT_ID?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
