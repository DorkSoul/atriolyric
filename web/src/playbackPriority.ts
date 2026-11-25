// Getting the song the user just asked for to start playing outranks
// everything else the app downloads: album art, and the whole-file prefetch
// of the next track. Browsers cap simultaneous connections per host, so
// those would otherwise sit in front of the new track's stream request. While
// playback is starting (the active element is loading or rebuffering), the
// art loader and the prefetch hold off, and resume the moment it can play.

// Never hold album art back longer than this, so a stream that is slow or
// broken can't leave every image blank.
const MAX_HOLD_MS = 4000
// The whole-file download of the next track waits longer: it is tens of MB on
// the same connection, and on a slow link starting it after four seconds leaves
// the song the user asked for with a share of the bandwidth before it has
// played a note.
const MAX_PREFETCH_HOLD_MS = 15000

let starting = false
let prefetchHeld = false
let holdTimer: number | undefined
let prefetchTimer: number | undefined
const listeners = new Set<() => void>()

function apply(nextStarting: boolean, nextPrefetchHeld: boolean) {
  if (starting === nextStarting && prefetchHeld === nextPrefetchHeld) return
  starting = nextStarting
  prefetchHeld = nextPrefetchHeld
  for (const listener of [...listeners]) listener()
}

export function setPlaybackStarting(next: boolean) {
  window.clearTimeout(holdTimer)
  window.clearTimeout(prefetchTimer)
  holdTimer = prefetchTimer = undefined
  if (next) {
    holdTimer = window.setTimeout(() => apply(false, prefetchHeld), MAX_HOLD_MS)
    prefetchTimer = window.setTimeout(() => setPlaybackStarting(false), MAX_PREFETCH_HOLD_MS)
  }
  apply(next, next)
}

export function isPlaybackStarting(): boolean {
  return starting
}

/** Calls `listener` whenever the starting state (or the prefetch hold) changes. */
export function onPlaybackStartingChange(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

/** Resolves once the track being started plays, fails or stalls, or after
 * MAX_PREFETCH_HOLD_MS (at once if nothing is starting). */
export function whenPlaybackStarted(signal: AbortSignal): Promise<void> {
  if (!prefetchHeld) return Promise.resolve()
  return new Promise((resolve) => {
    const done = () => {
      off()
      signal.removeEventListener('abort', done)
      resolve()
    }
    const off = onPlaybackStartingChange(() => {
      if (!prefetchHeld) done()
    })
    signal.addEventListener('abort', done)
  })
}
