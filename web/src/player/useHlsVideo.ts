// The refs and state setters passed in are stable, which this rule cannot
// see through the argument object.
/* eslint-disable react-hooks/exhaustive-deps */
import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'
import type Hls from 'hls.js'
import { api } from '../api'
import type { AppSettings } from '../settings'
import type { useToast } from '../ToastContext'
import type { MediaItem, PlaybackStateSnapshot } from '../types'
import { logPlaybackDiagnostic } from './diagnostics'
import { SLOW_SEGMENT_LOAD_POLICY } from './mediaRecovery'
import { BACKGROUND_VIDEO_AUDIO } from './mediaSession'
import { isNativeApp } from '../native/bridge'
import { keepsBackgroundVideoAudio, type OutputState } from './outputState'

export interface UseHlsVideoArgs {
  wsRef: RefObject<WebSocket | null>
  elementItemsRef: RefObject<WeakMap<HTMLMediaElement, MediaItem>>
  current: MediaItem | null
  showToast: ReturnType<typeof useToast>['showToast']
  videoLoadRef: RefObject<number>
  hlsRef: RefObject<Hls | null>
  playingRef: RefObject<boolean>
  videoBackgroundedRef: RefObject<boolean>
}

/** The buffered range the playhead is in (or just before), if any. */
function playingRange(element: HTMLMediaElement): { start: number; end: number } | null {
  const { buffered, currentTime } = element
  for (let i = 0; i < buffered.length; i++) {
    if (currentTime >= buffered.start(i) - 0.5 && currentTime <= buffered.end(i)) {
      return { start: buffered.start(i), end: buffered.end(i) }
    }
  }
  return null
}

/** Loading the video element's source (direct, hls.js or native HLS), the
 * hls.js error recovery, and media error reports to the server. */
