<script setup lang="ts">
/**
 * The calendar: every notification of every reminder, by month or by week.
 *
 * The view and the day it is anchored on live in the URL (`?view=week&d=`), so
 * going into a note and back lands on the same page. Each load asks the API
 * for the visible days only (a range of at most 62 days) in the account's
 * timezone; a sync that brought changes reloads it, so a reminder made or
 * finished on another device shows up. Clicking an entry opens its note.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { errorMessage } from '../api/client'
import { remindersApi } from '../api/endpoints'
import type { ReminderInRange } from '../api/types'
import {
  dayFromQuery,
  entriesByDay,
  rangeFor,
  shiftAnchor,
  titleFor,
  viewFromQuery,
  visibleDays,
  weekdayLabels,
  type CalendarEntry,
  type CalendarView,
} from '../lib/calendar'
import { dayOf, formatDay, parseDay, safeTimeZone, startOfDay, type Day } from '../lib/schedule'
import { useAuthStore } from '../stores/auth'
import { useNotesStore } from '../stores/notes'
import { usePushStore } from '../stores/push'
import PushToggle from './PushToggle.vue'

const MAX_PER_MONTH_CELL = 3

const auth = useAuthStore()
const notes = useNotesStore()
const push = usePushStore()
const route = useRoute()
const router = useRouter()

const tz = computed(() => safeTimeZone(auth.user?.timezone))
const today = computed(() => dayOf(Date.now(), tz.value))
const view = computed<CalendarView>(() => viewFromQuery(route.query.view))
const anchor = computed<Day>(() => dayFromQuery(route.query.d, today.value))
const days = computed(() => visibleDays(view.value, anchor.value))
const title = computed(() => titleFor(view.value, anchor.value))
const labels = weekdayLabels()
const anchorMonth = computed(() => parseDay(anchor.value).month)

const results = ref<ReminderInRange[]>([])
const loading = ref(false)
const error = ref('')
const loadedKey = ref('')
let seq = 0

const byDay = computed(() => entriesByDay(results.value, days.value, tz.value))
const isEmpty = computed(() => loadedKey.value !== '' && Object.keys(byDay.value).length === 0)

function go(next: { view?: CalendarView; d?: Day }) {
  void router.push({ path: '/calendar', query: { view: next.view ?? view.value, d: next.d ?? anchor.value } })
}

async function load() {
  const mine = ++seq
  const { from, to } = rangeFor(days.value, tz.value)
  const key = `${from}|${to}|${tz.value}`
  loading.value = true
  try {
    const response = await remindersApi.range(from, to)
    if (mine !== seq) return
    results.value = response.results
    error.value = ''
    loadedKey.value = key
  } catch (e) {
    if (mine !== seq) return
    error.value = errorMessage(e)
  } finally {
    if (mine === seq) loading.value = false
  }
}

onMounted(() => void push.start())
// Re-ask when the visible days change, and when sync brought writes (this tab's or another's).
watch([() => view.value, () => anchor.value, tz], load, { immediate: true })
watch(
  () => notes.lastRevision,
  () => void load(),
)

function open(entry: CalendarEntry) {
  void router.push(`/notes/${entry.noteId}`)
}

function entriesOf(day: Day): CalendarEntry[] {
  return byDay.value[day] ?? []
}

function shown(day: Day): CalendarEntry[] {
  const all = entriesOf(day)
  return view.value === 'month' ? all.slice(0, MAX_PER_MONTH_CELL) : all
}

function dayLabel(day: Day): string {
  return formatDay(startOfDay(day, tz.value), tz.value)
}

function entryLabel(entry: CalendarEntry): string {
  return `${entry.time} ${entry.title}${entry.isDue ? ' (due)' : ''}${entry.status === 'done' ? ' (done)' : ''}`
}
</script>

<template>
  <main class="page calendar-page">
    <div class="calendar-bar">
      <div class="calendar-nav">
        <button type="button" class="secondary" aria-label="Previous" @click="go({ d: shiftAnchor(view, anchor, -1) })">‹</button>
        <button type="button" class="secondary" @click="go({ d: today })">Today</button>
        <button type="button" class="secondary" aria-label="Next" @click="go({ d: shiftAnchor(view, anchor, 1) })">›</button>
      </div>
      <h2 class="calendar-title" aria-live="polite">{{ title }}</h2>
      <div class="calendar-views" role="group" aria-label="View">
        <button type="button" :class="view === 'month' ? '' : 'secondary'" :aria-pressed="view === 'month'" @click="go({ view: 'month' })">
          Month
        </button>
        <button type="button" :class="view === 'week' ? '' : 'secondary'" :aria-pressed="view === 'week'" @click="go({ view: 'week' })">
          Week
        </button>
      </div>
    </div>

    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-else-if="isEmpty && !loading" class="muted small">
      Nothing falls in this {{ view }}. Add a reminder from a note to see it here.
    </p>

    <div class="calendar" :class="view" :aria-busy="loading">
      <div class="calendar-head" aria-hidden="true">
        <span v-for="label in labels" :key="label">{{ label }}</span>
      </div>
      <div class="calendar-grid">
        <div
          v-for="day in days"
          role="group"
          :key="day"
          class="day"
          :class="{
            today: day === today,
            outside: view === 'month' && parseDay(day).month !== anchorMonth,
            empty: entriesOf(day).length === 0,
          }"
          :aria-label="dayLabel(day)"
        >
          <header class="day-head">
            <button
              v-if="view === 'month'"
              type="button"
              class="day-num"
              :aria-label="`Week of ${dayLabel(day)}`"
              @click="go({ view: 'week', d: day })"
            >
              {{ parseDay(day).day }}
            </button>
            <span v-else class="day-num">{{ parseDay(day).day }}</span>
            <span class="day-name muted small">{{ dayLabel(day) }}</span>
          </header>
          <ul class="entries">
            <li v-for="entry in shown(day)" :key="entry.key">
              <button
                type="button"
                class="entry"
                :class="{ due: entry.isDue, done: entry.status === 'done' }"
                :title="entryLabel(entry)"
                :aria-label="entryLabel(entry)"
                @click="open(entry)"
              >
                <span class="entry-time">{{ entry.time }}</span>
                <span class="entry-title">{{ entry.title }}</span>
              </button>
            </li>
            <li v-if="view === 'month' && entriesOf(day).length > MAX_PER_MONTH_CELL">
              <button type="button" class="link more" @click="go({ view: 'week', d: day })">
                +{{ entriesOf(day).length - MAX_PER_MONTH_CELL }} more
              </button>
            </li>
          </ul>
        </div>
      </div>
    </div>

    <p class="muted small">Times are in your time zone, {{ tz }}. Filled entries are due dates; the others are heads-ups.</p>
    <PushToggle v-if="push.available && !push.subscribed" />
  </main>
</template>
