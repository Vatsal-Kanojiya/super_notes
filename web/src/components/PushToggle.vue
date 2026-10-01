<script setup lang="ts">
/**
 * "Notifications on this device": the browser's permission prompt and the push
 * subscription. Renders nothing when the server has push off (D87: hide it
 * gracefully), and explains itself when this browser cannot do push.
 */
import { onMounted } from 'vue'
import { usePushStore } from '../stores/push'

const push = usePushStore()
onMounted(() => void push.start())
</script>

<template>
  <div v-if="push.available" class="push-toggle">
    <template v-if="!push.browserOk">
      <span class="muted small">This browser cannot show push notifications. Reminders still arrive by email.</span>
    </template>
    <template v-else-if="push.subscribed">
      <span class="small">Notifications are on for this device.</span>
      <button type="button" class="secondary" :disabled="push.busy" @click="push.disable()">Turn off</button>
    </template>
    <template v-else>
      <span class="muted small">
        {{
          push.permission === 'denied'
            ? 'Notifications are blocked for this site in the browser settings.'
            : 'Get reminders as notifications on this device, even when the tab is closed.'
        }}
      </span>
      <button type="button" :disabled="push.busy || push.permission === 'denied'" @click="push.enable()">
        Turn on notifications
      </button>
    </template>
    <p v-if="push.error" class="error small" role="alert">{{ push.error }}</p>
  </div>
</template>
