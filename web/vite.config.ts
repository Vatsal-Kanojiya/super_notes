import { execSync } from 'node:child_process'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

/**
 * The build id: `YYYYMMDDHHMM-<shortsha>` (UTC). CI can set VITE_APP_VERSION
 * itself; otherwise it is made here, with `nogit` when there is no git.
 */
function buildId(): string {
  if (process.env.VITE_APP_VERSION) return process.env.VITE_APP_VERSION
  const stamp = new Date().toISOString().replace(/\D/g, '').slice(0, 12)
  let sha = 'nogit'
  try {
    sha = execSync('git rev-parse --short HEAD', { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim() || sha
  } catch {
    // no git, or not a checkout
  }
  return `${stamp}-${sha}`
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  define: {
    'import.meta.env.VITE_APP_VERSION': JSON.stringify(buildId()),
  },
  server: {
    // Google sign-in works only from an origin registered on the OAuth
    // client, and the API's CORS_ALLOWED_ORIGINS names it too: keep it fixed.
    port: 5173,
    strictPort: true,
  },
})
