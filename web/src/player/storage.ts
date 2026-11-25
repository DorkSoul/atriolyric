// What the player keeps in this browser: the resume flags, the device token and a device-chosen accent colour.

import type { RadioStation } from '../types'

export const PLAYBACK_RESUME_KEY = 'atriolyric.playback-resume'
export const PLAYBACK_DEVICE_TOKEN_KEY = 'atriolyric.playback-device-token'
export const RADIO_RESUME_KEY = 'atriolyric.radio-resume'

export function readPlaybackResumeFlag(): boolean {
  try {
    return window.sessionStorage.getItem(PLAYBACK_RESUME_KEY) === '1'
  } catch {
    return false
  }
}

export function writePlaybackResumeFlag(shouldResume: boolean): void {
  try {
    if (shouldResume) window.sessionStorage.setItem(PLAYBACK_RESUME_KEY, '1')
    else window.sessionStorage.removeItem(PLAYBACK_RESUME_KEY)
  } catch {
    // Private browsing modes can deny storage. Server-side playback state
    // still makes the normal reconnect path work in that case.
  }
}

export function playbackDeviceToken(): string {
  try {
    const existing = window.localStorage.getItem(PLAYBACK_DEVICE_TOKEN_KEY)
    if (existing) return existing
    const token = typeof crypto?.randomUUID === 'function'
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`
    window.localStorage.setItem(PLAYBACK_DEVICE_TOKEN_KEY, token)
    return token
  } catch {
    // Storage can be unavailable in private browsing.  A per-page token
    // still preserves the normal connection while that page remains open.
    return `${Date.now()}-${Math.random().toString(36).slice(2)}`
  }
}

export function readRadioResumeStation(): RadioStation | null {
  try {
    const raw = window.sessionStorage.getItem(RADIO_RESUME_KEY)
    if (!raw) return null
    const station = JSON.parse(raw) as RadioStation
    return typeof station?.id === 'number' && typeof station?.name === 'string' ? station : null
  } catch {
    return null
  }
}

export function writeRadioResumeStation(station: RadioStation | null): void {
  try {
    if (station) window.sessionStorage.setItem(RADIO_RESUME_KEY, JSON.stringify(station))
    else window.sessionStorage.removeItem(RADIO_RESUME_KEY)
  } catch {
    // Private browsing modes can deny storage; the stream still works for
    // the current page and track playback remains server-backed.
  }
}

// Accent colors live on the user account. A device's settings blob may still
// carry a color chosen there; it is migrated once on login so the user keeps
// that color instead of silently falling back to the default.
export function readLegacyAccentColor(): string | null {
  try {
    const raw = window.localStorage.getItem('atriolyric.settings')
    if (!raw) return null
    const parsed = JSON.parse(raw) as { accentColor?: unknown }
    return typeof parsed.accentColor === 'string' &&
      /^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/.test(parsed.accentColor)
      ? parsed.accentColor
      : null
  } catch {
    return null
  }
}
