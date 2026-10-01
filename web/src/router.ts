/**
 * Routes (D86 replaces the view store of D46).
 *
 * The URL is the state: back, refresh and deep links work because the router
 * owns "which screen". Signed-out visitors are sent to /signin and returned to
 * where they were headed once in.
 */
import { createRouter, createWebHistory, type RouteLocationNormalized } from 'vue-router'
import AskPanel from './components/AskPanel.vue'
import NotePage from './components/NotePage.vue'
import NotesList from './components/NotesList.vue'
import SettingsPage from './components/SettingsPage.vue'
import SignIn from './components/SignIn.vue'
import { useAuthStore } from './stores/auth'

/** A same-site path only: never follow a `next` that points off the app. */
export function safeNext(next: unknown): string {
  // '//host' and '/\host' are protocol-relative to a browser; refuse both.
  return typeof next === 'string' && /^\/(?![/\\])/.test(next) ? next : '/notes'
}

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/notes' },
    { path: '/signin', name: 'signin', component: SignIn, meta: { public: true } },
    { path: '/notes', name: 'notes', component: NotesList },
    { path: '/notes/:id', name: 'note', component: NotePage, props: (r) => ({ noteId: r.params.id as string }) },
    { path: '/ask', name: 'ask', component: AskPanel },
    { path: '/settings', name: 'settings', component: SettingsPage },
    { path: '/:rest(.*)*', redirect: '/notes' },
  ],
})

export async function authGuard(to: RouteLocationNormalized) {
  const auth = useAuthStore()
  if (!auth.ready) await auth.init()
  if (to.meta.public) {
    return auth.signedIn ? safeNext(to.query.next) : true
  }
  if (!auth.signedIn) {
    return { name: 'signin', query: to.fullPath === '/notes' ? {} : { next: to.fullPath } }
  }
  return true
}

router.beforeEach(authGuard)
