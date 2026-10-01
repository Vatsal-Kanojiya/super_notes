<script setup lang="ts">
import { useAppVersionStore } from '../stores/appVersion'
import { useNotesStore } from '../stores/notes'

const version = useAppVersionStore()
const notes = useNotesStore()
</script>

<template>
  <div v-if="version.showBar" class="update-bar" role="status">
    <span>
      <template v-if="notes.hasUnsaved">New version available. Reload once your changes are saved.</template>
      <template v-else-if="version.required">This version is out of date. Reload to continue.</template>
      <template v-else>New version — reload</template>
    </span>
    <button type="button" @click="version.reload()">Reload</button>
  </div>
</template>
