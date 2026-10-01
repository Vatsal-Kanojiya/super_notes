<script setup lang="ts">
import { ref } from 'vue'
import { errorMessage } from '../api/client'
import { onMounted } from 'vue'
import { useAuthStore } from '../stores/auth'
import { usePushStore } from '../stores/push'
import PushToggle from './PushToggle.vue'

const auth = useAuthStore()
const push = usePushStore()
onMounted(() => void push.start())
const saving = ref(false)
const error = ref('')

async function setMemory(enabled: boolean) {
  saving.value = true
  error.value = ''
  try {
    await auth.updateMe({ memory_enabled: enabled })
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <main class="page">
    <h2>Settings</h2>

    <section v-if="auth.user" class="setting">
      <label class="setting-row">
        <input
          type="checkbox"
          :checked="auth.user.memory_enabled"
          :disabled="saving"
          @change="setMemory(($event.target as HTMLInputElement).checked)"
        />
        <span>
          <strong>Memory</strong>
          <span class="muted small">
            {{
              auth.user.memory_enabled
                ? 'On: a lasting memory is built from your chats to make answers more relevant.'
                : 'Off: answers use only your notes and the current chat.'
            }}
          </span>
        </span>
      </label>
      <p v-if="error" class="error small" role="alert">{{ error }}</p>
    </section>

    <section v-if="push.available" class="setting">
      <strong>Notifications</strong>
      <PushToggle />
    </section>

    <section v-if="auth.user" class="setting">
      <strong>Time zone</strong>
      <span class="muted small">{{ auth.user.timezone }}</span>
    </section>
  </main>
</template>
