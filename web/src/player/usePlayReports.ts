// The refs and state setters passed in are stable, which this rule cannot
// see through the argument object.
/* eslint-disable react-hooks/exhaustive-deps */
import { useCallback, useEffect, useRef, type RefObject } from 'react'
import { api, ApiError } from '../api'
import type { MediaItem } from '../types'
import { anotherDeviceIsActive } from './outputState'
import {
  LISTEN_MAX_STEP_SECONDS,
  LISTEN_OUTCOME_MIN_SECONDS,
  LISTEN_START_HINT_MAX_AGE_MS,
  listenStartHint,
  type ListenEndReason,
  type ListenSourceKind,
  type ListenStartHint,
  type ListenStartKind,
  PENDING_LISTENS_MAX,
  PENDING_PLAY_MAX_AGE_MS,
  PENDING_PLAY_RETRY_BASE_MS,
  PENDING_PLAY_RETRY_MAX_MS,
  PENDING_PLAYS_MAX,
  type PendingListen,
  type PendingPlay,
  playSourceKindOfQueue,
  playThresholdSeconds,
  readPendingListens,
  readPendingPlays,
  SHORT_TRACK_SECONDS,
  writePendingListens,
  writePendingPlays,
} from './playReports'

export interface UsePlayReportsArgs {
  connected: boolean
  current: MediaItem | null
  outputsLocally: () => boolean
  elementQueueItemId: (element: HTMLMediaElement) => number | null
  videoRef: RefObject<HTMLVideoElement | null>
  bgAudioRef: RefObject<HTMLAudioElement | null>
  activeAudioRef: RefObject<HTMLAudioElement | null>
  videoBackgroundedRef: RefObject<boolean>
  elementItemsRef: RefObject<WeakMap<HTMLMediaElement, MediaItem>>
  /** Whether playback is meant to be playing (the user's intent first). */
  playing: boolean
  /** The item the server's last state names, and the device it plays on. */
  serverMediaId: number | null
  activeDeviceId: number | null
  /** This page's device id as last registered (kept while the socket is down). */
  lastRegisteredDeviceIdRef: RefObject<number | null>
  /** What started the next listen (see ListenStartHint); the commands set it. */
  listenStartRef: RefObject<ListenStartHint | null>
}

// One listen on this device: the play report
// and the listen outcome both come from it.
interface Listen {
  mediaId: number
  queueItemId: number | null
  duration: number | null
  startedAt: string | null
  listened: number
  lastTime: number
  ended: boolean
  reported: boolean
  /** When this listen was set up: the start of one that never played. */
  createdAt: string
  /** Paused (by the user, the OS, a remote) while it was the server's item. */
  paused: boolean
  /** Could not be played: its outcome is `error`, whatever ends it. */
  failed: boolean
  /** Its outcome has been decided (reported, or below the floor). */
  closed: boolean
  startKind: ListenStartKind | null
  sourceKind: ListenSourceKind | null
}

/** Listening on this device and the reports it produces:
 * counts real listening on the outputting element, queues a play report
 * once a play counts and a listen outcome once the listen is left, and
 * sends both queues (with retries) to the server. */
