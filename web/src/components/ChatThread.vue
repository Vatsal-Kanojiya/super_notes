<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { AskQuery } from '../api/types'
import { canCompose, conversationLabel, usageLine } from '../lib/thread'
import { useChatStore } from '../stores/chat'
import AnswerBody from './AnswerBody.vue'

const chat = useChatStore()
const route = useRoute()
const router = useRouter()
const question = ref('')
const composer = ref<HTMLTextAreaElement | null>(null)

/** The conversation in the URL; null on /chat/new. */
const routeId = computed(() => (route.params.id === undefined ? null : Number(route.params.id)))

watch(
  () => [route.name, routeId.value] as const,
  ([name, id]) => {
    // Leaving for another screen changes the route a moment before this view goes: not a reason to reload.
    if (name !== 'chat-new' && name !== 'chat-thread') return
    question.value = ''
    void chat.open(id)
  },
  { immediate: true },
)
onBeforeUnmount(() => chat.close())

const canSend = computed(() => canCompose(chat.phase) && question.value.trim().length > 0)
const heading = computed(() => (chat.currentId === null ? 'New conversation' : conversationLabel({ title: chat.title })))
const status = computed(() => {
  if (chat.phase === 'waiting') return 'Waiting for the previous answer to finish…'
  if (chat.phase === 'sending') return 'Sending…'
  return ''
})
const usage = computed(() => usageLine(chat.chatTurns))

async function submit() {
  const text = question.value.trim()
  if (!text || !canCompose(chat.phase)) return
  const created = await chat.send(text)
  if (!chat.sendError) question.value = ''
  // A first question creates the conversation: move to its URL (replacing /chat/new, so back skips it).
  if (created !== undefined) await router.replace(`/chat/${created}`)
}

async function retry(turn: AskQuery) {
  const created = await chat.retry(turn)
  if (created !== undefined) await router.replace(`/chat/${created}`)
}

function onKeydown(event: KeyboardEvent) {
  // Enter asks; Shift+Enter is a new line.
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    void submit()
  }
}

// Keep the newest turn and the composer in view as turns arrive.
watch(
  () => chat.turns.map((t) => `${t.id}:${t.status}:${chat.streaming[t.id]?.length ?? 0}`).join(','),
  async () => {
    await nextTick()
    composer.value?.scrollIntoView?.({ block: 'nearest' })
  },
)
</script>

<template>
  <main class="page chat-page">
    <div class="chat-head">
      <router-link to="/chat" class="chat-back">‹ Conversations</router-link>
      <h2 class="chat-title">{{ heading }}</h2>
    </div>

    <p v-if="chat.missing" class="notice" role="alert">
      This conversation no longer exists. <router-link to="/chat">Back to your conversations</router-link>
    </p>
    <template v-else>
      <p v-if="chat.loading" class="muted">Loading the conversation…</p>
      <p v-else-if="chat.phase === 'empty' && !chat.sendError" class="muted empty">
        Ask a question about your notes. Follow-ups can refer back to earlier answers.
      </p>

      <ol class="turns" aria-live="polite">
        <li v-for="turn in chat.turns" :key="turn.id" class="answer turn" :data-status="turn.status">
          <p class="answer-question">{{ turn.question }}</p>
          <AnswerBody
            v-if="(turn.status === 'pending' || turn.status === 'running') && chat.streaming[turn.id]"
            :answer="chat.streaming[turn.id]!"
            :citations="[]"
          />
          <p v-else-if="turn.status === 'pending' || turn.status === 'running'" class="muted">
            <span class="spinner" aria-hidden="true"></span> Reading your notes…
          </p>
          <div v-else-if="turn.status === 'failed'" class="turn-failed">
            <p class="error">{{ turn.error || 'This question could not be answered. Please try again.' }}</p>
            <button
              v-if="chat.failedLast?.id === turn.id"
              type="button"
              class="secondary"
              :disabled="!canCompose(chat.phase)"
              @click="retry(turn)"
            >
              Retry
            </button>
          </div>
          <AnswerBody v-else :answer="turn.answer" :citations="turn.citations" />
        </li>
      </ol>

      <form class="ask-form" @submit.prevent="submit">
        <label for="question" class="visually-hidden">Question</label>
        <textarea
          id="question"
          ref="composer"
          v-model="question"
          rows="3"
          maxlength="1000"
          :placeholder="chat.turns.length ? 'Ask a follow-up…' : 'Ask something about your notes…'"
          @keydown="onKeydown"
        ></textarea>
        <div class="ask-row">
          <span class="muted small usage">{{ status || usage }}</span>
          <button type="submit" :disabled="!canSend">{{ chat.phase === 'sending' ? 'Asking…' : 'Ask' }}</button>
        </div>
      </form>

      <p v-if="chat.sendError && ['quota', 'paused', 'throttled'].includes(chat.sendError.kind)" class="notice" role="alert">
        {{ chat.sendError.message }}
      </p>
      <p v-else-if="chat.sendError" class="error" role="alert">{{ chat.sendError.message }}</p>
      <p v-else-if="chat.overQuota" class="notice small">No chat turns left this month.</p>
    </template>
  </main>
</template>
