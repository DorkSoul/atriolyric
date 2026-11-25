// Sends uncaught browser errors to the server log (POST /api/client-log) so a
// bug a user hits shows up in `docker logs` next to the request lines. Capped
// per page load and de-duplicated; a failed report is silently dropped.

const MAX_REPORTS = 10
const IGNORED = [/ResizeObserver loop/i, /^Script error\.?$/i]

const seen = new Set<string>()
let sent = 0

function report(message: string, source?: string, stack?: string) {
  const text = message.slice(0, 500)
  if (!text || sent >= MAX_REPORTS || seen.has(text) || IGNORED.some((re) => re.test(text))) return
  seen.add(text)
  sent += 1
  try {
    void fetch('/api/client-log', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      keepalive: true,
      body: JSON.stringify({
        level: 'error',
        message: text,
        source: source?.slice(0, 300),
        stack: stack?.slice(0, 2000),
        page: window.location.hash.slice(0, 300) || window.location.pathname,
      }),
    }).catch(() => {})
  } catch {
    // reporting must never throw
  }
}

/** An error React caught while rendering (an error boundary's, or one that
 * reached the root): the same capped, de-duplicated report as an uncaught one.
 * `componentStack` says which component threw. */
export function reportRenderError(error: unknown, componentStack?: string | null): void {
  const message = error instanceof Error ? error.message : String(error)
  const stack = [error instanceof Error ? error.stack : undefined, componentStack ? `Component stack:${componentStack}` : undefined]
    .filter(Boolean)
    .join('\n')
  report(`Render error: ${message}`, undefined, stack || undefined)
}

export function installClientErrorReporting(): void {
  window.addEventListener('error', (event) => {
    const where = event.filename ? `${event.filename}:${event.lineno}:${event.colno}` : undefined
    report(event.message || 'Uncaught error', where, event.error instanceof Error ? event.error.stack : undefined)
  })
  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason
    if (reason instanceof Error) report(`Unhandled rejection: ${reason.message}`, undefined, reason.stack)
    else report(`Unhandled rejection: ${String(reason)}`)
  })
}
