// The refs and state setters passed in are stable, which this rule cannot
// see through the argument object.
/* eslint-disable react-hooks/exhaustive-deps */
import { useCallback, useEffect, useRef, type RefObject } from 'react'
import { api } from '../api'
import type { AppSettings } from '../settings'
import type { MediaItem, PlaybackStateSnapshot } from '../types'
import { PLAY_INTENT_CONFIRM_MS } from './socketPolicy'

export interface UseOutputClaimArgs {
  lastServerStateRef: RefObject<PlaybackStateSnapshot | null>
  deviceIdRef: RefObject<number | null>
  isActiveDeviceRef: RefObject<boolean>
  mediaCacheRef: RefObject<Map<number, MediaItem>>
  activeAudioRef: RefObject<HTMLAudioElement | null>
  audioRef: RefObject<HTMLAudioElement | null>
  elementItemsRef: RefObject<WeakMap<HTMLMediaElement, MediaItem>>
  settings: AppSettings
  stopRadio: () => void
  noteElementItem: (element: HTMLMediaElement, item: MediaItem) => void
  elementHasSrc: (element: HTMLMediaElement, streamUrl: string) => boolean
  setElementSrc: (element: HTMLMediaElement, streamUrl: string) => void
}

/** Starting a queue on a device that may become the active one: start the
 * element inside the user's gesture, and stop it if the server does not hand over. */
export function useOutputClaim({
  lastServerStateRef,
  deviceIdRef,
  isActiveDeviceRef,
  mediaCacheRef,
  activeAudioRef,
  audioRef,
  elementItemsRef,
  settings,
  stopRadio,
  noteElementItem,
  elementHasSrc,
  setElementSrc,
}: UseOutputClaimArgs) {
  // Starting a new queue on a device makes it the active one on the server
  // when nothing is heard elsewhere: playback stopped, or the active device
  // offline or gone (claim_output_for_requester). Sequence: device A plays,
  // its browser closes without pausing, device C starts an album - C plays
  // it. Mirrored here only to start the element inside the user's gesture,
  // which a mobile browser requires; the snapshot naming this device then
  // finds the track already loaded. Not when a connected device is playing:
  // the command remote-controls it. If the server does not hand over after
  // all, the element is stopped again.
  const claimTimerRef = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(claimTimerRef.current), [])
  const primeClaimedPlayback = useCallback((startId: number) => {
    const state = lastServerStateRef.current
    if (deviceIdRef.current == null || isActiveDeviceRef.current || !state) return
    const activeOnline = state.devices.some((d) => d.id === state.active_device_id && d.is_connected)
    if (activeOnline && state.is_playing) return
    // A library context (the Songs tab, an artist or album) is tracks only;
    // an id list may hold videos, which play in another element.
    const item = mediaCacheRef.current.get(startId)
    const element = activeAudioRef.current ?? audioRef.current
    if ((item && item.kind !== 'track') || !element) return
    stopRadio()
    const src = api.streamUrl(startId, settings.maxBitrateKbps)
    if (item) noteElementItem(element, item)
    else elementItemsRef.current.delete(element)
    if (!elementHasSrc(element, src)) {
      setElementSrc(element, src)
      element.load()
    }
    element.play().catch(() => {})
    window.clearTimeout(claimTimerRef.current)
    claimTimerRef.current = window.setTimeout(() => {
      if (!isActiveDeviceRef.current) element.pause()
    }, PLAY_INTENT_CONFIRM_MS)
  }, [elementHasSrc, noteElementItem, setElementSrc, settings.maxBitrateKbps, stopRadio])

  return { primeClaimedPlayback }
}
