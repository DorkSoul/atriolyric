// Development-phase tracing for playback glitches (songs restarting after the
// app is minimised or the phone is locked). Batches events to
// POST /api/client-log/debug so they land in `docker logs` beside the server's
// own request and WebSocket lines. Never throws; a failed flush is retried.
//
// Off unless the server says so: the server reports `debug_log` on
// /api/version (true only when its verbose logging is on). Until that answer
// is true nothing here is installed - no media-element patching, no buffering,
// no requests - and `debugLog` is a no-op, so a normal install pays nothing
// and a locked phone is not woken to send batches the server would discard.
// It also stays off while nobody is signed in: the endpoint needs a session,
// so on the login page every flush would be a 401, and a burst of those can get
// a visitor IP-banned by a proxy's CrowdSec rules. AuthContext
// reports sign-in and sign-out through setDebugLogSignedIn.
// The answer is re-checked whenever the page becomes visible again, because
// the flag changes by redeploying.

import { onServerVersion } from './serverVersion'

type Scalar = string | number | boolean | null
type Fields = Record<string, unknown>

interface DebugEvent {
  at: string
  event: string
  fields: Record<string, Scalar>
}

const SESSION = Math.random().toString(36).slice(2, 10)
const FLUSH_INTERVAL_MS = 2000
const MAX_BUFFERED = 500
// The server refuses a report with more than 50 events (MAX_DEBUG_EVENTS).
// A bigger batch would never be accepted, and would be put back and sent
// again every two seconds for good.
const BATCH_SIZE = 50

let buffer: DebugEvent[] = []
let installed = false
let enabled = false
// The server wants debug events / someone is signed in; tracing runs only
// while both are true.
let serverWants = false
let signedIn = false
// Undo functions for everything switched on by enable().
let teardown: Array<() => void> = []

function scalarise(fields: Fields): Record<string, Scalar> {
  const out: Record<string, Scalar> = {}
  for (const [key, value] of Object.entries(fields)) {
    if (value == null || typeof value === 'boolean' || typeof value === 'string') out[key] = (value ?? null) as Scalar
    else if (typeof value === 'number') out[key] = Number.isFinite(value) ? value : String(value)
    else {
      try {
        out[key] = JSON.stringify(value)?.slice(0, 200) ?? null
      } catch {
        out[key] = String(value).slice(0, 200)
      }
    }
  }
  return out
}

/** Record one event. Cheap and safe to call from anywhere. */
export function debugLog(event: string, fields: Fields = {}): void {
  if (!enabled) return
  try {
    const entry = { at: new Date().toISOString(), event, fields: scalarise(fields) }
    console.debug('[Atriolyric debug]', event, entry.fields)
    buffer.push(entry)
    if (buffer.length > MAX_BUFFERED) buffer = buffer.slice(-MAX_BUFFERED)
  } catch {
    // tracing must never break the app
  }
}

/** `final` is for a page going away (pagehide, freeze, hidden): it is sent
 * with sendBeacon, else a keepalive fetch, so it survives the page. The
 * periodic flush is an ordinary fetch. */
function flush(final = false): void {
  if (!enabled || buffer.length === 0) return
  const batch = buffer.slice(0, BATCH_SIZE)
  const body = JSON.stringify({ session: SESSION, events: batch })
  buffer = buffer.slice(batch.length)
  try {
    if (final && navigator.sendBeacon?.('/api/client-log/debug', new Blob([body], { type: 'application/json' }))) {
      return
    }
    void fetch('/api/client-log/debug', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      ...(final ? { keepalive: true } : {}),
      body,
    })
      .then((res) => {
        // Only a server fault may improve by retrying. A 4xx says this
        // batch will be refused again (signed out, too many, malformed), so
        // it is dropped instead of blocking everything behind it.
        if (res.status === 401) setDebugLogSignedIn(false)
        if (res.status >= 500) buffer = [...batch, ...buffer].slice(-MAX_BUFFERED)
      })
      .catch(() => {
        buffer = [...batch, ...buffer].slice(-MAX_BUFFERED)
      })
  } catch {
    buffer = [...batch, ...buffer].slice(-MAX_BUFFERED)
  }
}

