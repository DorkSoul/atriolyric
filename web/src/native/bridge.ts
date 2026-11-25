// The Android app's native side (mobile/, plugin "AtrioNative"). The page is
// the same bundle the browser loads; these calls exist only when it runs
// inside the app, where Capacitor injects `window.Capacitor` before any
// script. Nothing here imports Capacitor, so the PWA carries none of it.

interface CapacitorGlobal {
  isNativePlatform?: () => boolean
  nativePromise?: (plugin: string, method: string, options?: unknown) => Promise<unknown>
  addListener?: (plugin: string, event: string, callback: (data: unknown) => void) => { remove: () => Promise<void> }
}

const PLUGIN = 'AtrioNative'

function capacitor(): CapacitorGlobal | undefined {
  return (globalThis as { Capacitor?: CapacitorGlobal }).Capacitor
}

/** Whether this page runs inside the Android app. */
export function isNativeApp(): boolean {
  const cap = capacitor()
  return !!cap && cap.isNativePlatform?.() === true && typeof cap.nativePromise === 'function'
}

export function nativeCall<T = void>(method: string, options?: Record<string, unknown>): Promise<T> {
  const cap = capacitor()
  if (!cap?.nativePromise) return Promise.reject(new Error('Not running in the app'))
  return cap.nativePromise(PLUGIN, method, options ?? {}) as Promise<T>
}

/** Subscribes to a native event; returns the unsubscribe function. */
export function onNative<T>(event: string, callback: (data: T) => void): () => void {
  const handle = capacitor()?.addListener?.(PLUGIN, event, callback as (data: unknown) => void)
  return () => {
    void handle?.remove()
  }
}

/** Tells the app the page's colours, so the areas behind the phone's status
 * and navigation bars match it: the page background on top, the bottom bar's
 * surface below. Read from the palette in force (styles/base.css). A no-op in
 * a browser, and in an app too old to know the call. */
export function syncSystemBars(): void {
  if (!isNativeApp()) return
  const root = document.documentElement
  const style = getComputedStyle(root)
  const top = style.getPropertyValue('--bg').trim()
  const bottom = style.getPropertyValue('--surface').trim()
  const light = root.getAttribute('data-theme') === 'light'
  nativeCall('setSystemBars', { top, bottom, light }).catch(() => {})
}

/** One native player (see PlayerHub.Slot.state in the app). */
export interface SlotState {
  slot: string
  loadId: number
  mediaId: number
  playbackState: 'idle' | 'buffering' | 'ready' | 'ended'
  playWhenReady: boolean
  playing: boolean
  position: number
  duration: number | null
  buffered: number
  error: string | null
  /** How many errors this native player has had; an error is new when this grows. */
  errorCount?: number
  /** Whether the native side will start this slot itself when the playing one ends. */
  armed?: boolean
  active: boolean
}

export type DownloadState = 'queued' | 'downloading' | 'completed' | 'failed' | 'removing' | 'stopped'

/** One song or video kept on the device. */
export interface NativeDownload {
  mediaId: number
  kind: 'track' | 'video' | 'music_video'
  title: string
  artist: string
  album: string
  /** 'original', a bitrate cap in kbps ('192') or a video height ('720'). */
  quality: string
  qualityLabel: string
  duration: number
  addedAt: number
  state: DownloadState
  bytes: number
  totalBytes: number | null
  percent: number | null
  hasArt: boolean
  /** When the art file last changed (ms), 0 without one; missing in older apps. */
  artVersion?: number
}

export interface ShellState {
  serverUrl: string | null
  remote: boolean
  reason: string | null
  appVersion: string
}
