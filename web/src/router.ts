/**
 * Routes (D86 replaces the view store of D46).
 *
 * The URL is the state: back, refresh and deep links work because the router
 * owns "which screen". Signed-out visitors are sent to /signin and returned to
 * where they were headed once in.
 */
import { createRouter, createWebHistory, type RouteLocationNormalized } from 'vue-router'
import SignIn from './components/SignIn.vue'
import { safeNext } from './lib/safeNext'
import { useAuthStore } from './stores/auth'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/notes' },
    { path: '/signin', name: 'signin', component: SignIn, meta: { public: true } },
    { path: '/notes', name: 'notes', component: () => import('./components/NotesList.vue') },
    { path: '/notes/:id', name: 'note', component: () => import('./components/NotePage.vue'), props: (r) => ({ noteId: r.params.id as string }) },
    { path: '/ask', name: 'ask', component: () => import('./components/AskPanel.vue') },
    { path: '/settings', name: 'settings', component: () => import('./components/SettingsPage.vue') },
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
