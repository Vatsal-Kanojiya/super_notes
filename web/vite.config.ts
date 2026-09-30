import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  server: {
    // Google sign-in works only from an origin registered on the OAuth
    // client, and the API's CORS_ALLOWED_ORIGINS names it too: keep it fixed.
    port: 5173,
    strictPort: true,
  },
})