let nextElementId = 1
const elementIds = new WeakMap<HTMLMediaElement, number>()
const watched = new WeakSet<HTMLMediaElement>()
const lastTime = new WeakMap<HTMLMediaElement, number>()

function elementTag(el: HTMLMediaElement): string {
  let id = elementIds.get(el)
  if (id == null) {
    id = nextElementId++
    elementIds.set(el, id)
  }
  const kind = el.tagName === 'VIDEO' ? 'video' : 'audio'
  return `${kind}#${id}${el.isConnected ? '' : '(detached)'}`
}

export function describeMedia(el: HTMLMediaElement): Fields {
  return {
    el: elementTag(el),
    src: (el.currentSrc || el.getAttribute('src') || '').replace(/^https?:\/\/[^/]+/, '').slice(0, 120),
    time: Number.isFinite(el.currentTime) ? Number(el.currentTime.toFixed(2)) : null,
    duration: Number.isFinite(el.duration) ? Number(el.duration.toFixed(2)) : null,
    paused: el.paused,
    ended: el.ended,
    seeking: el.seeking,
    ready: el.readyState,
    network: el.networkState,
    error: el.error?.code ?? null,
    vis: document.visibilityState,
  }
}

const MEDIA_EVENTS = [
  'loadstart', 'loadedmetadata', 'canplay', 'play', 'playing', 'pause', 'ended', 'error',
  'stalled', 'waiting', 'seeking', 'seeked', 'emptied', 'abort', 'suspend', 'durationchange',
] as const

function watchElement(el: HTMLMediaElement): void {
  if (watched.has(el)) return
  watched.add(el)
  for (const name of MEDIA_EVENTS) {
    el.addEventListener(name, () => debugLog(`media.${name}`, describeMedia(el)))
  }
  // A song "restarting" shows up as playback time jumping back without a seek.
  el.addEventListener('timeupdate', () => {
    const previous = lastTime.get(el)
    const now = el.currentTime
    lastTime.set(el, now)
    if (previous != null && now < previous - 1 && !el.seeking) {
      debugLog('media.time_regressed', { ...describeMedia(el), from: Number(previous.toFixed(2)) })
    }
  })
}

function callerStack(): string {
  const lines = (new Error().stack ?? '').split('\n').slice(3, 7)
  return lines.map((line) => line.trim().replace(/^at /, '').replace(/https?:\/\/[^/]+/, '')).join(' < ').slice(0, 400)
}

function patchMediaElement(): void {
  const proto = HTMLMediaElement.prototype
  for (const method of ['play', 'pause', 'load'] as const) {
    const original = proto[method] as (...args: unknown[]) => unknown
    teardown.push(() => {
      ;(proto as unknown as Record<string, unknown>)[method] = original
    })
    ;(proto as unknown as Record<string, unknown>)[method] = function (this: HTMLMediaElement, ...args: unknown[]) {
      try {
        watchElement(this)
        debugLog(`call.${method}`, { ...describeMedia(this), caller: callerStack() })
      } catch {
        // ignore
      }
      return original.apply(this, args)
    }
  }
  for (const prop of ['currentTime', 'src'] as const) {
    const descriptor = Object.getOwnPropertyDescriptor(proto, prop)
    if (!descriptor?.set) continue
    teardown.push(() => Object.defineProperty(proto, prop, descriptor))
    Object.defineProperty(proto, prop, {
      ...descriptor,
      set(this: HTMLMediaElement, value: unknown) {
        try {
          watchElement(this)
          debugLog(`set.${prop}`, { ...describeMedia(this), value: String(value).replace(/^https?:\/\/[^/]+/, '').slice(0, 120), caller: callerStack() })
        } catch {
          // ignore
        }
        descriptor.set!.call(this, value)
      },
    })
  }
}

