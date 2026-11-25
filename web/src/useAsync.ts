import { useCallback, useEffect, useEffectEvent, useRef, useState } from 'react'
import { currentLibraryGeneration } from './api'

export type AsyncStatus = 'loading' | 'ready' | 'error'

export interface AsyncState<T> {
  status: AsyncStatus
  /** The last result that belongs to the current inputs. While `status` is
   * 'loading' it is only set for a `reload()` or with `keepPrevious`, so a
   * list for one album never shows under another's header. */
  data: T | undefined
  error: unknown
  /** Starts again after a failure, from a clean "loading" state. */
  retry: () => void
  /** Fetches again while leaving the current data on screen (after an edit). */
  reload: () => void
  /** Replaces the data without a request (an update the server already returned). */
  setData: (next: T | ((previous: T | undefined) => T)) => void
}

interface Options {
  /** Keep the previous data visible while loading for NEW inputs, not just
   * for a `reload()`. For a list that refetches as the user types. */
  keepPrevious?: boolean
  /** False holds the request back (it depends on something not there yet). */
  enabled?: boolean
  /** For a view in a kept-mounted tab: shown again within this many ms of
   * its last answer, it keeps that answer instead of asking again. A
   * `retry`/`reload`, new inputs or a library change (clearBrowseCache)
   * still ask at once. 0 (the default) asks on every show. */
  staleMs?: number
  /** With `staleMs`: something the data depends on that should not refetch
   * while the view is showing, but makes a kept answer stale for the next
   * show when it changes (the track playing, for "Recently played"). */
  staleKey?: unknown
}

/** How long a kept-mounted tab's data counts as fresh when it is shown again. */
export const KEPT_TAB_STALE_MS = 60_000

function sameDeps(a: readonly unknown[], b: readonly unknown[]): boolean {
  return a.length === b.length && a.every((value, index) => Object.is(value, b[index]))
}

/** One way to load data for a view: `{status, data, error}` with a Retry.
 *
 * - A run is ignored (and its AbortSignal aborted) when `deps` change or the
 *   component unmounts, so a slow answer for an old input never lands, and no
 *   state is set after unmount.
 * - The state that belongs to new `deps` is derived while rendering, not in an
 *   effect, so the first render with new inputs already says "loading".
 * - Inside a hidden `<Activity>` its effect is disconnected, so nothing is
 *   fetched or set while the view is out of sight; on reveal the run starts
 *   again and the data already on screen stays until the fresh answer lands. */
export function useAsync<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
  options: Options = {},
): AsyncState<T> {
  const { keepPrevious = false, enabled = true, staleMs = 0, staleKey } = options
  const [run, setRun] = useState(0)
  // `answeredAt`: when the data on screen came back from a load for exactly
  // these inputs (and under which library generation and stale key) - the
  // proof `staleMs` needs that it may keep it. Dropped whenever the inputs,
  // the run or `enabled` change, or the data is replaced by hand.
  const [state, setState] = useState<{
    status: AsyncStatus
    data: T | undefined
    error: unknown
    answeredAt?: { at: number; generation: number; staleKey: unknown }
  }>({
    status: 'loading',
    data: undefined,
    error: null,
  })
  const [seen, setSeen] = useState({ deps, run, enabled })
  const load = useEffectEvent((signal: AbortSignal) => loader(signal))
  const staleKeyRef = useRef(staleKey)
  useEffect(() => {
    staleKeyRef.current = staleKey
  })
  // Read when the effect (re)connects - a hidden tab shown again - without
  // being one of its dependencies.
  const stillFresh = useEffectEvent((): boolean => {
    const answered = state.answeredAt
    return (
      staleMs > 0 &&
      state.status === 'ready' &&
      answered != null &&
      answered.generation === currentLibraryGeneration() &&
      Object.is(answered.staleKey, staleKeyRef.current) &&
      Date.now() - answered.at < staleMs
    )
  })

  if (!sameDeps(seen.deps, deps) || seen.run !== run || seen.enabled !== enabled) {
    const depsChanged = !sameDeps(seen.deps, deps)
    setSeen({ deps, run, enabled })
    setState((previous) => ({
      status: 'loading',
      data: depsChanged && !keepPrevious ? undefined : previous.data,
      error: null,
    }))
  }

  useEffect(() => {
    if (!enabled) return
    // Shown again soon after its last answer: that answer is still current.
    if (stillFresh()) return
    const generation = currentLibraryGeneration()
    const controller = new AbortController()
    let live = true
    load(controller.signal).then(
      (data) => {
        if (!live) return
        setState({
          status: 'ready',
          data,
          error: null,
          answeredAt: { at: Date.now(), generation, staleKey: staleKeyRef.current },
        })
      },
      (error: unknown) => {
        if (live) setState((previous) => ({ status: 'error', data: previous.data, error }))
      },
    )
    return () => {
      live = false
      controller.abort()
    }
    // The loader is an effect event: callers pass a fresh closure every
    // render, and `deps` says when its result would differ.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, run, enabled])

  const retry = useCallback(() => setRun((value) => value + 1), [])
  const setData = useCallback((next: T | ((previous: T | undefined) => T)) => {
    setState((previous) => ({
      status: 'ready',
      error: null,
      data: typeof next === 'function' ? (next as (previous: T | undefined) => T)(previous.data) : next,
    }))
  }, [])

  return { status: state.status, data: state.data, error: state.error, retry, reload: retry, setData }
}

/** Waits `ms`, or rejects when `signal` aborts (which `useAsync` ignores). */
export function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, ms)
    signal.addEventListener(
      'abort',
      () => {
        window.clearTimeout(timer)
        reject(signal.reason)
      },
      { once: true },
    )
  })
}
