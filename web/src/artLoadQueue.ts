// A grid view (Artists/Albums especially) can mount hundreds to thousands
// of <Art> tiles at once, each wanting its own image. Letting every one of
// them request immediately floods the server with a burst of simultaneous
// work and looks like nothing is happening until it all resolves at once.
// This caps how many are actually in flight at a time and decides which go
// first: tiles on screen now before tiles merely near it, so after a scroll
// or a tab switch the user sees what they are looking at fill in first.
//
// The loader does the fetching itself (fetch + AbortController, handing the
// tile an object URL) rather than leaving it to <img src>: removing an <img>
// doesn't reliably cancel its download, and a download nobody is waiting on
// any more would otherwise hold a connection ahead of the ones that matter.
//
// Three lanes:
//   visible   - the tile intersects the viewport. FIFO.
//   near      - within the tile's preload margin but off screen. FIFO, and
//               only started while no visible tile is waiting. While
//               visible tiles wait for a slot, running near loads are
//               interrupted (most recently started first) and requeued, one
//               per waiting visible tile.
//   set-aside - URLs whose tile went away before its image arrived. They
//               are still downloaded (into the browser's HTTP cache, for
//               when the user comes back), newest first, but only once
//               nothing else is loading and the app's data requests have
//               been quiet for a moment - and any new tile interrupts them.

import { isNetworkBusy, onNetworkIdle } from './networkActivity'
import { isPlaybackStarting, onPlaybackStartingChange } from './playbackPriority'

const MAX_CONCURRENT = 6
const SET_ASIDE_CONCURRENCY = 2
const SET_ASIDE_IDLE_MS = 500
const SET_ASIDE_LIMIT = 300

export type ArtLoadResult = { objectUrl: string } | { failed: true }

export interface ArtLoadHandle {
  /** Moves a still-waiting request between the visible and near lanes. */
  setVisible(visible: boolean): void
  /** Gives up on this request: `onDone` never fires afterwards. An
   * unfinished download is interrupted and set aside for later. */
  cancel(): void
}

interface Entry {
  url: string
  visible: boolean
  onDone: (result: ArtLoadResult) => void
  state: 'queued' | 'running' | 'done' | 'cancelled'
  controller: AbortController | null
}

interface SetAsideJob {
  url: string
  controller: AbortController
}

const visibleQueue: Entry[] = []
const nearQueue: Entry[] = []
/** Running visible/near loads, oldest start first. */
const running: Entry[] = []
/** Newest first. */
const setAside: string[] = []
const setAsideRunning = new Set<SetAsideJob>()
/** URLs downloaded in full this session - already in the HTTP cache, so
 * never worth setting aside again. */
const loaded = new Set<string>()
/** URLs the server answered "no art" (204) this session. Asking again would
 * only send the server - and anything watching its access log, like a
 * CrowdSec bouncer - another empty answer, so the tile fails at once. */
const missing = new Set<string>()

let pumpScheduled = false
let setAsideTimer: number | undefined
let lastPrimaryActivityAt = 0
let lastNetworkIdleAt = 0

onPlaybackStartingChange(schedulePump)

onNetworkIdle(() => {
  lastNetworkIdleAt = Date.now()
  schedulePump()
})

/** Pumping waits for a microtask so every change in one React commit (a
 * whole tab's tiles cancelling, the next tab's tiles asking) lands before
 * any slot is handed out. */
function schedulePump() {
  if (pumpScheduled) return
  pumpScheduled = true
  queueMicrotask(pump)
}

function removeFrom<T>(list: T[], item: T): boolean {
  const i = list.indexOf(item)
  if (i === -1) return false
  list.splice(i, 1)
  return true
}

function queueFor(entry: Entry): Entry[] {
  return entry.visible ? visibleQueue : nearQueue
}

function releaseRunning(entry: Entry) {
  removeFrom(running, entry)
  lastPrimaryActivityAt = Date.now()
}

function addToSetAside(url: string) {
  if (loaded.has(url) || missing.has(url)) return
  removeFrom(setAside, url)
  setAside.unshift(url)
  if (setAside.length > SET_ASIDE_LIMIT) setAside.length = SET_ASIDE_LIMIT
}

function pump() {
  pumpScheduled = false

  if (isPlaybackStarting()) {
    // The song the user just asked for is still starting: nothing here may
    // sit in front of its stream request. Interrupt what is downloading and
    // put it back (visible first) until playback can start.
    for (const entry of running.splice(0).reverse()) {
      entry.controller?.abort()
      entry.controller = null
      entry.state = 'queued'
      queueFor(entry).unshift(entry)
    }
    abortSetAside()
    return
  }

  // Make room for every waiting visible tile, not just one: a scroll can
  // turn several running loads into near ones at once (a row the user
  // scrolled past), and each must give way, or on-screen art waits behind
  // art nobody is looking at.
  while (visibleQueue.length > MAX_CONCURRENT - running.length) {
    const victim = running.findLast((entry) => !entry.visible)
    if (!victim) break
    victim.controller?.abort()
    victim.controller = null
    releaseRunning(victim)
    victim.state = 'queued'
    nearQueue.unshift(victim)
  }

  while (running.length < MAX_CONCURRENT) {
    const next = visibleQueue.shift() ?? nearQueue.shift()
    if (!next) break
    start(next)
  }

  pumpSetAside()
}

