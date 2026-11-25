import { useEffect, useState, type RefObject } from 'react'

/** True once `ref`'s element has come within `margin` of the scrolling page
 * area (`.app-main`), and stays true after - for cards that fetch their data
 * only when they are about to be seen. `idleAfterMs` also turns it on after
 * the element has been mounted (its tab open) that long. Hidden tabs
 * disconnect their effects, so neither the observer nor the timer runs there.
 * The second value turns it on by hand (focus moving into the card). */
export function useNearViewport(
  ref: RefObject<Element | null>,
  options: { margin?: number; idleAfterMs?: number } = {},
): [boolean, () => void] {
  const { margin = 240, idleAfterMs } = options
  const [near, setNear] = useState(() => typeof IntersectionObserver === 'undefined')
  useEffect(() => {
    if (near) return
    const el = ref.current
    if (!el) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) setNear(true)
      },
      { root: el.closest('.app-main'), rootMargin: `${margin}px 0px` },
    )
    observer.observe(el)
    const timer = idleAfterMs == null ? undefined : window.setTimeout(() => setNear(true), idleAfterMs)
    return () => {
      observer.disconnect()
      window.clearTimeout(timer)
    }
  }, [near, ref, margin, idleAfterMs])
  return [near, () => setNear(true)]
}

type Status = 'idle' | 'loading' | 'ready' | 'error'

/** Runs `load` once `active` turns true (and again after `retry`), and says
 * where it is. A failure is a state to show, never an unhandled rejection. */
export function useLazyLoad(
  active: boolean,
  load: () => Promise<unknown>,
): { status: Status; retry: () => void } {
  const [attempt, setAttempt] = useState(0)
  const [result, setResult] = useState<{ attempt: number; failed: boolean } | null>(null)
  useEffect(() => {
    if (!active) return
    let live = true
    load().then(
      () => live && setResult({ attempt, failed: false }),
      () => live && setResult({ attempt, failed: true }),
    )
    return () => {
      live = false
    }
    // `load` is a fresh closure every render; the attempt is what re-asks.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, attempt])
  const status: Status = !active
    ? 'idle'
    : result?.attempt === attempt
      ? result.failed
        ? 'error'
        : 'ready'
      : 'loading'
  return { status, retry: () => setAttempt((n) => n + 1) }
}
