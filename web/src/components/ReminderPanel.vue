<script setup lang="ts">
/**
 * A note's reminders: each with its due time, schedule and channels, and a form
 * to add or change one (D95). Times are typed and shown in the account's
 * timezone, whatever the browser's. The list comes from the notes store, which
 * `notes/changes/` keeps current (reminders ride along with their note).
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { errorMessage } from '../api/client'
import type { Id, Reminder, ReminderChannel } from '../api/types'
import {
  buildReminderRequest,
  defaultDueInput,
  describeReminder,
  diffReminderRequest,
  LEAD_DAYS_DEFAULT,
  LEAD_DAYS_MAX,
  safeTimeZone,
  toLocalInput,
} from '../lib/schedule'
import { useAuthStore } from '../stores/auth'
import { useNotesStore } from '../stores/notes'
import { usePushStore } from '../stores/push'
import { useRemindersStore } from '../stores/reminders'
import PushToggle from './PushToggle.vue'

const props = defineProps<{ noteId: Id }>()

const auth = useAuthStore()
const notes = useNotesStore()
const push = usePushStore()
const store = useRemindersStore()

const tz = computed(() => safeTimeZone(auth.user?.timezone))
const now = ref(Date.now())
let tick: number | undefined
onMounted(() => {
  void push.start()
  tick = window.setInterval(() => (now.value = Date.now()), 60_000)
})
onBeforeUnmount(() => window.clearInterval(tick))

const reminders = computed<Reminder[] | null>(() => {
  const held = notes.byId[props.noteId]?.reminders
  if (held) return held
  // Not carried yet: the first sync has not reached this note (a note opened from a link).
  return notes.loaded ? [] : null
})

const summaries = computed(() =>
  (reminders.value ?? []).map((reminder) => ({
    reminder,
    info: describeReminder(reminder, tz.value, now.value),
  })),
)

// ------------------------------------------------------------------ form --

interface FormState {
  /** The reminder being changed, or null for a new one. */
  editing: Reminder | null
  due: string
  leadDays: number | string
  email: boolean
  push: boolean
}

const form = ref<FormState | null>(null)
const saving = ref(false)
const error = ref('')
const actionError = ref('')
const busyId = ref<Id | null>(null)

function startAdd() {
  error.value = ''
  form.value = {
    editing: null,
    due: defaultDueInput(Date.now(), tz.value),
    leadDays: LEAD_DAYS_DEFAULT,
    email: true,
    push: push.available,
  }
}

function startEdit(reminder: Reminder) {
  error.value = ''
  form.value = {
    editing: reminder,
    due: toLocalInput(reminder.due_at, tz.value),
    leadDays: reminder.lead_days,
    email: reminder.channels.includes('email'),
    push: reminder.channels.includes('push'),
  }
}

function channelsOf(f: FormState): ReminderChannel[] {
  const channels: ReminderChannel[] = []
  if (f.email) channels.push('email')
  if (f.push) channels.push('push')
  return channels
}

async function submit() {
  const f = form.value
  if (!f || saving.value) return
  const built = buildReminderRequest(
    { due: f.due, leadDays: f.leadDays, channels: channelsOf(f) },
    tz.value,
    Date.now(),
    f.editing?.due_at,
  )
  if (!built.ok) {
    error.value = built.error
    return
  }
  saving.value = true
  error.value = ''
  try {
    if (f.editing) {
      const changes = diffReminderRequest(f.editing, built.body)
      if (changes) await store.edit(f.editing, changes)
    } else {
      await store.add(props.noteId, built.body)
    }
    form.value = null
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    saving.value = false
  }
}

async function act(reminder: Reminder, run: () => Promise<unknown>) {
  busyId.value = reminder.id
  actionError.value = ''
  try {
    await run()
    if (form.value?.editing?.id === reminder.id) form.value = null
  } catch (e) {
    actionError.value = errorMessage(e)
  } finally {
    busyId.value = null
  }
}

const markDone = (reminder: Reminder) => act(reminder, () => store.markDone(reminder))

function remove(reminder: Reminder) {
  if (!confirm('Delete this reminder?')) return
  void act(reminder, () => store.remove(reminder))
}

const stateLabel: Record<string, string> = { done: 'Done', cancelled: 'Cancelled', past: 'Past due' }
</script>

<template>
  <section class="reminders" aria-labelledby="reminders-title">
    <div class="reminders-head">
      <h3 id="reminders-title">Reminders</h3>
      <button v-if="!form" type="button" class="link" @click="startAdd">+ Add reminder</button>
    </div>

    <p v-if="reminders === null" class="muted small">Loading reminders…</p>
    <p v-else-if="reminders.length === 0 && !form" class="muted small">
      No reminders. Add one to be told as something expires or falls due.
    </p>

    <ul v-if="summaries.length" class="reminder-list">
      <li v-for="{ reminder, info } in summaries" :key="reminder.id" class="reminder" :class="info.state">
        <div class="reminder-main">
          <div class="reminder-due">
            <strong>{{ info.dueText }}</strong>
            <span v-if="stateLabel[info.state]" class="badge">{{ stateLabel[info.state] }}</span>
          </div>
          <span class="muted small">{{ info.scheduleText }}</span>
          <span v-if="info.nextText" class="small">{{ info.nextText }}</span>
          <span class="muted small">{{ info.channelsText }}</span>
        </div>
        <div class="reminder-actions">
          <button
            v-if="reminder.status === 'scheduled'"
            type="button"
            class="secondary"
            :disabled="busyId === reminder.id"
            @click="markDone(reminder)"
          >
            Mark done
          </button>
          <button type="button" class="link" :disabled="busyId === reminder.id" @click="startEdit(reminder)">Edit</button>
          <button type="button" class="link danger" :disabled="busyId === reminder.id" @click="remove(reminder)">
            Delete
          </button>
        </div>
      </li>
    </ul>
    <p v-if="actionError" class="error small" role="alert">{{ actionError }}</p>

    <form v-if="form" class="reminder-form" @submit.prevent="submit">
      <strong>{{ form.editing ? 'Change reminder' : 'New reminder' }}</strong>
      <label class="field">
        <span>Due</span>
        <input v-model="form.due" type="datetime-local" required />
        <span class="muted small">Time zone: {{ tz }}</span>
      </label>
      <label class="field">
        <span>Heads-ups, days before</span>
        <input v-model="form.leadDays" type="number" min="0" :max="LEAD_DAYS_MAX" step="1" inputmode="numeric" required />
        <span class="muted small">
          A notification every day for this many days before, at the same time, and on the due day. 0 means just one.
        </span>
      </label>
      <fieldset class="field channels">
        <legend>Notify by</legend>
        <label class="check"><input v-model="form.email" type="checkbox" /> Email</label>
        <label v-if="push.available" class="check"><input v-model="form.push" type="checkbox" /> Push notification</label>
      </fieldset>
      <PushToggle v-if="form.push && !push.subscribed" />
      <p v-if="error" class="error small" role="alert">{{ error }}</p>
      <div class="actions">
        <button type="submit" :disabled="saving">{{ saving ? 'Saving…' : 'Save' }}</button>
        <button type="button" class="secondary" :disabled="saving" @click="form = null">Cancel</button>
      </div>
    </form>
  </section>
</template>
