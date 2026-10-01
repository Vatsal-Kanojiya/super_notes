import { createPinia } from 'pinia'
import { createApp } from 'vue'
import App from './App.vue'
import { reloadOnce } from './lib/version'
import { router } from './router'
import './style.css'

// A lazy chunk that no longer exists (a new deploy replaced it): reload once to
// pick up the new build; the guard in reloadOnce stops a loop.
window.addEventListener('vite:preloadError', (event) => {
  let storage: Storage | null = null
  try {
    storage = window.sessionStorage
  } catch {
    // blocked: reloadOnce then declines to reload
  }
  if (reloadOnce('superNotes.preloadReload', storage, () => window.location.reload())) event.preventDefault()
})

createApp(App).use(createPinia()).use(router).mount('#app')