export function useHlsVideo({
  wsRef,
  elementItemsRef,
  current,
  showToast,
  videoLoadRef,
  hlsRef,
  playingRef,
  videoBackgroundedRef,
}: UseHlsVideoArgs) {
  // Playback failures go to the container logs as well as the console, so a
  // mobile reproduction can be traced without developer tools.
  const reportMediaError = useCallback((details: Record<string, unknown>) => {
    logPlaybackDiagnostic('media_error_report', details)
    const ws = wsRef.current
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'client_diagnostic', event: 'media_error', ...details }))
    }
  }, [])

  const hlsErrorHandler = useCallback((
    source: string,
    element: HTMLMediaElement,
    hls: Hls,
    onFatal: () => void,
    events: typeof Hls.Events,
  ) => {
    let recoveries = 0
    // The count resets, so the third fatal error of a long video is not final
    // when the earlier ones have recovered. It starts again once a fragment
    // loads after playback has moved on from where the
    // last recovery happened - not merely after a fragment loads, or a
    // fragment that loads and then fails to decode would recover forever.
    let recoveredAt: number | null = null
    let restartTimer: number | undefined
    hls.on(events.FRAG_LOADED, () => {
      if (recoveredAt != null && Number.isFinite(element.currentTime) && element.currentTime > recoveredAt + 1) {
        recoveries = 0
        recoveredAt = null
      }
    })
    // The stream turned out to end before the playlist said (see below):
    // where the media runs out, and how to stop watching for it.
    let endsAt: number | null = null
    let stopWatchingEnd = () => {}
    // A reload still waiting (see below) must not touch a destroyed instance.
    hls.on(events.DESTROYING, () => {
      window.clearTimeout(restartTimer)
      stopWatchingEnd()
    })
    /** The server answers 404 for a segment past the real end of the video:
     * its playlist is built from the duration the file's header claims, and a
     * cut-off file (an interrupted download) has less. hls.js would retry it,
     * fail, and the video would sit frozen at the last frame under a
     * "Couldn't play" message. Instead, stop loading, play what is there, and
     * end the item where the media ends, as if the playlist had ended there. */
    const endEarly = (from: number) => {
      hls.stopLoad()
      if (endsAt != null) {
        endsAt = Math.min(endsAt, from)
        return
      }
      endsAt = from
      const check = () => {
        if (endsAt == null) return
        if (element.seeking && element.currentTime < endsAt - 1) {
          // Back into the video: load again from there (the end is found
          // again, the same way, when loading reaches it).
          hls.startLoad(element.currentTime)
          return
        }
        // At the end: playing up to it, or stalled just short of it (the
        // last frame can end a little before the last sound).
        const stalled = !element.paused && element.readyState < 3
        if (element.currentTime >= endsAt - (stalled ? 1.5 : 0.3)) {
          stopWatchingEnd()
          element.pause()
          element.dispatchEvent(new Event('ended'))
        }
      }
      const media = ['timeupdate', 'waiting', 'seeking'] as const
      for (const type of media) element.addEventListener(type, check)
      stopWatchingEnd = () => {
        endsAt = null
        for (const type of media) element.removeEventListener(type, check)
      }
      check()
    }
    return (_event: unknown, data: {
      type: string
      details: string
      fatal: boolean
      response?: { code?: number }
      frag?: { sn?: number | string; start?: number; duration?: number }
    }) => {
      const buffered = element.buffered
      const recoverable = data.fatal && recoveries < 3 && (data.type === 'networkError' || data.type === 'mediaError')
      reportMediaError({
        source,
        media_item_id: elementItemsRef.current.get(element)?.id ?? current?.id ?? null,
        hls_type: data.type,
        hls_details: data.details,
        fatal: data.fatal,
        recovery: data.fatal ? (recoverable ? `${data.type}_retry_${recoveries + 1}` : 'none') : 'n/a',
        http_status: data.response?.code ?? null,
        segment: typeof data.frag?.sn === 'number' ? data.frag.sn : null,
        current_time: Number.isFinite(element.currentTime) ? Number(element.currentTime.toFixed(3)) : null,
        buffered_end: buffered.length ? Number(buffered.end(buffered.length - 1).toFixed(3)) : null,
        ready_state: element.readyState,
        network_state: element.networkState,
      })
      // A segment the server says does not exist, right after the buffered
      // stretch being played (and not the first one: then there is nothing
      // to play, which stays an error): the video ends where that stretch
      // does. Checked before hls.js gives up, so its retries stop at the first
      // answer. The stretch can end up to a segment early: the last segment
      // that exists holds whatever was left of the file.
      const fragStart = data.frag?.start
      const playing = playingRange(element)
      if (
        data.details === 'fragLoadError' && data.response?.code === 404
        && typeof fragStart === 'number' && fragStart > 0 && playing != null
        && fragStart >= playing.end - 0.5 && fragStart - playing.end <= (data.frag?.duration ?? 0) + 0.5
      ) {
        endEarly(playing.end)
        return
      }
      if (!data.fatal) return
      // The server's transcode cap: retrying only adds load, so say why once.
      if (data.response?.code === 503) {
        showToast('Too many videos are being converted right now - try again in a moment', 'error', 'hls-busy')
        return
      }
      // hls.js documents these two as recoverable in place; only an
      // unrecoverable (or repeatedly failing) error should reach the user.
      if (recoverable) {
        recoveries += 1
        recoveredAt = Number.isFinite(element.currentTime) ? element.currentTime : 0
        // Reloading the instant the network failed mostly fails again (the
        // connection is still down, or the encoder is still busy): wait a moment.
        if (data.type === 'networkError') {
          window.clearTimeout(restartTimer)
          restartTimer = window.setTimeout(() => hls.startLoad(), 1000)
        } else hls.recoverMediaError()
        return
      }
      onFatal()
    }
  }, [current?.id, reportMediaError, showToast])

  const loadVideoSource = useCallback(async (element: HTMLVideoElement, streamUrl: string) => {
    const token = ++videoLoadRef.current
    hlsRef.current?.destroy()
    hlsRef.current = null
    element.removeAttribute('src')
    const play = () => {
      if (playingRef.current && !videoBackgroundedRef.current) element.play().catch(() => {})
    }
    const fail = () => element.dispatchEvent(new Event('error'))
    let info: { mode: 'direct' | 'hls'; url: string } = { mode: 'direct', url: streamUrl }
    const mediaItemId = Number(/\/api\/stream\/(\d+)/.exec(streamUrl)?.[1])
    if (Number.isFinite(mediaItemId)) {
      try {
        info = await api.playbackInfo(mediaItemId)
      } catch {
        // No answer: try the file directly.
      }
    }
    if (token !== videoLoadRef.current) return
    const startPosition = element.currentTime
    if (info.mode === 'hls') {
      try {
        const { default: HlsLib } = await import('hls.js')
        if (token !== videoLoadRef.current) return
        if (HlsLib.isSupported()) {
          const hls = new HlsLib({
            startPosition: startPosition > 0 ? startPosition : -1,
            fragLoadPolicy: SLOW_SEGMENT_LOAD_POLICY,
            // The Capacitor app talks to the server cross-origin.
            xhrSetup: (xhr) => {
              xhr.withCredentials = true
            },
          })
          hlsRef.current = hls
          hls.on(HlsLib.Events.ERROR, hlsErrorHandler('video_hls', element, hls, fail, HlsLib.Events))
          hls.on(HlsLib.Events.MANIFEST_PARSED, play)
          hls.loadSource(info.url)
          hls.attachMedia(element)
          return
        }
      } catch {
        // Fall through to native HLS / direct below.
      }
      if (element.canPlayType('application/vnd.apple.mpegurl')) {
        element.addEventListener('loadedmetadata', play, { once: true })
        element.src = info.url
        element.currentTime = startPosition
        return
      }
    }
    element.src = streamUrl
    element.currentTime = startPosition
    play()
  }, [hlsErrorHandler])

  useEffect(() => () => {
    videoLoadRef.current += 1
    hlsRef.current?.destroy()
    hlsRef.current = null
  }, [])

  return { reportMediaError, hlsErrorHandler, loadVideoSource }
}

