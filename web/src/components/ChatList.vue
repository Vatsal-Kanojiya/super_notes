<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue'
import { errorMessage } from '../api/client'
import type { Conversation } from '../api/types'
import { formatRelative } from '../lib/format'
import { conversationLabel } from '../lib/thread'
import { useChatStore } from '../stores/chat'

const chat = useChatStore()
const error = ref('')
const loadingMore = ref(false)
const renaming = ref<number | null>(null)
const draft = ref('')
const renameInput = ref<HTMLInputElement[] | HTMLInputElement | null>(null)

async function load(more = false) {
  error.value = ''
  loadingMore.value = more
  try {
    await chat.loadList(more)
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    loadingMore.value = false
  }
}

onMounted(() => load())

async function startRename(c: Conversation) {
  renaming.value = c.id
  draft.value = c.title
  await nextTick()
  const el = Array.isArray(renameInput.value) ? renameInput.value[0] : renameInput.value
  el?.focus()
  el?.select()
}

async function saveRename(c: Conversation) {
  if (renaming.value !== c.id) return
  const title = draft.value.trim()
  renaming.value = null
  if (!title || title === c.title) return
  error.value = ''
  try {
    await chat.rename(c.id, title)
  } catch (e) {
    error.value = errorMessage(e)
  }
}

async function remove(c: Conversation) {
  if (!confirm(`Delete “${conversationLabel(c)}”? Its questions and answers go with it.`)) return
  error.value = ''
  try {
    await chat.remove(c.id)
  } catch (e) {
    error.value = errorMessage(e)
  }
}
</script>

<template>
  <main class="page">
    <div class="actions">
      <router-link to="/chat/new" class="button-link">New conversation</router-link>
    </div>

    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="!chat.listLoaded && !error" class="muted">Loading your conversations…</p>
    <p v-else-if="chat.listLoaded && chat.conversations.length === 0" class="muted empty">
      No conversations yet. Start one above.
    </p>

    <ul class="conversation-list">
      <li v-for="c in chat.conversations" :key="c.id" class="conversation">
        <form v-if="renaming === c.id" class="rename" @submit.prevent="saveRename(c)">
          <label class="visually-hidden" :for="`rename-${c.id}`">Conversation title</label>
          <input
            :id="`rename-${c.id}`"
            ref="renameInput"
            v-model="draft"
            type="text"
            maxlength="200"
            @keydown.esc="renaming = null"
            @blur="saveRename(c)"
          />
          <button type="submit">Save</button>
        </form>
        <template v-else>
          <router-link class="note-card conversation-link" :to="`/chat/${c.id}`">
            <span class="note-title">{{ conversationLabel(c) }}</span>
            <span class="muted small">{{ formatRelative(c.updated_at) }}</span>
          </router-link>
          <button type="button" class="secondary" :aria-label="`Rename ${conversationLabel(c)}`" @click="startRename(c)">
            Rename
          </button>
          <button type="button" class="secondary danger" :aria-label="`Delete ${conversationLabel(c)}`" @click="remove(c)">
            Delete
          </button>
        </template>
      </li>
    </ul>

    <button
      v-if="chat.listCursor"
      type="button"
      class="secondary full"
      :disabled="loadingMore"
      @click="load(true)"
    >
      Load more
    </button>
  </main>
</template>
