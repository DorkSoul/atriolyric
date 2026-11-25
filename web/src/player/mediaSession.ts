// The mobile media session keepalive and its position state.

import { isNativeApp } from '../native/bridge'

// Chrome on Android 15 gives up audio focus and tears down the media session
// (and the foreground-service notification with it) the instant no element is
// playing - which is exactly what happens between two tracks. The next track's
// fresh focus request then has to beat Android's audio-focus hardening, which
// denies it when the app has no foreground service. It is a race the request
// can lose within a second ("Audio focus request blocked by hardening"); Chrome
// then pauses the new track and, silent and hidden, the page is frozen a
// couple of minutes later. A silent looping element keeps a player alive
// across every hand-off, so focus is never abandoned and there is no request
// to deny.
//
// That behaviour is Chrome for Android's. The keepalive is applied to every mobile
// browser as a precaution: they all drive a platform media session and audio
// focus from the same "is any element playing" signal, and a phone is where a
// page is routinely locked mid-queue. The other mobile browsers (iOS Safari,
// Firefox and Samsung Internet on Android, ...) have NOT been tested with it.
// Desktop browsers are left alone - nothing there revokes focus in the gap, and
// a second player could take over a desktop Now Playing widget.
export function isMobileBrowser(): boolean {
  if (typeof navigator === 'undefined') return false
  if (/Android|iPhone|iPad|iPod|Mobile|Silk|Opera Mini/i.test(navigator.userAgent)) return true
  // iPadOS reports a desktop Mac user agent; the touch screen gives it away.
  return navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1
}
// Not in the Android app: its native players hold the session, and an element
// playing in the page would take the audio focus away from them.
export const KEEP_MEDIA_SESSION_ALIVE = isMobileBrowser() && !isNativeApp()

// Whether a playing video hands its sound to an audio-only element while the
// page is hidden (mobile browsers pause a hidden <video>). In the Android app
// too, where that element is played natively and so survives the screen off.
export const BACKGROUND_VIDEO_AUDIO = isMobileBrowser()

// One second of 8 kHz mono silence, made in memory so there is nothing to fetch.
export function createSilentWavUrl(): string {
  const sampleRate = 8000
  const buffer = new ArrayBuffer(44 + sampleRate)
  const view = new DataView(buffer)
  const writeTag = (offset: number, tag: string) => {
    for (let i = 0; i < tag.length; i += 1) view.setUint8(offset + i, tag.charCodeAt(i))
  }
  writeTag(0, 'RIFF')
  view.setUint32(4, 36 + sampleRate, true)
  writeTag(8, 'WAVE')
  writeTag(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true) // PCM
  view.setUint16(22, 1, true) // mono
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate, true)
  view.setUint16(32, 1, true)
  view.setUint16(34, 8, true)
  writeTag(36, 'data')
  view.setUint32(40, sampleRate, true)
  new Uint8Array(buffer, 44).fill(128) // 8-bit PCM silence
  return URL.createObjectURL(new Blob([buffer], { type: 'audio/wav' }))
}

// With two players Chrome can no longer work out the lock-screen position
// itself (it reports it as unknown and the progress bar disappears), so say
// so explicitly. Chrome extrapolates from this while the session is playing.
export function setMediaSessionPosition(durationSeconds: number | null | undefined, positionSeconds: number) {
  if (!KEEP_MEDIA_SESSION_ALIVE || !('mediaSession' in navigator)) return
  if (durationSeconds == null || !Number.isFinite(durationSeconds) || durationSeconds <= 0) return
  try {
    navigator.mediaSession.setPositionState({
      duration: durationSeconds,
      position: Math.min(Math.max(0, positionSeconds), durationSeconds),
      playbackRate: 1,
    })
  } catch {
    // An out-of-range value is only a cosmetic loss.
  }
}