export interface UseBackgroundVideoAudioArgs {
  isVideo: boolean
  current: MediaItem | null
  /** Whether this page is the output (see outputState.ts). */
  outputState: OutputState
  snapshot: PlaybackStateSnapshot | null
  settings: AppSettings
  showToast: ReturnType<typeof useToast>['showToast']
  appMediaChangeUntilRef: RefObject<number>
  videoBackgroundedRef: RefObject<boolean>
  bgAudioRef: RefObject<HTMLAudioElement | null>
  videoRef: RefObject<HTMLVideoElement | null>
  playingRef: RefObject<boolean>
  elementItemsRef: RefObject<WeakMap<HTMLMediaElement, MediaItem>>
  volumeRef: RefObject<number>
  deviceIdRef: RefObject<number | null>
  hlsErrorHandler: ReturnType<typeof useHlsVideo>['hlsErrorHandler']
}

/** While the page is hidden a playing video's sound moves to an audio-only
 * element (the audio HLS rendition), and back when the page returns. */
export function useBackgroundVideoAudio({
  isVideo,
  current,
  outputState,
  snapshot,
  settings,
  showToast,
  appMediaChangeUntilRef,
  videoBackgroundedRef,
  bgAudioRef,
  videoRef,
  playingRef,
  elementItemsRef,
  volumeRef,
  deviceIdRef,
  hlsErrorHandler,
}: UseBackgroundVideoAudioArgs) {
  const bgHlsRef = useRef<Hls | null>(null)
  const bgLoadRef = useRef(0)
  const [videoBackgrounded, setVideoBackgrounded] = useState(false)
  // Hand a playing video's sound to the audio-only element while the page is
  // hidden, and take it back (at the same position) when it returns.
  useEffect(() => {
    if (!BACKGROUND_VIDEO_AUDIO) return
    const onVisibility = () => {
      const hidden = document.visibilityState === 'hidden'
      appMediaChangeUntilRef.current = Date.now() + 1500
      if (hidden === videoBackgroundedRef.current) return
      const bg = bgAudioRef.current
      const video = videoRef.current
      if (hidden) {
        if (!isVideo || !video || !playingRef.current) return
        videoBackgroundedRef.current = true
        setVideoBackgrounded(true)
        return
      }
      videoBackgroundedRef.current = false
      setVideoBackgrounded(false)
      if (bg && video && bg.dataset.streamSrc && Number.isFinite(bg.currentTime) && bg.currentTime > 0) {
        video.currentTime = bg.currentTime
      }
      if (isVideo && playingRef.current && video) video.play().catch(() => {})
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => document.removeEventListener('visibilitychange', onVisibility)
  }, [isVideo])

  useEffect(() => {
    const bg = bgAudioRef.current
    if (!bg) return
    const teardown = () => {
      bgLoadRef.current += 1
      bgHlsRef.current?.destroy()
      bgHlsRef.current = null
      bg.pause()
      bg.removeAttribute('src')
      delete bg.dataset.streamSrc
      bg.load()
    }
    // A dropped socket is not a hand-off (see the sync effect's dropout
    // comment in PlayerContext): a locked phone loses its socket routinely,
    // and tearing the sound down there would stop a backgrounded video for
    // good. Another tab taking the device over is a hand-off.
    if (!videoBackgrounded || !isVideo || !current || !keepsBackgroundVideoAudio(outputState)) {
      if (bg.dataset.streamSrc) teardown()
      return
    }
    // Still down: whatever is playing carries on, nothing new is started.
    if (outputState !== 'active') return
    const streamUrl = api.streamUrl(current.id)
    if (bg.dataset.streamSrc === streamUrl) return
    teardown()
    bg.dataset.streamSrc = streamUrl
    const token = bgLoadRef.current
    const video = videoRef.current
    // A snapshot position is only this video's when the snapshot names it.
    const startPosition = video && elementItemsRef.current.get(video)?.id === current.id
      ? video.currentTime
      : snapshot?.media_item_id === current.id ? snapshot.position_seconds : 0
    appMediaChangeUntilRef.current = Date.now() + 1000
    video?.pause()
    bg.volume = volumeRef.current
    const play = () => {
      if (token === bgLoadRef.current && playingRef.current) bg.play().catch(() => {})
    }
    // The server's audio-only HLS rendition of the video (no picture, so far
    // less data than the video stream), seekable from any position.
    const audioUrl = `/api/stream/${current.id}/audio/index.m3u8`
      + (settings.maxBitrateKbps ? `?max_bitrate=${settings.maxBitrateKbps}` : '')
    void (async () => {
      // In the Android app the element is a native player, which plays HLS itself.
      if (isNativeApp()) {
        bg.addEventListener('loadedmetadata', play, { once: true })
        bg.src = audioUrl
        bg.currentTime = startPosition
        return
      }
      try {
        const { default: HlsLib } = await import('hls.js')
        if (token !== bgLoadRef.current) return
        if (HlsLib.isSupported()) {
          const hls = new HlsLib({
            startPosition: startPosition > 0 ? startPosition : -1,
            fragLoadPolicy: SLOW_SEGMENT_LOAD_POLICY,
            // The Capacitor app talks to the server cross-origin.
            xhrSetup: (xhr) => {
              xhr.withCredentials = true
            },
          })
          bgHlsRef.current = hls
          hls.on(HlsLib.Events.MANIFEST_PARSED, play)
          // A fatal error here is reported the way an element error is, so a
          // hidden video does not go silent while the app still says it is
          // playing.
          const failedItem = current
          hls.on(HlsLib.Events.ERROR, hlsErrorHandler('bg_audio_hls', bg, hls, () => {
            if (token !== bgLoadRef.current) return
            logPlaybackDiagnostic('bg_audio_hls_fatal', {
              device_id: deviceIdRef.current,
              media_item_id: failedItem.id,
              visibility: document.visibilityState,
            })
            showToast(`Couldn't play "${failedItem.title}" in the background - the audio stream failed.`, 'error')
          }, HlsLib.Events))
          hls.loadSource(audioUrl)
          hls.attachMedia(bg)
          return
        }
      } catch {
        // Fall through to native HLS / the whole file below.
      }
      if (bg.canPlayType('application/vnd.apple.mpegurl')) {
        bg.addEventListener('loadedmetadata', play, { once: true })
        bg.src = audioUrl
        bg.currentTime = startPosition
        return
      }
      bg.src = streamUrl
      bg.currentTime = startPosition
      play()
    })()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoBackgrounded, isVideo, current?.id, outputState])

  useEffect(() => {
    const bg = bgAudioRef.current
    if (!bg || !videoBackgrounded || !bg.dataset.streamSrc) return
    if (snapshot?.is_playing) bg.play().catch(() => {})
    else bg.pause()
  }, [videoBackgrounded, snapshot?.is_playing])

  useEffect(() => () => {
    bgLoadRef.current += 1
    bgHlsRef.current?.destroy()
    bgHlsRef.current = null
  }, [])
}
