// The refs and state setters passed in are stable, which this rule cannot
// see through the argument object.
/* eslint-disable react-hooks/exhaustive-deps */
import { useCallback, useEffect, useRef, type RefObject } from 'react'
import { api } from '../api'
import { whenPlaybackStarted } from '../playbackPriority'
import type { StreamQuality } from '../settings'
import type { MediaItem, PlaybackStateSnapshot } from '../types'
import { armNativeNext } from '../native/nativeAudio'
import type { TrackDownloadSample } from './adaptiveBitrate'
import {
  forgetBlobSizesExcept,
  heldBlobBytes,
  MAX_TRACK_PREFETCH_BYTES,
  notifyTrackBlobRemembered,
  onTrackBlobRemembered,
  prefetchAheadCount,
  playingQueueIndex,
  PREFETCH_BUDGET,
  prefetchTrackBlob,
  streamTrackId,
} from './prefetch'

export interface UsePrefetchCacheArgs {
  trackBlobsRef: RefObject<Map<string, string>>
  prefetchRef: RefObject<{ src: string; controller: AbortController } | null>
  current: MediaItem | null
  queue: MediaItem[]
  snapshot: PlaybackStateSnapshot | null
}

/** In-memory copies (blob: URLs) of the previous and current track and the
 * ones ahead in the queue (see PREFETCH_AHEAD). */
export function usePrefetchCache({
  trackBlobsRef,
  prefetchRef,
  current,
  queue,
  snapshot,
}: UsePrefetchCacheArgs) {
  const keepTrackIdsRef = useRef(new Set<number>())
  const previousTrackIdRef = useRef<number | null>(null)
  const trackHistoryRef = useRef<{ currentId: number | null }>({ currentId: null })
  // The exact stream if it is held; otherwise a copy of the just-finished
  // track fetched at another quality (going back should not re-download it).
  const findTrackBlob = useCallback((streamUrl: string): string | undefined => {
    const blobs = trackBlobsRef.current
    const exact = blobs.get(streamUrl)
    if (exact) return exact
    const id = streamTrackId(streamUrl)
    if (id == null || id !== previousTrackIdRef.current) return undefined
    for (const [src, blob] of blobs) if (streamTrackId(src) === id) return blob
    return undefined
  }, [])

  // Drop every blob that is not for the previous, current or an upcoming track.
  const pruneTrackBlobs = useCallback(() => {
    const keep = keepTrackIdsRef.current
    const blobs = trackBlobsRef.current
    for (const [src, blob] of blobs) {
      const id = streamTrackId(src)
      if (id != null && keep.has(id)) continue
      blobs.delete(src)
      URL.revokeObjectURL(blob)
    }
    forgetBlobSizesExcept(blobs.values())
  }, [])

  const rememberTrackBlob = useCallback((streamUrl: string, blobUrl: string) => {
    const old = trackBlobsRef.current.get(streamUrl)
    if (old && old !== blobUrl) URL.revokeObjectURL(old)
    trackBlobsRef.current.set(streamUrl, blobUrl)
    pruneTrackBlobs()
    notifyTrackBlobRemembered()
  }, [pruneTrackBlobs])

  // The track that was playing before this one is the single "previous" copy.
  useEffect(() => {
    if (!current || current.kind !== 'track') return
    const history = trackHistoryRef.current
    if (history.currentId !== current.id) {
      if (history.currentId != null) previousTrackIdRef.current = history.currentId
      history.currentId = current.id
    }
    const index = playingQueueIndex(queue, current, snapshot?.queue_index)
    const upcoming = queue
      .slice(index + 1, index + 1 + prefetchAheadCount())
      .map((item) => (item.kind === 'track' ? item.id : null))
    keepTrackIdsRef.current = new Set(
      [current.id, previousTrackIdRef.current, ...upcoming]
        .filter((id): id is number => id != null),
    )
    pruneTrackBlobs()
  }, [current, queue, snapshot?.queue_index, pruneTrackBlobs])

  useEffect(() => {
    const blobs = trackBlobsRef.current
    return () => {
      prefetchRef.current?.controller.abort()
      for (const blobUrl of blobs.values()) URL.revokeObjectURL(blobUrl)
      blobs.clear()
    }
  }, [])

  return { findTrackBlob, rememberTrackBlob }
}

/** A finished download of an upcoming track, for the adaptive quality:
 * the quality it was fetched at and the track that is next at that moment. */
export interface UpcomingTrackDownload extends TrackDownloadSample {
  nextTrackId: number
  quality: StreamQuality | null
}

