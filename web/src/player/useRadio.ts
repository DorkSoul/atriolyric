// The refs and state setters passed in are stable, which this rule cannot
// see through the argument object.
/* eslint-disable react-hooks/exhaustive-deps */
import { useCallback, useEffect, useRef, type RefObject, type Dispatch, type SetStateAction } from 'react'
import { api } from '../api'
import type { useToast } from '../ToastContext'
import type { RadioStation } from '../types'
import { logPlaybackDiagnostic } from './diagnostics'
import {
  MEDIA_ERR_ABORTED,
  radioStreamStatus,
  MEDIA_RETRY_DELAYS_MS,
  RADIO_STALL_LIMIT_MS,
} from './mediaRecovery'
import { writeRadioResumeStation } from './storage'

export interface UseRadioControlsArgs {
  radioAudioRef: RefObject<HTMLAudioElement | null>
  radioRetryRef: RefObject<{ attempts: number; timer: number | undefined; stallTimer: number | undefined; playingSince: number | null; lastPosition: number; wanted: boolean }>
  radioResumeRequestedRef: RefObject<boolean>
  radioStation: RadioStation | null
  setRadioStation: Dispatch<SetStateAction<RadioStation | null>>
  setIsRadioPlaying: Dispatch<SetStateAction<boolean>>
  setRadioNowPlaying: Dispatch<SetStateAction<string | null>>
  showToast: ReturnType<typeof useToast>['showToast']
}

/** Radio commands and the radio element's failure handling (reconnects,
 * stall detection, giving up with a message). No effects. */
export function useRadioControls({
  radioAudioRef,
  radioRetryRef,
  radioResumeRequestedRef,
  radioStation,
  setRadioStation,
  setIsRadioPlaying,
  setRadioNowPlaying,
  showToast,
}: UseRadioControlsArgs) {
  const radioReloadCountRef = useRef(0)
  const stopRadio = useCallback(() => {
    const radioRetry = radioRetryRef.current
    window.clearTimeout(radioRetry.timer)
    window.clearTimeout(radioRetry.stallTimer)
    radioRetryRef.current = { attempts: 0, timer: undefined, stallTimer: undefined, playingSince: null, lastPosition: 0, wanted: false }
    const audio = radioAudioRef.current
    if (audio) {
      audio.pause()
      audio.removeAttribute('src')
      audio.load()
    }
    setRadioStation(null)
    setIsRadioPlaying(false)
    setRadioNowPlaying(null)
    radioResumeRequestedRef.current = false
    writeRadioResumeStation(null)
  }, [])

  // Radio: a station's stream fails, ends or stalls far more often than a
  // file. Reconnect it a few times (a fresh URL, so no cache or half-dead
  // connection is reused) before giving up with a message.
  const reloadRadio = useCallback((station: RadioStation) => {
    const audio = radioAudioRef.current
    if (!audio) return
    window.clearTimeout(radioRetryRef.current.stallTimer)
    radioRetryRef.current.stallTimer = undefined
    radioRetryRef.current.lastPosition = 0
    radioReloadCountRef.current += 1
    audio.src = `${api.stationStreamUrl(station.id)}?reconnect=${Date.now().toString(36)}-${radioReloadCountRef.current}`
    audio.load()
    audio.play().catch(() => {})
  }, [])

  const toggleRadioPlayPause = useCallback(() => {
    const audio = radioAudioRef.current
    if (!audio) return
    if (audio.paused) {
      radioResumeRequestedRef.current = true
      const retry = radioRetryRef.current
      // After a failure the element is dead (errored, ended, or given up
      // on): play() would do nothing, so reconnect.
      const failed = audio.error != null || audio.ended || retry.attempts > 0
      window.clearTimeout(retry.timer)
      radioRetryRef.current = { ...retry, attempts: 0, timer: undefined, wanted: true }
      if (failed && radioStation) reloadRadio(radioStation)
      else audio.play().catch(() => {})
      setIsRadioPlaying(true)
    } else {
      audio.pause()
      radioRetryRef.current.wanted = false
      window.clearTimeout(radioRetryRef.current.timer)
      radioRetryRef.current.timer = undefined
      radioResumeRequestedRef.current = false
      setIsRadioPlaying(false)
    }
  }, [radioStation, reloadRadio])

  function handleRadioFailure(cause: 'error' | 'ended' | 'stalled') {
    const audio = radioAudioRef.current
    const retry = radioRetryRef.current
    window.clearTimeout(retry.stallTimer)
    retry.stallTimer = undefined
    retry.playingSince = null
    if (!radioStation || !audio || !retry.wanted) return
    if (cause === 'error' && audio.error?.code === MEDIA_ERR_ABORTED) return
    setIsRadioPlaying(false)
    if (retry.timer != null) return
    const station = radioStation
    // The element cannot say why it failed: one same-origin request for the
    // status tells "too many streams" (429, retrying cannot help) from a
    // station that is down (404, 502). Only after an error: an ended or
    // stalled stream did load.
    const status = cause === 'error' ? radioStreamStatus(api.stationStreamUrl(station.id)) : Promise.resolve(null)
    function giveUp(httpStatus: number | null) {
      retry.wanted = false
      window.clearTimeout(retry.timer)
      retry.timer = undefined
      audio!.pause()
      setIsRadioPlaying(false)
      radioResumeRequestedRef.current = false
      writeRadioResumeStation(null)
      logPlaybackDiagnostic('radio_given_up', { cause, status: httpStatus, attempt: retry.attempts })
      showToast(httpStatus === 429
        ? `Too many radio streams are playing right now, so "${station.name}" can't start. Stop another stream or try again later.`
        : `Couldn't play "${station.name}" - the station may be offline.`, 'error')
    }
    if (retry.attempts < MEDIA_RETRY_DELAYS_MS.length) {
      // A stall has already been waited out (RADIO_STALL_LIMIT_MS).
      const delay = cause === 'stalled' ? 0 : MEDIA_RETRY_DELAYS_MS[retry.attempts]
      retry.attempts += 1
      const timer = window.setTimeout(() => {
        radioRetryRef.current.timer = undefined
        if (radioRetryRef.current.wanted) reloadRadio(station)
      }, delay)
      retry.timer = timer
      logPlaybackDiagnostic('radio_retry_scheduled', { cause, attempt: retry.attempts, delay_ms: delay })
      // The retry waits for nobody; a 429 that arrives before it runs
      // cancels it. (One that arrives later is found again by the retry's
      // own failure.)
      void status.then((httpStatus) => {
        if (httpStatus === 429 && radioRetryRef.current === retry && retry.wanted && retry.timer === timer) giveUp(429)
      })
      return
    }
    retry.wanted = false
    audio.pause()
    void status.then((httpStatus) => {
      if (radioRetryRef.current === retry) giveUp(httpStatus)
    })
  }

  // Waiting for data now and then is normal; ten seconds of it is a dead
  // stream that will not recover by itself.
  function noteRadioWaiting() {
    const retry = radioRetryRef.current
    if (retry.stallTimer != null || !retry.wanted) return
    retry.stallTimer = window.setTimeout(() => {
      radioRetryRef.current.stallTimer = undefined
      handleRadioFailure('stalled')
    }, RADIO_STALL_LIMIT_MS)
  }

  function noteRadioProgress(playing: boolean) {
    const retry = radioRetryRef.current
    const audio = radioAudioRef.current
    // A timeupdate only counts when the stream actually moved on.
    const position = audio && Number.isFinite(audio.currentTime) ? audio.currentTime : 0
    if (!playing && position <= retry.lastPosition) return
    retry.lastPosition = position
    window.clearTimeout(retry.stallTimer)
    retry.stallTimer = undefined
    if (playing) {
      retry.playingSince ??= Date.now()
      setIsRadioPlaying(true)
    }
    // Ten seconds of real playback: the next failure starts a fresh budget.
    if (retry.playingSince != null && Date.now() - retry.playingSince > RADIO_STALL_LIMIT_MS) retry.attempts = 0
  }

  return { stopRadio, reloadRadio, toggleRadioPlayPause, handleRadioFailure, noteRadioWaiting, noteRadioProgress }
}

