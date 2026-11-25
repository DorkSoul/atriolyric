// The refs and state setters passed in are stable, which this rule cannot
// see through the argument object.
/* eslint-disable react-hooks/exhaustive-deps */
import { useEffect, useRef, type RefObject } from 'react'
import { api } from '../api'
import { debugLog } from '../clientDebugLog'
import type { MediaItem, PlaybackStateSnapshot } from '../types'
import { createSilentWavUrl, setMediaSessionPosition } from './mediaSession'
import { keepsMediaSessionThroughSocketLoss, wantsKeepAlive, type OutputState } from './outputState'

export interface UseMediaSessionSyncArgs {
  current: MediaItem | null
  /** Whether this page is the output (see outputState.ts). Another tab
   * taking this device over is not a dropout: this page is silent. */
  outputState: OutputState
  snapshot: PlaybackStateSnapshot | null
  send: (msg: Record<string, unknown>, options?: { queue?: boolean; external?: boolean }) => void
  mediaSessionCommandRef: RefObject<(command: 'play' | 'pause' | 'next' | 'prev') => void>
  activeAudioRef: RefObject<HTMLAudioElement | null>
  videoRef: RefObject<HTMLVideoElement | null>
}

/** The OS media session (lock screen / notification) for the device producing sound. */
export function useMediaSessionSync({
  current,
  outputState,
  snapshot,
  send,
  mediaSessionCommandRef,
  activeAudioRef,
  videoRef,
}: UseMediaSessionSyncArgs) {
  // MediaSession (lock-screen/notification controls) only makes sense for
  // whichever device is actually producing sound - or, while the control
  // plane is down, for whichever device was producing sound a moment ago.
  useEffect(() => {
    if (!('mediaSession' in navigator)) return
    if (!current || outputState !== 'active') {
      // Same dropout distinction as the playback-sync effect's teardown in
      // PlayerContext: a suspended WebSocket is a lost *control plane*, not a
      // transport change, so `isActiveDevice` - which folds in `connected` -
      // flips false on every hidden-socket drop even though this device is
      // still the one playing. Nulling the metadata there destroys the OS
      // media notification, and with it the page's background-audio
      // exemption, which is fatal rather than cosmetic: a hidden page can stop
      // running JS altogether and stay silent until a full reload. The audio
      // is still playing, so keep the notification Chrome Android needs in
      // order to keep the page alive.
      const dropoutKeepsMediaSession = keepsMediaSessionThroughSocketLoss(outputState, {
        hasCurrent: current != null,
        serverPlaying: snapshot?.is_playing === true,
        hidden: document.visibilityState === 'hidden',
      })
      if (dropoutKeepsMediaSession) {
        navigator.mediaSession.playbackState = 'playing'
        return
      }
      // A real hand-off keeps `connected` true and points the snapshot at
      // another device; a stopped/paused queue has nothing to advertise.
      // Both must still tear the session down.
      navigator.mediaSession.metadata = null
      return
    }

    const artworkUrl = new URL(api.artUrl(current.id), window.location.href).href
    navigator.mediaSession.metadata = new MediaMetadata({
      title: current.title,
      artist: current.artist ?? undefined,
      album: current.album ?? undefined,
      // Firefox for Android retrieves artwork from the src URL through its
      // native media layer. Use the canonical same-origin JPEG URL rather
      // than a data/blob URL; the platform owns any rectangular crop.
      artwork: [96, 128, 192, 256, 384, 512].map((size) => ({
        src: artworkUrl,
        sizes: `${size}x${size}`,
        type: 'image/jpeg',
      })),
    })
    for (const [action, command] of [['play', 'play'], ['pause', 'pause'], ['nexttrack', 'next'], ['previoustrack', 'prev']] as const) {
      navigator.mediaSession.setActionHandler(action, () => {
        debugLog('mediasession.action', { action, vis: document.visibilityState })
        mediaSessionCommandRef.current(command)
      })
    }
    navigator.mediaSession.playbackState = snapshot?.is_playing ? 'playing' : 'paused'
    setMediaSessionPosition(current.duration_seconds, (current.kind === 'track' ? activeAudioRef.current : videoRef.current)?.currentTime ?? 0)
  }, [outputState, current, snapshot?.is_playing, send])
}

export interface UseMediaSessionKeepAliveArgs {
  current: MediaItem | null
  outputState: OutputState
  snapshot: PlaybackStateSnapshot | null
  keepAliveAudioRef: RefObject<HTMLAudioElement | null>
}

/** The silent keep-alive player (see KEEP_MEDIA_SESSION_ALIVE). */
export function useMediaSessionKeepAlive({
  current,
  outputState,
  snapshot,
  keepAliveAudioRef,
}: UseMediaSessionKeepAliveArgs) {
  const keepAliveUrlRef = useRef<string | null>(null)
  // Keep a silent player running for as long as this device is producing
  // sound (see KEEP_MEDIA_SESSION_ALIVE). Like the media session above it
  // must ride out a control-plane dropout while hidden, or the socket
  // dropping would end the keepalive and reopen the very gap it closes.
  useEffect(() => {
    const keepAlive = keepAliveAudioRef.current
    if (!keepAlive) return
    const wanted = wantsKeepAlive(outputState, {
      hasCurrent: current != null,
      serverPlaying: snapshot?.is_playing === true,
      hidden: document.visibilityState === 'hidden',
    })
    if (!wanted) {
      keepAlive.pause()
      return
    }
    if (!keepAliveUrlRef.current) {
      keepAliveUrlRef.current = createSilentWavUrl()
      keepAlive.src = keepAliveUrlRef.current
    }
    if (keepAlive.paused) keepAlive.play().catch(() => {
      // Autoplay policy can refuse this until a gesture; the real element is
      // subject to the same rule, so there is nothing more to do.
    })
  }, [outputState, current, snapshot?.is_playing])

  useEffect(() => () => {
    if (keepAliveUrlRef.current) URL.revokeObjectURL(keepAliveUrlRef.current)
    keepAliveUrlRef.current = null
  }, [])
}