export interface UseStandbyPrefetchArgs {
  audioRef: RefObject<HTMLAudioElement | null>
  preloadAudioRef: RefObject<HTMLAudioElement | null>
  activeAudioRef: RefObject<HTMLAudioElement | null>
  isActiveDevice: boolean
  isVideo: boolean
  current: MediaItem | null
  queue: MediaItem[]
  snapshot: PlaybackStateSnapshot | null
  prefetchRef: RefObject<{ src: string; controller: AbortController } | null>
  elementSrcRef: RefObject<WeakMap<HTMLMediaElement, string>>
  trackBlobsRef: RefObject<Map<string, string>>
  nextTrackBitrate: StreamQuality | null
  noteElementItem: (element: HTMLMediaElement, item: MediaItem) => void
  elementHasSrc: (element: HTMLMediaElement, streamUrl: string) => boolean
  setElementSrc: (element: HTMLMediaElement, streamUrl: string) => void
  rememberTrackBlob: (streamUrl: string, blobUrl: string) => void
  /** Called (through the ref, so with the latest render's state) for every
   * copy of an upcoming track that finished downloading. */
  upcomingDownloadRef: RefObject<(download: UpcomingTrackDownload) => void>
}

/** Keeps the next track warm in the standby audio element on the active device. */
export function useStandbyPrefetch({
  audioRef,
  preloadAudioRef,
  activeAudioRef,
  isActiveDevice,
  isVideo,
  current,
  queue,
  snapshot,
  prefetchRef,
  elementSrcRef,
  trackBlobsRef,
  nextTrackBitrate,
  noteElementItem,
  elementHasSrc,
  setElementSrc,
  rememberTrackBlob,
  upcomingDownloadRef,
}: UseStandbyPrefetchArgs) {
  const standbyWantRef = useRef<{ element: HTMLAudioElement; src: string } | null>(null)
  // Keep one upcoming track warm on the active device. A second audio
  // element lets the browser fetch and buffer the next file while the
  // current one plays; when playback advances, the already-loaded element
  // is promoted above. `preload="auto"` is intentionally browser-managed -
  // the web platform has no portable way to request exactly N seconds of a
  // compressed audio stream.
  useEffect(() => {
    const audio = audioRef.current
    const preloadAudio = preloadAudioRef.current
    if (!audio || !preloadAudio || !isActiveDevice || isVideo || !current) return
    if (!activeAudioRef.current) activeAudioRef.current = audio
    const next = queue[playingQueueIndex(queue, current, snapshot?.queue_index) + 1]
    const standby = activeAudioRef.current === audio ? preloadAudio : audio
    // In the Android app the native side starts an armed standby itself when
    // the playing track ends, so the queue moves on even while the page is
    // frozen in the background. Only a real next track: with repeat-one the
    // page restarts the same track instead (see handleEnded), and the
    // standby's queue neighbour must not start. No-op in a browser.
    armNativeNext(activeAudioRef.current ?? audio, false)
    armNativeNext(standby, !!next && next.kind === 'track' && next.id !== current.id && snapshot?.repeat_mode !== 'one')
    if (!next || next.kind !== 'track') {
      standbyWantRef.current = null
      prefetchRef.current?.controller.abort()
      prefetchRef.current = null
      standby.pause()
      standby.removeAttribute('src')
      delete standby.dataset.streamSrc
      elementSrcRef.current.delete(standby)
      standby.load()
      return
    }
    const nextSrc = api.streamUrl(next.id, nextTrackBitrate)
    noteElementItem(standby, next)
    standbyWantRef.current = { element: standby, src: nextSrc }
    if (elementHasSrc(standby, nextSrc)) return
    if (trackBlobsRef.current.has(nextSrc)) {
      setElementSrc(standby, nextSrc)
      standby.preload = 'auto'
      standby.load()
      return
    }
    // The standby may still be streaming a track that is no longer the next
    // one (the element that was playing before a skip keeps its source). Stop
    // that download: it shares the connection with the track being started.
    // A copy in memory costs nothing and stays.
    const standbySrc = standby.getAttribute('src')
    if (standbySrc && !standbySrc.startsWith('blob:')) {
      standby.pause()
      standby.removeAttribute('src')
      delete standby.dataset.streamSrc
      elementSrcRef.current.delete(standby)
      standby.load()
    }
    // This effect re-runs more often than the next track changes (a new
    // `current` object, a queue edit elsewhere in the queue). Let a download that
    // is already fetching this exact track carry on instead of restarting it.
    if (prefetchRef.current?.src === nextSrc) return
    prefetchRef.current?.controller.abort()
    const controller = new AbortController()
    prefetchRef.current = { src: nextSrc, controller }
    // The track being started comes first: the next one waits until it plays.
    void whenPlaybackStarted(controller.signal).then(() => (
      controller.signal.aborted
        ? null
        : prefetchTrackBlob(nextSrc, controller.signal, undefined, (sample) => {
          upcomingDownloadRef.current({ ...sample, nextTrackId: next.id, quality: nextTrackBitrate })
        })
    )).then((blobUrl) => {
      if (prefetchRef.current?.controller !== controller) {
        if (blobUrl) URL.revokeObjectURL(blobUrl)
        return
      }
      prefetchRef.current = null
      if (blobUrl) rememberTrackBlob(nextSrc, blobUrl)
      // Whichever element is the standby *now* takes the track, provided it
      // is still the next one and has not become the playing element.
      const want = standbyWantRef.current
      if (!want || want.src !== nextSrc || want.element === activeAudioRef.current) return
      if (elementHasSrc(want.element, nextSrc)) return
      // No copy (too large, or the request failed): stream it.
      setElementSrc(want.element, nextSrc)
      want.element.preload = 'auto'
      want.element.load()
    })
  }, [current, elementHasSrc, isActiveDevice, isVideo, noteElementItem, queue, rememberTrackBlob, setElementSrc, snapshot?.queue_index, snapshot?.repeat_mode, nextTrackBitrate])

  useAheadPrefetch({ isActiveDevice, isVideo, current, queue, snapshot, trackBlobsRef, nextTrackBitrate, rememberTrackBlob, upcomingDownloadRef })
}

