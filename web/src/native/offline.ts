// Whether the Android app can reach its server right now. There is no
// separate offline mode to choose: while the server answers, the app is the
// web app; while it does not, the same pages show what was loaded before
// (offlineCache.ts) and only downloads play (localPlayback.ts). A browser is
// never "offline" in this sense - it has nothing to play without the server.

import { useSyncExternalStore } from 'react'
import { isNativeApp } from './bridge'

const PROBE_TIMEOUT_MS = 4_000
// One unanswered check is not proof: a phone just back from sleep, or a
// mobile link busy with a download, can leave a small request waiting for
// seconds. Before the app changes over to its downloads it asks once more, a
// little later and with more patience. With no network at all both fail at
// once, so that case is still found in about a second.
const SECOND_PROBE_AFTER_MS = 1_000
const SECOND_PROBE_TIMEOUT_MS = 6_000
const PROBE_EVERY_MS = 15_000

let offline = false
/** When the server last answered anything (a request, the playback socket). */
let lastAnswerAt = 0
let probing: Promise<boolean> | null = null
let timer: number | undefined
let started = false
const listeners = new Set<() => void>()

export function isOffline(): boolean {
  return offline
}

function setOffline(next: boolean) {
  if (next === offline) return
  offline = next
  window.clearInterval(timer)
  // Offline: keep asking, so the app is itself again soon after the server is back.
  if (offline) timer = window.setInterval(() => void probeServer(), PROBE_EVERY_MS)
  for (const listener of [...listeners]) listener()
}

/** Asks the server for its version. It is there when it answers with its
 * version, or when a sign-in gateway in front of it answers instead (a
 * redirect to its login, or a refusal) - the same rule the app uses before
 * it loads the page (`connect` in AtrioNativePlugin.kt). Anything else (a
 * hotel Wi-Fi's own page, a proxy's "not found") is not the server, and the
 * app is better off with its downloads than with requests failing one by one. */
async function askVersion(timeoutMs: number): Promise<boolean> {
  const controller = new AbortController()
  const giveUp = window.setTimeout(() => controller.abort(), timeoutMs)
  try {
    const answer = await fetch('/api/version', { cache: 'no-store', redirect: 'manual', signal: controller.signal })
    if (answer.type === 'opaqueredirect' || answer.status === 401 || answer.status === 403) return true
    if (answer.status !== 200) return false
    const body: unknown = await answer.json().catch(() => null)
    return !!body && typeof body === 'object' && 'version' in body
  } catch {
    return false
  } finally {
    window.clearTimeout(giveUp)
  }
}

/** Whether the server is there. While the app is online, "no" takes two
 * unanswered checks and nothing else answered in the meantime; while it is
 * offline one check is enough (the next comes in PROBE_EVERY_MS). */
async function serverReachable(): Promise<boolean> {
  const since = Date.now()
  if (await askVersion(PROBE_TIMEOUT_MS)) return true
  if (offline) return false
  await new Promise((resolve) => window.setTimeout(resolve, SECOND_PROBE_AFTER_MS))
  if (lastAnswerAt >= since) return true
  return (await askVersion(SECOND_PROBE_TIMEOUT_MS)) || lastAnswerAt >= since
}

function probeServer(): Promise<boolean> {
  probing ??= serverReachable().then((reachable) => {
    probing = null
    setOffline(!reachable)
    return reachable
  })
  return probing
}

function start() {
  if (started || !isNativeApp()) return
  started = true
  window.addEventListener('online', () => void probeServer())
  window.addEventListener('offline', () => void probeServer())
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && offline) void probeServer()
  })
}

/** A request got no answer (or the playback socket would not open): find out
 * whether the server is gone. Resolves true when it is. */
export function checkOffline(): Promise<boolean> {
  if (!isNativeApp()) return Promise.resolve(false)
  start()
  return probeServer().then((reachable) => !reachable)
}

/** The server just answered a request. */
export function noteServerAnswered(): void {
  lastAnswerAt = Date.now()
  if (offline) setOffline(false)
}

export function onOfflineChange(listener: () => void): () => void {
  start()
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

/** True while the app cannot reach its server. Always false in a browser. */
export function useOffline(): boolean {
  return useSyncExternalStore(onOfflineChange, isOffline)
}
