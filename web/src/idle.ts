/** Runs `task` once the browser is idle (or after `timeoutMs` at the latest),
 * so work nobody is waiting for never competes with what the screen needs.
 * Returns a cancel function. */
export function onIdle(task: () => void, timeoutMs = 2000): () => void {
  if (typeof window.requestIdleCallback === 'function') {
    const handle = window.requestIdleCallback(() => task(), { timeout: timeoutMs })
    return () => window.cancelIdleCallback(handle)
  }
  const timer = window.setTimeout(task, Math.min(300, timeoutMs))
  return () => window.clearTimeout(timer)
}