interface UseAheadPrefetchArgs {
  isActiveDevice: boolean
  isVideo: boolean
  current: MediaItem | null
  queue: MediaItem[]
  snapshot: PlaybackStateSnapshot | null
  trackBlobsRef: RefObject<Map<string, string>>
  nextTrackBitrate: StreamQuality | null
  rememberTrackBlob: (streamUrl: string, blobUrl: string) => void
  upcomingDownloadRef: RefObject<(download: UpcomingTrackDownload) => void>
}

/** Holds the tracks after the next one in memory too (up to PREFETCH_AHEAD
 * queue items ahead, within PREFETCH_BUDGET), so a phone that loses the
 * network for a while keeps playing. One download at a time, in play order,
 * and only once the next track's own copy is in: the hand-off to the next
 * track comes first, and a track that is starting waits for nothing. */
function useAheadPrefetch({
  isActiveDevice,
  isVideo,
  current,
  queue,
  snapshot,
  trackBlobsRef,
  nextTrackBitrate,
  rememberTrackBlob,
  upcomingDownloadRef,
}: UseAheadPrefetchArgs) {
  const aheadRef = useRef<{ src: string; controller: AbortController } | null>(null)

  useEffect(() => {
    const abortAhead = () => {
      aheadRef.current?.controller.abort()
      aheadRef.current = null
    }
    const index = playingQueueIndex(queue, current, snapshot?.queue_index)
    const next = queue[index + 1]
    const count = prefetchAheadCount()
    if (!isActiveDevice || isVideo || !current || !next || next.kind !== 'track' || count <= 1) {
      abortAhead()
      return
    }
    const nextSrc = api.streamUrl(next.id, nextTrackBitrate)
    // At the quality the next track is fetched at: that is what each of
    // these will be played at when it becomes the next one. A copy at any
    // other quality would never be used, so it goes.
    const ahead = queue.slice(index + 2, index + 1 + count).filter((item) => item.kind === 'track')
    const wanted = ahead.map((item) => api.streamUrl(item.id, nextTrackBitrate))
    const aheadIds = new Set(ahead.map((item) => item.id))
    const blobs = trackBlobsRef.current
    for (const [src, blob] of blobs) {
      const id = streamTrackId(src)
      if (id == null || !aheadIds.has(id) || id === current.id || id === next.id || wanted.includes(src)) continue
      blobs.delete(src)
      URL.revokeObjectURL(blob)
    }
    // A download for a track that is still wanted carries on; any other stops.
    if (aheadRef.current && !wanted.includes(aheadRef.current.src)) abortAhead()

    let active = true
    let stopped = false
    const step = () => {
      if (!active || stopped || aheadRef.current) return
      if (!trackBlobsRef.current.has(nextSrc)) return
      const src = wanted.find((candidate) => !trackBlobsRef.current.has(candidate))
      if (!src) return
      const room = PREFETCH_BUDGET.bytes - heldBlobBytes(trackBlobsRef.current.values())
      if (room <= 0) return
      const controller = new AbortController()
      aheadRef.current = { src, controller }
      void whenPlaybackStarted(controller.signal).then(() => (
        controller.signal.aborted
          ? null
          // Measured like the next track's download: once the tracks ahead
          // are held, this is the only download a track change still starts.
          : prefetchTrackBlob(src, controller.signal, Math.min(MAX_TRACK_PREFETCH_BYTES, room), (sample) => {
            upcomingDownloadRef.current({ ...sample, nextTrackId: next.id, quality: nextTrackBitrate })
          })
      )).then((blobUrl) => {
        if (aheadRef.current?.controller !== controller) {
          if (blobUrl) URL.revokeObjectURL(blobUrl)
          return
        }
        aheadRef.current = null
        // Too large for what is left of the budget, or the request failed:
        // nothing further ahead this time round.
        if (!blobUrl) {
          stopped = true
          return
        }
        // Tells `step` (this run's or a newer one's) to fetch the one after.
        rememberTrackBlob(src, blobUrl)
      })
    }
    const off = onTrackBlobRemembered(step)
    step()
    return () => {
      active = false
      off()
    }
  }, [current, isActiveDevice, isVideo, queue, rememberTrackBlob, snapshot?.queue_index, nextTrackBitrate])

  useEffect(() => () => {
    aheadRef.current?.controller.abort()
    aheadRef.current = null
  }, [])
}
