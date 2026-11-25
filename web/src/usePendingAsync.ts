import { useEffect, useEffectEvent, useRef, useState } from 'react'
import { delay, useAsync, type AsyncState } from './useAsync'

// The server's "pending" convention (lyrics, info dialogs, top tracks, the
// want-list and catalog pages): on a cache miss it answers at once with what
// it has stored, `pending: true`, and fetches the rest in the background. The
// client asks the same URL again on this schedule until `pending` is false. A
// look-up that fails on the server is not re-queued for a while and answers
// `pending: false`, so asking always ends; the time limit is a backstop.

/** How long to wait between two asks while the answer is still pending. */
export const PENDING_REASK_MS = 2_000
/** When to stop asking and keep what is on screen. */
export const PENDING_GIVE_UP_MS = 60_000

export interface Pendable {
  pending?: boolean | null
}

/** Asks `load`, hands each answer to `onAnswer`, and while the answer is
 * pending waits and asks again - until it is not, the time limit passes or
 * `signal` aborts (which rejects, like `delay`). Resolves with the last
 * answer. The first ask's failure rejects; a failed re-ask resolves with the
 * answer already given, so what is on screen stays. */
export async function askUntilSettled<T extends Pendable>(
  load: (signal: AbortSignal) => Promise<T>,
  signal: AbortSignal,
  onAnswer?: (answer: T) => void,
): Promise<T> {
  const started = Date.now()
  let answer = await load(signal)
  onAnswer?.(answer)
  while (answer.pending && Date.now() - started < PENDING_GIVE_UP_MS) {
    await delay(PENDING_REASK_MS, signal)
    try {
      answer = await load(signal)
    } catch (error) {
      if (signal.aborted) throw error
      return answer
    }
    onAnswer?.(answer)
  }
  return answer
}

export interface PendingAsyncState<T> extends AsyncState<T> {
  /** The answer on screen is partial and the rest is still being asked for
   * (show a quiet "Fetching more…"). False once it is complete, the time
   * limit passed or a re-ask failed. */
  fetchingMore: boolean
}

/** `useAsync` for an endpoint that may answer `pending`: the first answer
 * shows straight away, then the same request is repeated every
 * PENDING_REASK_MS while the answer is pending (PENDING_GIVE_UP_MS at most),
 * each answer replacing the last. Asking stops when the inputs change, the
 * component unmounts (a dialog or page closes) or its view is hidden, and
 * resumes when it is shown again. `setData` with a complete answer (a
 * Refresh) stops it too. */
export function usePendingAsync<T extends Pendable>(
  loader: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
  options: { enabled?: boolean; keepPrevious?: boolean; staleMs?: number } = {},
): PendingAsyncState<T> {
  const enabled = options.enabled ?? true
  const state = useAsync(loader, deps, options)
  const load = useEffectEvent((signal: AbortSignal) => loader(signal))
  const { data, status, setData } = state
  const pending = status === 'ready' && data?.pending === true
  // When the current run of pending answers began; reset when an answer is
  // complete, so a later pending answer (new inputs) gets the full time.
  const startedRef = useRef<number | null>(null)
  // The pending answer after which asking stopped (time limit, failure).
  const [stoppedAt, setStoppedAt] = useState<T | undefined>(undefined)

  useEffect(() => {
    if (!pending) {
      startedRef.current = null
      return
    }
    if (!enabled || stoppedAt === data) return
    startedRef.current ??= Date.now()
    const controller = new AbortController()
    const elapsed = Date.now() - startedRef.current
    const stop = () => setStoppedAt(() => data)
    if (elapsed >= PENDING_GIVE_UP_MS) {
      stop()
      return
    }
    delay(PENDING_REASK_MS, controller.signal)
      .then(() => load(controller.signal))
      .then(
        (next) => setData(next),
        () => {
          if (!controller.signal.aborted) stop()
        },
      )
    return () => controller.abort()
  }, [pending, data, enabled, stoppedAt, setData])

  // Also true while a reload is on its way with a pending answer still showing.
  const fetchingMore = data?.pending === true && status !== 'error' && enabled && stoppedAt !== data
  return { ...state, fetchingMore }
}
