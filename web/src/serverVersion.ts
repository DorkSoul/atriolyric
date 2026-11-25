import { setArtEpoch } from './artEpoch'
import { onIdle } from './idle'

// One /api/version check for the whole page, shared by the update prompt
// (which build the server has) and the debug tracing (whether the server
// wants debug events). Asked once the page is idle after it starts - nobody
// is waiting for it - and again whenever the page comes back to the
// foreground (a phone can keep one page alive for days), at most once a
// minute.

export interface ServerVersion {
  /** null in development, where Vite serves the app instead of a build. */
  version: string | null
  debugLog: boolean
  /** The Android app this server offers for download, if it carries one. */
  androidApp: { version: string; size: number } | null
}

const RECHECK_MS = 60_000

const listeners = new Set<(answer: ServerVersion) => void>()
let lastCheck = 0
let latest: ServerVersion | null = null
let started = false

async function check(): Promise<void> {
  if (Date.now() - lastCheck < RECHECK_MS) return
  lastCheck = Date.now()
  try {
    const response = await fetch('/api/version', { cache: 'no-store', credentials: 'same-origin' })
    if (!response.ok) return
    const data: unknown = await response.json()
    const record = data && typeof data === 'object'
      ? (data as { version?: unknown; debug_log?: unknown; art_epoch?: unknown; android_app?: { version?: unknown; size?: unknown } | null })
      : {}
    setArtEpoch(record.art_epoch)
    const app = record.android_app
    latest = {
      version: typeof record.version === 'string' && record.version ? record.version : null,
      debugLog: record.debug_log === true,
      androidApp: app && typeof app.version === 'string' && app.version
        ? { version: app.version, size: typeof app.size === 'number' ? app.size : 0 }
        : null,
    }
    const answer = latest
    listeners.forEach((listener) => listener(answer))
  } catch {
    // Offline or a proxy error page: ask again next time the page is visible.
  }
}

function start() {
  if (started) return
  started = true
  onIdle(() => void check())
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') void check()
  })
}

/** Hears every answer (the last one at once, if there is one). Starts the
 * checks the first time anything subscribes. */
export function onServerVersion(listener: (answer: ServerVersion) => void): () => void {
  listeners.add(listener)
  if (latest) listener(latest)
  start()
  return () => {
    listeners.delete(listener)
  }
}
