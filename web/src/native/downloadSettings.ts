// The Android app's download settings: the qualities songs and videos are
// saved at (apart from the streaming quality: someone may stream at 192 kbps
// and keep originals) and whether downloads wait for Wi-Fi. Kept per device
// like the other display/playback settings. A module of its own so that the
// background library copy (offlineLibrary.ts) can read the Wi-Fi choice
// without importing the download code, which imports the API client.

import { useSyncExternalStore } from 'react'
import { STREAM_QUALITIES, type StreamQuality } from '../settings'
import { isNativeApp, nativeCall } from './bridge'

export const VIDEO_DOWNLOAD_QUALITIES = ['original', '1080', '720', '480'] as const
export type VideoDownloadQuality = (typeof VIDEO_DOWNLOAD_QUALITIES)[number]

export interface DownloadSettings {
  /** A bitrate cap in kbps, or null for the original file. */
  musicKbps: StreamQuality | null
  video: VideoDownloadQuality
  /** Downloads (and the app's background fetches) wait for a network that is
   * not metered (Wi-Fi), so a big playlist does not use mobile data. */
  wifiOnly: boolean
}

const STORAGE_KEY = 'atriolyric.downloadSettings'
let settings: DownloadSettings | null = null
const listeners = new Set<() => void>()

export function downloadSettings(): DownloadSettings {
  if (settings) return settings
  let parsed: Partial<DownloadSettings> = {}
  try {
    parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}') as Partial<DownloadSettings>
  } catch {
    // Unreadable: the defaults.
  }
  settings = {
    musicKbps: STREAM_QUALITIES.includes(parsed.musicKbps as StreamQuality) ? (parsed.musicKbps as StreamQuality) : null,
    video: VIDEO_DOWNLOAD_QUALITIES.includes(parsed.video as VideoDownloadQuality) ? (parsed.video as VideoDownloadQuality) : 'original',
    wifiOnly: parsed.wifiOnly === true,
  }
  return settings
}

/** Tells the app's download manager whether to wait for Wi-Fi. The app keeps
 * the choice itself too, so downloads already queued follow it before any
 * page has loaded. */
export function sendRequirements(wifiOnly: boolean) {
  if (!isNativeApp()) return
  void nativeCall('downloadSetRequirements', { wifiOnly }).catch(() => {})
}

export function updateDownloadSettings(patch: Partial<DownloadSettings>): void {
  const before = downloadSettings()
  settings = { ...before, ...patch }
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(settings))
  } catch {
    // Kept for this visit only.
  }
  if (settings.wifiOnly !== before.wifiOnly) sendRequirements(settings.wifiOnly)
  for (const listener of [...listeners]) listener()
}

/** Whether background work that moves a lot of data (the collection sync,
 * the daily library copy) should wait: Wi-Fi only is chosen and the phone is
 * on a metered network now. An app too old to say counts as not metered. */
export async function shouldWaitForWifi(): Promise<boolean> {
  if (!isNativeApp() || !downloadSettings().wifiOnly) return false
  try {
    const { metered } = await nativeCall<{ connected: boolean; metered: boolean }>('networkInfo')
    return metered === true
  } catch {
    return false
  }
}

export function useDownloadSettings(): DownloadSettings {
  return useSyncExternalStore((listener) => {
    listeners.add(listener)
    return () => {
      listeners.delete(listener)
    }
  }, downloadSettings)
}
