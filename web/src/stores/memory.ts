/**
 * The facts the assistant remembers about the user (Settings > Memory).
 * Facts are text only and read-only: the user can delete one or forget all.
 * Deleting a fact also deletes the older ones it replaced (server side), so
 * after a delete the list is reloaded rather than patched.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { cursorFrom } from '../api/client'
import { memoryApi } from '../api/endpoints'
import type { Id, UserFact } from '../api/types'

export const useMemoryStore = defineStore('memory', () => {
  const facts = ref<UserFact[]>([])
  const cursor = ref<string | null>(null)
  const loaded = ref(false)

  async function load() {
    const page = await memoryApi.list()
    facts.value = page.results
    cursor.value = cursorFrom(page.next)
    loaded.value = true
  }

  async function loadMore() {
    if (!cursor.value) return
    const page = await memoryApi.list(cursor.value)
    const seen = new Set(facts.value.map((f) => f.id))
    facts.value = [...facts.value, ...page.results.filter((f) => !seen.has(f.id))]
    cursor.value = cursorFrom(page.next)
  }

  async function forget(id: Id) {
    await memoryApi.forget(id)
    await load()
  }

  async function forgetAll() {
    await memoryApi.forgetAll()
    facts.value = []
    cursor.value = null
    loaded.value = true
  }

  function reset() {
    facts.value = []
    cursor.value = null
    loaded.value = false
  }

  return { facts, cursor, loaded, load, loadMore, forget, forgetAll, reset }
})