export interface UseRadioStreamArgs {
  radioAudioRef: RefObject<HTMLAudioElement | null>
  radioStation: RadioStation | null
  radioRetryRef: RefObject<{ attempts: number; timer: number | undefined; stallTimer: number | undefined; playingSince: number | null; lastPosition: number; wanted: boolean }>
  radioResumeRequestedRef: RefObject<boolean>
  setIsRadioPlaying: Dispatch<SetStateAction<boolean>>
  setRadioNowPlaying: Dispatch<SetStateAction<string | null>>
  showToast: ReturnType<typeof useToast>['showToast']
}

/** Loads and plays a newly selected station, and polls what it is playing. */
export function useRadioStream({
  radioAudioRef,
  radioStation,
  radioRetryRef,
  radioResumeRequestedRef,
  setIsRadioPlaying,
  setRadioNowPlaying,
  showToast,
}: UseRadioStreamArgs) {
  // Radio isn't part of the multi-device sync protocol - it's purely local
  // to whichever device started it, so loading/playing a newly-selected
  // station just drives the dedicated radio <audio> element directly.
  useEffect(() => {
    const audio = radioAudioRef.current
    if (!audio || !radioStation) return
    const retry = radioRetryRef.current
    window.clearTimeout(retry.timer)
    window.clearTimeout(retry.stallTimer)
    radioRetryRef.current = { attempts: 0, timer: undefined, stallTimer: undefined, playingSince: null, lastPosition: 0, wanted: true }
    audio.src = api.stationStreamUrl(radioStation.id)
    audio.load()
    audio.play().then(() => {
      radioResumeRequestedRef.current = false
    }).catch((error: unknown) => {
      setIsRadioPlaying(false)
      // A stream that failed to load also fires `error`: handleRadioFailure
      // retries it and finds out why before it says anything.
      if (audio.error != null || (error instanceof DOMException && error.name === 'NotSupportedError')) return
      radioResumeRequestedRef.current = false
      writeRadioResumeStation(null)
      showToast(`Couldn't play "${radioStation.name}" - the station may be offline.`, 'error')
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [radioStation?.id])

  useEffect(() => {
    if (!radioStation) return
    let cancelled = false
    function poll() {
      api.getNowPlaying(radioStation!.id)
        .then((r) => {
          if (!cancelled) setRadioNowPlaying(r.title)
        })
        .catch(() => {
          if (!cancelled) setRadioNowPlaying(null)
        })
    }
    // Only while the page is visible: nobody reads the line in a hidden tab
    // (or on a locked phone). Coming back asks at once, then every 15 s again.
    let id: number | undefined
    function start() {
      if (id !== undefined) return
      poll()
      id = window.setInterval(poll, 15000)
    }
    function stop() {
      window.clearInterval(id)
      id = undefined
    }
    function onVisibility() {
      if (document.visibilityState === 'visible') start()
      else stop()
    }
    if (document.visibilityState === 'hidden') poll()
    else start()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      cancelled = true
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [radioStation])
}