function lifecycleFields(): Fields {
  const connection = (navigator as Navigator & { connection?: { effectiveType?: string; downlink?: number; rtt?: number } }).connection
  return {
    vis: document.visibilityState,
    focus: document.hasFocus(),
    online: navigator.onLine,
    net: connection?.effectiveType ?? null,
    downlink: connection?.downlink ?? null,
    rtt: connection?.rtt ?? null,
  }
}

function enable(): void {
  if (enabled) return
  enabled = true
  try {
    const standalone = window.matchMedia?.('(display-mode: standalone)').matches
    debugLog('app.start', {
      ua: navigator.userAgent.slice(0, 160),
      standalone,
      // "reload" / "back_forward" here with no user action, or was_discarded,
      // means the browser threw the page away and reloaded it (a restart).
      nav_type: (performance.getEntriesByType?.('navigation')[0] as PerformanceNavigationTiming | undefined)?.type ?? null,
      was_discarded: (document as Document & { wasDiscarded?: boolean }).wasDiscarded ?? null,
      url: window.location.pathname + window.location.hash.slice(0, 80),
      ...lifecycleFields(),
    })
    patchMediaElement()
    const listen = (target: EventTarget, name: string, handler: (event: Event) => void) => {
      target.addEventListener(name, handler)
      teardown.push(() => target.removeEventListener(name, handler))
    }
    if (navigator.serviceWorker) {
      listen(navigator.serviceWorker, 'controllerchange', () => debugLog('life.sw_controllerchange', lifecycleFields()))
    }
    const lifecycle: Array<[EventTarget, string]> = [
      [document, 'visibilitychange'], [document, 'freeze'], [document, 'resume'],
      [window, 'pagehide'], [window, 'pageshow'], [window, 'focus'], [window, 'blur'],
      [window, 'online'], [window, 'offline'], [window, 'beforeunload'],
    ]
    for (const [target, name] of lifecycle) {
      listen(target, name, (event) => {
        const persisted = (event as PageTransitionEvent).persisted
        debugLog(`life.${name}`, { ...lifecycleFields(), ...(persisted != null ? { persisted } : {}) })
        if (name === 'pagehide' || name === 'freeze' || (name === 'visibilitychange' && document.visibilityState === 'hidden')) {
          flush(true)
        }
      })
    }
    const connection = (navigator as Navigator & { connection?: EventTarget }).connection
    if (connection) listen(connection, 'change', () => debugLog('life.connection_change', lifecycleFields()))
    // Gaps between ticks reveal the page being frozen/suspended in the background.
    let lastTick = Date.now()
    const timer = window.setInterval(() => {
      const now = Date.now()
      const gap = now - lastTick
      lastTick = now
      if (gap > 5000) debugLog('life.timer_gap', { gap_ms: gap, ...lifecycleFields() })
      flush()
    }, FLUSH_INTERVAL_MS)
    teardown.push(() => window.clearInterval(timer))
  } catch {
    // tracing must never break the app
  }
}

function disable(): void {
  if (!enabled) return
  enabled = false
  for (const undo of teardown.reverse()) {
    try {
      undo()
    } catch {
      // ignore
    }
  }
  teardown = []
  buffer = []
}

function sync(): void {
  if (serverWants && signedIn) enable()
  else disable()
}

/** Called by AuthContext: tracing sends nothing until a user is signed in,
 * and stops when the session ends (or the server answers 401). */
export function setDebugLogSignedIn(value: boolean): void {
  signedIn = value
  sync()
}

export function installClientDebugLogging(): void {
  if (installed) return
  installed = true
  try {
    // The shared /api/version check (asked on idle, and again when the page
    // is visible again) says whether the server wants debug events.
    onServerVersion((answer) => {
      serverWants = answer.debugLog
      sync()
    })
  } catch {
    // tracing must never break the app
  }
}