function start(entry: Entry) {
  const controller = new AbortController()
  entry.state = 'running'
  entry.controller = controller
  running.push(entry)
  lastPrimaryActivityAt = Date.now()
  void run(entry, controller)
}

async function run(entry: Entry, controller: AbortController) {
  let result: ArtLoadResult = { failed: true }
  try {
    const res = await fetch(entry.url, { signal: controller.signal })
    if (res.status === 204) {
      missing.add(entry.url)
      removeFrom(setAside, entry.url)
    } else if (res.ok) {
      const blob = await res.blob()
      loaded.add(entry.url)
      removeFrom(setAside, entry.url)
      result = { objectUrl: URL.createObjectURL(blob) }
    } else {
      void res.body?.cancel()
    }
  } catch {
    // An abort was already accounted for by whoever aborted it.
    if (controller.signal.aborted) return
  }
  if (!settle(entry, controller, result) && 'objectUrl' in result) {
    URL.revokeObjectURL(result.objectUrl)
  }
}

/** Hands `result` to the tile, unless it no longer wants it. Returns
 * whether it was delivered. */
function settle(entry: Entry, controller: AbortController, result: ArtLoadResult): boolean {
  if (entry.state === 'cancelled' || entry.state === 'done') return false
  if (entry.state === 'queued') {
    // Preempted just as its download finished - deliver it anyway.
    removeFrom(queueFor(entry), entry)
  } else if (entry.controller !== controller) {
    // A newer attempt (after a preemption) is already running.
    return false
  } else {
    releaseRunning(entry)
  }
  entry.state = 'done'
  entry.controller = null
  schedulePump()
  entry.onDone(result)
  return true
}

function abortSetAside() {
  for (const job of [...setAsideRunning].reverse()) {
    job.controller.abort()
    setAsideRunning.delete(job)
    addToSetAside(job.url)
  }
}

function pumpSetAside() {
  if (setAsideTimer !== undefined) {
    window.clearTimeout(setAsideTimer)
    setAsideTimer = undefined
  }
  if (visibleQueue.length > 0 || nearQueue.length > 0 || running.length > 0) {
    abortSetAside()
    return
  }
  if (setAside.length === 0 || isNetworkBusy()) return
  const quietAt = Math.max(lastPrimaryActivityAt, lastNetworkIdleAt) + SET_ASIDE_IDLE_MS
  const wait = quietAt - Date.now()
  if (wait > 0) {
    setAsideTimer = window.setTimeout(() => {
      setAsideTimer = undefined
      schedulePump()
    }, wait)
    return
  }
  while (setAsideRunning.size < SET_ASIDE_CONCURRENCY) {
    const url = setAside.shift()
    if (url === undefined) break
    const job = { url, controller: new AbortController() }
    setAsideRunning.add(job)
    void runSetAside(job)
  }
}

async function runSetAside(job: SetAsideJob) {
  try {
    const res = await fetch(job.url, { signal: job.controller.signal })
    if (res.status === 204) {
      missing.add(job.url)
    } else if (res.ok) {
      // Read the whole body so the download completes into the HTTP cache.
      await res.arrayBuffer()
      loaded.add(job.url)
    } else {
      void res.body?.cancel()
    }
  } catch {
    // Aborted (already put back by abortSetAside) or failed (dropped).
  } finally {
    if (setAsideRunning.delete(job)) schedulePump()
  }
}

/** Asks for `url` to be loaded, as soon as its lane gets a turn. `onDone`
 * fires once, with an object URL the caller now owns (and must revoke) or
 * a failure (a non-OK or empty 204 response, or a network error). */
export function loadArt(
  url: string,
  visible: boolean,
  onDone: (result: ArtLoadResult) => void,
): ArtLoadHandle {
  const entry: Entry = { url, visible, onDone, state: 'queued', controller: null }
  if (missing.has(url)) {
    // Known to have no art: fail without a request. Still asynchronous, so
    // the caller's effect has finished before its callback runs.
    entry.state = 'done'
    queueMicrotask(() => {
      if (entry.state === 'done') onDone({ failed: true })
    })
    return { setVisible() {}, cancel() { entry.state = 'cancelled' } }
  }
  // Asked for again (e.g. StrictMode's dev double-mount, or scrolling back)
  // - it is primary work now, not something to fetch later.
  removeFrom(setAside, url)
  queueFor(entry).push(entry)
  schedulePump()

  return {
    setVisible(nextVisible) {
      if (entry.visible === nextVisible) return
      if (entry.state === 'queued') removeFrom(queueFor(entry), entry)
      entry.visible = nextVisible
      if (entry.state === 'queued') queueFor(entry).push(entry)
      schedulePump()
    },
    cancel() {
      if (entry.state === 'queued') {
        removeFrom(queueFor(entry), entry)
        addToSetAside(url)
      } else if (entry.state === 'running') {
        entry.controller?.abort()
        releaseRunning(entry)
        addToSetAside(url)
      }
      entry.state = 'cancelled'
      entry.controller = null
      schedulePump()
    },
  }
}
