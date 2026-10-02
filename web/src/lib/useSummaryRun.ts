/** One "Summarize" button's state for Vue: idle or working, the last problem, and the key to reuse. */
import { onBeforeUnmount, ref, shallowRef } from 'vue'
import type { SummaryJob } from '../api/types'
import { runSummary, type SummaryDeps, type SummaryProblem } from './summaryJob'
import { uuid4 } from './uuid'

export function useSummaryRun(
  deps: Pick<SummaryDeps, 'get'> & { create: SummaryDeps['create'] },
  onDone: (job: SummaryJob) => void,
  onSettled: () => void = () => {},
) {
  const working = ref(false)
  const problem = shallowRef<SummaryProblem | null>(null)
  let key = ''
  let reuseKey = false
  let run = 0

  async function start() {
    if (working.value) return
    const mine = ++run
    problem.value = null
    working.value = true
    // A POST that got no answer may have made the job: its retry reuses the key.
    const attempt = reuseKey && key ? key : uuid4()
    key = attempt
    const outcome = await runSummary(deps, attempt, { cancelled: () => mine !== run })
    if (mine !== run || outcome.kind === 'cancelled') return
    working.value = false
    if (outcome.kind === 'done') {
      reuseKey = false
      onDone(outcome.job)
    } else {
      reuseKey = outcome.problem.reuseKey
      problem.value = outcome.problem
    }
    onSettled()
  }

  function cancel() {
    run++
    working.value = false
  }

  onBeforeUnmount(() => {
    run++
  })

  return { working, problem, start, cancel }
}
