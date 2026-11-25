// The Android app and the server it talks to: the server can carry the app's
// APK (GET /api/download/app/android). A browser is offered it as a download;
// the installed app compares the server's copy with its own version and can
// fetch and install a newer one.

import { useEffect, useState } from 'react'
import { onServerVersion } from '../serverVersion'
import { isNativeApp, nativeCall, type ShellState } from './bridge'

export const ANDROID_APP_URL = '/api/download/app/android'

/** Negative when `a` is older than `b`. Compares the numbers of "1.2.3". */
export function compareVersions(a: string, b: string): number {
  const parts = (version: string) => version.split(/[.+-]/).map((part) => Number.parseInt(part, 10) || 0)
  const left = parts(a)
  const right = parts(b)
  for (let index = 0; index < 3; index += 1) {
    const difference = (left[index] ?? 0) - (right[index] ?? 0)
    if (difference !== 0) return difference
  }
  return 0
}

let installedVersion: Promise<string | null> | null = null

/** The version of the installed app (null in a browser). */
function installedAppVersion(): Promise<string | null> {
  if (!isNativeApp()) return Promise.resolve(null)
  installedVersion ??= nativeCall<ShellState>('shellState').then((state) => state.appVersion || null, () => null)
  return installedVersion
}

export interface AndroidAppStatus {
  /** What the server offers; null when it carries no app. */
  offered: { version: string; size: number } | null
  /** The installed app's version; null in a browser. */
  installed: string | null
  /** True in the app when the server's copy is newer than the installed one. */
  updateAvailable: boolean
}

export function useAndroidApp(): AndroidAppStatus {
  const [offered, setOffered] = useState<AndroidAppStatus['offered']>(null)
  const [installed, setInstalled] = useState<string | null>(null)
  useEffect(() => onServerVersion((answer) => setOffered(answer.androidApp)), [])
  useEffect(() => {
    let live = true
    void installedAppVersion().then((version) => {
      if (live) setInstalled(version)
    })
    return () => {
      live = false
    }
  }, [])
  return {
    offered,
    installed,
    updateAvailable: !!offered && !!installed && compareVersions(installed, offered.version) < 0,
  }
}

/** In the app: fetches the server's APK and opens Android's installer. */
export function installAppUpdate(): Promise<void> {
  return nativeCall('installUpdate', { url: new URL(ANDROID_APP_URL, window.location.origin).href })
}
