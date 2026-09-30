<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { errorMessage } from '../api/client'
import { authApi } from '../api/endpoints'
import type { Device } from '../api/types'
import { formatRelative } from '../lib/format'

const devices = ref<Device[]>([])
const loading = ref(true)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    devices.value = await authApi.devices()
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    loading.value = false
  }
}

async function signOutDevice(device: Device) {
  if (!confirm(`Sign out “${device.label || 'this device'}”?`)) return
  try {
    await authApi.signOutDevice(device.id)
    devices.value = devices.value.filter((d) => d.id !== device.id)
  } catch (e) {
    error.value = errorMessage(e)
  }
}

onMounted(load)
</script>

<template>
  <section class="devices">
    <h3>Signed-in devices</h3>
    <p v-if="loading" class="muted">Loading…</p>
    <p v-else-if="error" class="error">{{ error }}</p>
    <ul v-else class="device-list">
      <li v-for="device in devices" :key="device.id">
        <div class="device-text">
          <!-- The label is a User-Agent string: untrusted, rendered as text only. -->
          <span class="device-label">{{ device.label || 'Unknown device' }}</span>
          <span class="muted small">
            <template v-if="device.current">This device · </template>
            Last seen {{ formatRelative(device.last_seen_at) }}
          </span>
        </div>
        <button v-if="!device.current" type="button" class="link danger" @click="signOutDevice(device)">
          Sign out
        </button>
      </li>
      <li v-if="devices.length === 0" class="muted">No devices.</li>
    </ul>
  </section>
</template>
