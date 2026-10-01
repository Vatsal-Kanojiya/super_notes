<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import type { Citation } from '../api/types'
import { splitAnswer } from '../lib/citations'

const props = defineProps<{ answer: string; citations: Citation[] }>()
const router = useRouter()

// Model output is split into text and citation segments and rendered as
// text: never v-html (D50).
const segments = computed(() => splitAnswer(props.answer, props.citations))

function openCitation(citation: Citation) {
  void router.push(`/notes/${citation.note_id}`)
}
</script>

<template>
  <p class="answer-text">
    <template v-for="(segment, i) in segments" :key="i">
      <template v-if="segment.kind === 'text'">{{ segment.text }}</template>
      <button
        v-else
        type="button"
        class="cite"
        :title="segment.citation.title || 'Open the note'"
        @click="openCitation(segment.citation)"
      >
        {{ segment.citation.n }}
      </button>
    </template>
  </p>
  <ol v-if="citations.length" class="sources">
    <li v-for="citation in citations" :key="citation.n">
      <button type="button" class="source" @click="openCitation(citation)">
        <span class="cite static">{{ citation.n }}</span>
        <span class="source-text">
          <strong>{{ citation.title || 'Untitled' }}</strong>
          <span class="muted small">{{ citation.snippet }}</span>
        </span>
      </button>
    </li>
  </ol>
</template>