export function usePlayReports({
  connected,
  current,
  outputsLocally,
  elementQueueItemId,
  videoRef,
  bgAudioRef,
  activeAudioRef,
  videoBackgroundedRef,
  elementItemsRef,
  playing,
  serverMediaId,
  activeDeviceId,
  lastRegisteredDeviceIdRef,
  listenStartRef,
}: UsePlayReportsArgs) {
  // The play being listened to on this device. One at a time:
  // it follows the queue item the outputting element plays, so a quality
  // switch or a media-error reload (same element, same item) continues it,
  // while a gapless hand-off, a hidden-page advance or a skip (another item)
  // starts the next one. `queueItemId` is null while it cannot be told (see
  // queueItemIdFor); the media id and the moment the play began then stand
  // for it, and the id is filled in once known.
  const listenRef = useRef<Listen | null>(null)
  // Reports not yet confirmed by the server (null: not read from storage yet).
  const pendingPlaysRef = useRef<PendingPlay[] | null>(null)
  const pendingListensRef = useRef<PendingListen[] | null>(null)
  const pendingPlaysTimerRef = useRef<number | undefined>(undefined)
  const pendingPlaysFlushingRef = useRef(false)
  // The latest props, for the event handlers below.
  const playingRef = useRef(playing)
  playingRef.current = playing
  const currentIdRef = useRef<number | null>(current?.id ?? null)
  currentIdRef.current = current?.id ?? null
  // When the list the listens now come from was started by a user pick on
  // this device (null: not known), for their play source.
  const queueStartedAtRef = useRef<number | null>(null)

  // Sends every due report, oldest first, plays before listen outcomes. One
  // the server could not take (offline, a timeout, 5xx) backs off and ends
  // the pass - the rest would fail the same way; one it refused (an item
  // gone, a start out of range) is dropped. The server ignores a repeat of
  // the same play or listen (started_at), so a report whose answer was lost
  // is safe to send again.
  const flushPendingPlays = useCallback(async () => {
    window.clearTimeout(pendingPlaysTimerRef.current)
    pendingPlaysTimerRef.current = undefined
    if (pendingPlaysFlushingRef.current) return
    pendingPlaysFlushingRef.current = true
    // False when the server could not take one: the pass ends.
    async function sendDue<T extends { started_at: string; attempts: number; next_at: number }>(
      ref: RefObject<T[] | null>,
      send: (entry: T) => Promise<unknown>,
    ): Promise<boolean> {
      const drop = (entry: T) => {
        ref.current = (ref.current ?? []).filter((e) => e !== entry)
      }
      for (const entry of [...(ref.current ?? [])]) {
        if (Date.now() - Date.parse(entry.started_at) > PENDING_PLAY_MAX_AGE_MS) {
          drop(entry)
          continue
        }
        if (entry.next_at > Date.now()) continue
        try {
          await send(entry)
          drop(entry)
        } catch (error) {
          const status = error instanceof ApiError ? error.status : 0
          if (status >= 400 && status < 500 && ![401, 408, 429].includes(status)) {
            drop(entry)
            continue
          }
          entry.attempts += 1
          entry.next_at = Date.now() + Math.min(PENDING_PLAY_RETRY_MAX_MS, PENDING_PLAY_RETRY_BASE_MS * 2 ** (entry.attempts - 1))
          return false
        }
      }
      return true
    }
    try {
      pendingPlaysRef.current ??= readPendingPlays()
      pendingListensRef.current ??= readPendingListens()
      const playsSent = await sendDue(pendingPlaysRef, (entry) => api.recordPlayed(entry.media_item_id, {
        started_at: entry.started_at,
        queue_item_id: entry.queue_item_id,
        listened_seconds: entry.listened_seconds,
      }))
      if (playsSent) {
        await sendDue(pendingListensRef, ({ media_item_id, attempts: _attempts, next_at: _nextAt, ...listen }) => (
          api.recordListened(media_item_id, listen)
        ))
      }
    } finally {
      pendingPlaysFlushingRef.current = false
      const plays = pendingPlaysRef.current ?? []
      const listens = pendingListensRef.current ?? []
      writePendingPlays(plays)
      writePendingListens(listens)
      if (plays.length > 0 || listens.length > 0) {
        const due = Math.min(...plays.map((entry) => entry.next_at), ...listens.map((entry) => entry.next_at))
        pendingPlaysTimerRef.current = window.setTimeout(() => { void flushPendingPlays() }, Math.max(0, due - Date.now()))
      }
    }
  }, [])

  // Reports from an earlier load of this tab, and the network coming back.
  // A reconnect of the playback socket flushes too (see the effect on
  // `connected` below).
  useEffect(() => {
    void flushPendingPlays()
    const onOnline = () => { void flushPendingPlays() }
    window.addEventListener('online', onOnline)
    return () => {
      window.removeEventListener('online', onOnline)
      window.clearTimeout(pendingPlaysTimerRef.current)
      pendingPlaysTimerRef.current = undefined
    }
  }, [flushPendingPlays])
  useEffect(() => {
    if (connected) void flushPendingPlays()
  }, [connected, flushPendingPlays])

  function reportPlay(play: Listen) {
    play.reported = true
    pendingPlaysRef.current ??= readPendingPlays()
    pendingPlaysRef.current = [...pendingPlaysRef.current, {
      media_item_id: play.mediaId,
      started_at: play.startedAt ?? new Date().toISOString(),
      queue_item_id: play.queueItemId,
      listened_seconds: Math.round(play.listened * 10) / 10,
      attempts: 0,
      next_at: 0,
    }].slice(-PENDING_PLAYS_MAX)
    writePendingPlays(pendingPlaysRef.current)
    void flushPendingPlays()
  }

  // How a listen that was not played to its end was left (see
  // app.listening): another device took over; nothing is current any more,
  // or it was paused - stopped; otherwise the user moved on while it played.
  function leftReason(play: Listen, nextMediaId: number | null): ListenEndReason {
    if (outputMovedElsewhere()) return 'transferred'
    if (nextMediaId == null || play.paused) return 'stopped'
    return 'skipped'
  }

  // Another device is producing the sound now. Not just "this page is not
  // the active device": that is also briefly true around a reconnect.
  function outputMovedElsewhere(): boolean {
    return !outputsLocally() && anotherDeviceIsActive(activeDeviceId, lastRegisteredDeviceIdRef.current)
  }

  // Decides the listen's outcome, once, and queues it - unless less than
  // LISTEN_OUTCOME_MIN_SECONDS was heard (scanning), which is not reported
  // (an error always is). `beacon`: the page is going away, so it is also
  // sent as a keepalive request the browser finishes after it.
  function closeListen(play: Listen, reason: ListenEndReason, beacon = false) {
    if (play.closed) return
    play.closed = true
    const endReason: ListenEndReason = play.failed && reason !== 'finished' ? 'error' : reason
    if (endReason !== 'error' && play.listened < LISTEN_OUTCOME_MIN_SECONDS) return
    const round = (value: number) => Math.round(value * 10) / 10
    const listen = {
      started_at: play.startedAt ?? play.createdAt,
      ended_at: new Date().toISOString(),
      listened_seconds: round(play.listened),
      duration_seconds: play.duration != null ? round(play.duration) : null,
      position_seconds: Number.isFinite(play.lastTime) && play.lastTime >= 0 ? round(play.lastTime) : null,
      end_reason: endReason,
      start_kind: play.startKind,
      source_kind: play.sourceKind,
    }
    pendingListensRef.current ??= readPendingListens()
    pendingListensRef.current = [...pendingListensRef.current, {
      ...listen,
      media_item_id: play.mediaId,
      attempts: 0,
      next_at: 0,
    }].slice(-PENDING_LISTENS_MAX)
    writePendingListens(pendingListensRef.current)
    // Kept in the queue as well: a reload of this tab sends it again, and
    // the server takes it once.
    if (beacon) api.sendListenedOnUnload(play.mediaId, listen)
    else void flushPendingPlays()
  }

  // A new listen of `item`, started by what the last command said if it is
  // recent and about this item; one with no such hint was started by
  // something this page did not see (a remote control, a resume), so its
  // play source is not known either.
  function startListen(item: MediaItem, queueItemId: number | null, time: number, element: HTMLMediaElement | null): Listen {
    const hint = listenStartRef.current
    let startKind: ListenStartKind | null = null
    if (hint && Date.now() - hint.at <= LISTEN_START_HINT_MAX_AGE_MS && (hint.mediaId == null || hint.mediaId === item.id)) {
      listenStartRef.current = null
      startKind = hint.kind
      if (hint.newQueue) queueStartedAtRef.current = hint.at
    } else {
      queueStartedAtRef.current = null
    }
    const elementDuration = element && Number.isFinite(element.duration) && element.duration > 0 ? element.duration : null
    return {
      mediaId: item.id,
      queueItemId,
      duration: item.duration_seconds ?? elementDuration,
      startedAt: null,
      listened: 0,
      lastTime: time,
      ended: false,
      reported: false,
      createdAt: new Date().toISOString(),
      paused: !playingRef.current,
      failed: false,
      closed: false,
      startKind,
      sourceKind: playSourceKindOfQueue(queueStartedAtRef.current),
    }
  }

  // A `timeupdate` on a media element. Counts real listening on the element
  // producing the sound, on this device only (a remote never reports): the
  // forward step since the last one, if playing and no more than
  // LISTEN_MAX_STEP_SECONDS - so a seek, the jump back to a position after a
  // reload, or a stall adds nothing, and nothing is counted twice. Plain
  // videos (kind `video`) are still recorded when loaded (the sync effect);
  // tracks and music videos follow the listening rule.
  function noteListening(element: HTMLMediaElement) {
    if (!outputsLocally()) return
    const isVideoElement = element === videoRef.current || element === bgAudioRef.current
    const outputting = isVideoElement
      ? (videoBackgroundedRef.current ? bgAudioRef.current : videoRef.current)
      : activeAudioRef.current
    if (element !== outputting) return
    const item = elementItemsRef.current.get(element) ?? (isVideoElement ? current : null)
    if (!item || item.kind === 'video') return
    const time = element.currentTime
    if (!Number.isFinite(time)) return
    const queueItemId = elementQueueItemId(element)
    const play = listenRef.current
    // A listen that was left (not ended) is over: the item playing again
    // is a new listen.
    const sameItem = play != null && play.mediaId === item.id && !(play.closed && !play.ended) &&
      (play.queueItemId == null || queueItemId == null || play.queueItemId === queueItemId)
    // A new play: another item, or this one starting over after it ended
    // (repeat-one; the element is put back to 0 at `ended`).
    if (!play || !sameItem || (play.ended && time < play.lastTime)) {
      if (play && !play.closed) closeListen(play, leftReason(play, item.id))
      listenRef.current = startListen(item, queueItemId, time, element)
      return
    }
    play.queueItemId ??= queueItemId
    const step = time - play.lastTime
    play.lastTime = time
    if (element.paused || element.seeking || step <= 0 || step > LISTEN_MAX_STEP_SECONDS) return
    // When the listening began - the scrobble's timestamp.
    play.startedAt ??= new Date(Date.now() - step * 1000).toISOString()
    play.listened += step
    play.paused = false
    if (!play.reported && play.listened >= playThresholdSeconds(play.duration)) reportPlay(play)
  }

  // The outputting element reached its end (called before the local advance,
  // which puts a repeat-one track back to 0). A track of 30 seconds or less
  // played to its end counts even with less than half of it heard. The
  // listen is finished, and whatever plays next was started by the queue.
  function noteListenEnded(element: HTMLMediaElement) {
    const play = listenRef.current
    const item = elementItemsRef.current.get(element) ?? current
    if (!play || !item || play.mediaId !== item.id || !outputsLocally()) return
    play.ended = true
    if (!play.reported && play.listened > 0 && play.duration != null && play.duration <= SHORT_TRACK_SECONDS) {
      reportPlay(play)
    }
    if (Number.isFinite(element.currentTime)) play.lastTime = element.currentTime
    closeListen(play, 'finished')
    listenStartRef.current = listenStartHint('auto')
  }

  // The current item could not be played (out of retries, or a file that
  // cannot be): its listen ends as an `error` however it is left, so it
  // never counts against the track. One that had not started yet is set up
  // here.
  function noteListenFailed() {
    if (!current || current.kind === 'video' || !outputsLocally()) return
    const play = listenRef.current
    if (play && !play.closed && play.mediaId === current.id) {
      play.failed = true
      return
    }
    if (play && !play.closed) closeListen(play, leftReason(play, current.id))
    listenRef.current = { ...startListen(current, null, 0, null), failed: true }
  }

  // The listen is left without a timeupdate of the next item to say so: the
  // current item changed (Next, a pick, a cleared queue), or another device
  // took over. The paused state is followed only while the server still
  // names this listen's item, so a new list starting (which the server
  // answers with "playing") does not make a paused listen look played on.
  useEffect(() => {
    const play = listenRef.current
    if (!play || play.closed) return
    if (serverMediaId === play.mediaId) play.paused = !playing
    if (outputMovedElsewhere()) closeListen(play, 'transferred')
    else if (current?.id !== play.mediaId && outputsLocally()) closeListen(play, leftReason(play, current?.id ?? null))
  }, [current?.id, playing, serverMediaId, activeDeviceId])

  // The page going away (closed, reloaded, navigated): how the listen would
  // have ended cannot be told.
  useEffect(() => {
    function onPageHide() {
      const play = listenRef.current
      if (!play || play.closed || !outputsLocally()) return
      closeListen(play, 'unknown', true)
    }
    window.addEventListener('pagehide', onPageHide)
    return () => window.removeEventListener('pagehide', onPageHide)
  }, [])

  return { noteListening, noteListenEnded, noteListenFailed }
}
