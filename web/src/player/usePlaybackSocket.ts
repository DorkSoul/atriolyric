// The refs and state setters passed in are stable, which this rule cannot
// see through the argument object.
/* eslint-disable react-hooks/exhaustive-deps */
import {
  useCallback,
  useEffect,
  useRef,
  type RefObject,
  type Dispatch,
  type SetStateAction,
} from 'react'
import { api, ApiError, notifyUnauthorized } from '../api'
import { debugLog } from '../clientDebugLog'
import type { useAuth } from '../AuthContext'
import { applyAccentColor } from '../settings'
import type { AudioSettings } from '../audioEffects'
import type { useToast } from '../ToastContext'
import type { MediaItem, PlaybackStateSnapshot } from '../types'
import { logPlaybackDiagnostic, type PendingEndMeta, type PlaybackEndDiagnostic } from './diagnostics'
import {
  CONNECT_TIMEOUT_MS,
  guessDeviceLabel,
  HEARTBEAT_INTERVAL_MS,
  OFFLINE_COMMAND_TTL_MS,
  OFFLINE_QUEUED_COMMANDS,
  OWN_ECHO_COMMANDS,
  PENDING_END_MAX_AGE_MS,
  PENDING_END_MAX_AGE_ON_ADVANCE_MS,
  PLAY_INTENT_CONFIRM_MS,
  MAX_IDS_PER_MESSAGE,
  type PlayIntent,
  RECONNECT_BASE_DELAY_MS,
  RECONNECT_MAX_DELAY_MS,
  RECONNECTING_NOTICE_DELAY_MS,
  SEEK_ECHO_WINDOW_MS,
  SOCKET_SILENCE_LIMIT_MS,
} from './socketPolicy'
import { playbackDeviceToken, readPlaybackResumeFlag } from './storage'
import { isNativeApp } from '../native/bridge'
import { checkOffline, isOffline, noteServerAnswered, onOfflineChange } from '../native/offline'
import { clearHandback, handback, LOCAL_DEVICE_ID, LocalPlaybackSocket, noteServerState } from '../native/localPlayback'

export interface UsePlaybackSocketArgs {
  showToast: ReturnType<typeof useToast>['showToast']
  setUser: ReturnType<typeof useAuth>['setUser']
  userRef: RefObject<ReturnType<typeof useAuth>['user']>
  wsRef: RefObject<WebSocket | null>
  deviceIdRef: RefObject<number | null>
  connectionGenerationRef: RefObject<number>
  playbackSocketRegisteredRef: RefObject<boolean>
  lastRegisteredDeviceIdRef: RefObject<number | null>
  pendingTrackEndedRef: RefObject<PlaybackEndDiagnostic | null>
  pendingEndMetaRef: RefObject<PendingEndMeta>
  lastServerStateRef: RefObject<PlaybackStateSnapshot | null>
  playIntentRef: RefObject<PlayIntent | null>
  setPlayIntentState: Dispatch<SetStateAction<PlayIntent | null>>
  activeAudioRef: RefObject<HTMLAudioElement | null>
  videoRef: RefObject<HTMLVideoElement | null>
  bgAudioRef: RefObject<HTMLAudioElement | null>
  elementItemsRef: RefObject<WeakMap<HTMLMediaElement, MediaItem>>
  connected: boolean
  setConnected: Dispatch<SetStateAction<boolean>>
  /** Closed with 4000: another tab of this browser has the device now. Owned
   * by the provider, because it also decides whether this page may go on
   * producing sound (see `outputsLocally` and the sync effect). */
  socketReplaced: boolean
  setSocketReplaced: (replaced: boolean) => void
  setReconnecting: Dispatch<SetStateAction<boolean>>
  forceDeviceIdTick: Dispatch<SetStateAction<number>>
  setSnapshot: Dispatch<SetStateAction<PlaybackStateSnapshot | null>>
  setCurrent: Dispatch<SetStateAction<MediaItem | null>>
  setPositionCommand: Dispatch<SetStateAction<{ seconds: number; nonce: number; trackId: number | null } | null>>
  resolveIds: (ids: number[]) => Promise<MediaItem[]>
  /** Looks up a snapshot's queue (the rows around `index` first) and applies
   * it while `wanted` still says so; a failed lookup is retried by the next. */
  resolveQueue: (ids: number[], index: number, wanted: () => boolean) => void
  queueItemIdFor: (mediaId: number | null | undefined) => number | null
  trackEndedMessage: () => Record<string, unknown>
  applyRemoteAudioSettings: (raw: Partial<AudioSettings>) => void
}

/** The playback WebSocket: connection, registration, reconnect and
 * heartbeat, the snapshots it delivers, and `send` with its offline hold
 * queue and play intents. Also the "Reconnecting..." notice. */
export function usePlaybackSocket({
  showToast,
  setUser,
  userRef,
  wsRef,
  deviceIdRef,
  connectionGenerationRef,
  playbackSocketRegisteredRef,
  lastRegisteredDeviceIdRef,
  pendingTrackEndedRef,
  pendingEndMetaRef,
  lastServerStateRef,
  playIntentRef,
  setPlayIntentState,
  activeAudioRef,
  videoRef,
  bgAudioRef,
  elementItemsRef,
  connected,
  setConnected,
  socketReplaced,
  setSocketReplaced,
  setReconnecting,
  forceDeviceIdTick,
  setSnapshot,
  setCurrent,
  setPositionCommand,
  resolveIds,
  resolveQueue,
  queueItemIdFor,
  trackEndedMessage,
  applyRemoteAudioSettings,
}: UsePlaybackSocketArgs) {
  const snapshotGenerationRef = useRef(0)
  // The seeks this device itself sent recently (periodic position saves and
  // user seeks). The server broadcasts every seek to every device including
  // the sender, so these tell an echo of our own position from another
  // device's seek. Every recent one is kept, not just the newest: on a slow
  // link echoes arrive several saves late, and an older echo mistaken for a
  // remote seek would jump playback back by that many seconds.
  const recentSeeksSentRef = useRef<{ seconds: number; at: number }[]>([])
  // While set in the future, a broadcast position is our own resync echo, not another device's seek.
  const resyncUntilRef = useRef(0)
  // User commands given while the socket was down (see OFFLINE_QUEUED_COMMANDS)
  // and the timer that drops them; flushed after the next registration.
  const offlineQueueRef = useRef<Array<{ msg: Record<string, unknown>; at: number }>>([])
  const offlineQueueTimerRef = useRef<number | undefined>(undefined)
  // Registered, but the queue above has not been flushed yet: it goes out on
  // the first snapshot, after any pending track_ended replay, so the server
  // processes what happened in the order it happened (the track ended, then
  // the user pressed Next) and the flush can see which device is active.
  // Commands given meanwhile join the queue instead of overtaking it.
  const offlineFlushPendingRef = useRef(false)
  const playIntentTimerRef = useRef<number | undefined>(undefined)
  const lastPlaybackDiagnosticStateRef = useRef<string | null>(null)
  // True when a state broadcast carries a seek from ANOTHER device for the
  // track this (active) device is playing. Track changes are handled by the
  // playback-sync effect, and the echo of our own seek or periodic position
  // save is ignored (seeking back to it would make audio jump on that cadence).
  function rememberSeekSent(seconds: number) {
    const now = Date.now()
    recentSeeksSentRef.current = [
      ...recentSeeksSentRef.current.filter((sent) => now - sent.at < SEEK_ECHO_WINDOW_MS),
      { seconds, at: now },
    ]
  }

  function isRemoteSeekForActiveDevice(state: PlaybackStateSnapshot): boolean {
    if (state.active_device_id !== deviceIdRef.current) return false
    if (Date.now() < resyncUntilRef.current) return false
    const now = Date.now()
    if (recentSeeksSentRef.current.some(
      (sent) => now - sent.at < SEEK_ECHO_WINDOW_MS && Math.abs(sent.seconds - state.position_seconds) < 0.05,
    )) return false
    const live = [activeAudioRef.current, videoRef.current, bgAudioRef.current].find(
      (el) => el != null && elementItemsRef.current.get(el)?.id === state.media_item_id,
    )
    if (!live) return false
    return Math.abs(live.currentTime - state.position_seconds) > 1
  }

  const setPlayIntent = useCallback((intent: PlayIntent | null) => {
    playIntentRef.current = intent
    setPlayIntentState(intent)
    window.clearTimeout(playIntentTimerRef.current)
    // A command the server never confirms (lost on a half-open socket, or
    // rejected) must not outrank its snapshots for ever.
    if (intent?.sentAt != null) {
      playIntentTimerRef.current = window.setTimeout(() => {
        if (playIntentRef.current === intent) setPlayIntent(null)
      }, PLAY_INTENT_CONFIRM_MS)
    }
  }, [])

  useEffect(() => () => {
    window.clearTimeout(playIntentTimerRef.current)
    window.clearTimeout(offlineQueueTimerRef.current)
  }, [])

  // Drops every held command (not play/pause) once the oldest has waited too
  // long: they only make sense in order, so a later one is not sent without
  // the earlier ones. One message per drop, however many commands it was.
  const scheduleOfflineExpiry = useCallback(() => {
    window.clearTimeout(offlineQueueTimerRef.current)
    const oldest = offlineQueueRef.current.find((entry) => entry.msg.type !== 'play' && entry.msg.type !== 'pause')
    if (!oldest) return
    offlineQueueTimerRef.current = window.setTimeout(() => {
      const before = offlineQueueRef.current.length
      offlineQueueRef.current = offlineQueueRef.current.filter(
        (entry) => entry.msg.type === 'play' || entry.msg.type === 'pause',
      )
      if (offlineQueueRef.current.length === before) return
      logPlaybackDiagnostic('offline_commands_dropped', { count: before - offlineQueueRef.current.length })
      showToast('Not connected - try again', 'error', 'playback-offline')
    }, Math.max(0, oldest.at + OFFLINE_COMMAND_TTL_MS - Date.now()))
  }, [showToast])

  const send = useCallback((msg: Record<string, unknown>, options: { queue?: boolean; external?: boolean } = {}) => {
    const command = typeof msg.type === 'string' ? msg.type : null
    const diagnosticCommand = command != null && ['transfer_to', 'next', 'play', 'pause'].includes(command)
    const ws = wsRef.current
    if (command !== 'heartbeat') {
      debugLog('ws.send', {
        ...Object.fromEntries(Object.entries(msg).filter(([k, v]) => k !== 'client_token' && (v == null || typeof v !== 'object'))),
        ws_state: ws?.readyState ?? null,
      })
    }
    const playPause = command === 'play' || command === 'pause'
    // A socket that is open but not registered yet (the server ignores
    // commands until then), or registered with held commands not yet
    // flushed, is not ready for a user command either.
    const ready = ws?.readyState === WebSocket.OPEN &&
      playbackSocketRegisteredRef.current &&
      !offlineFlushPendingRef.current
    if (playPause) {
      setPlayIntent({
        command,
        sentAt: ready ? Date.now() : null,
        external: options.external === true,
      })
    }
    // A position report is stale by the next one: never held, and not
    // written to a socket that is not ready either, where it would overtake
    // the held commands and a track_ended replay (see offlineFlushPendingRef).
    if (!ready && command === 'position_report') return
    if (!ready && (playPause || (options.queue !== false && OFFLINE_QUEUED_COMMANDS.has(command)))) {
      const queue = offlineQueueRef.current.filter((entry) => (
        // play/pause collapse to the latest; so does playing a new list.
        !(playPause && (entry.msg.type === 'play' || entry.msg.type === 'pause')) &&
        !(command === 'set_queue_context' && entry.msg.type === 'set_queue_context')
      ))
      queue.push({ msg, at: Date.now() })
      offlineQueueRef.current = queue
      scheduleOfflineExpiry()
      if (diagnosticCommand) {
        logPlaybackDiagnostic('command_held_socket_unavailable', {
          command,
          device_id: deviceIdRef.current,
          websocket_ready_state: ws?.readyState ?? WebSocket.CLOSED,
        })
      }
      return
    }
    if ((command === 'seek' || command === 'position_report') && typeof msg.position_seconds === 'number') {
      rememberSeekSent(msg.position_seconds)
    }
    if (ws?.readyState === WebSocket.OPEN) {
      if (diagnosticCommand) {
        logPlaybackDiagnostic('command_sent', {
          command,
          device_id: deviceIdRef.current,
          target_device_id: msg.device_id ?? null,
        })
      }
      ws.send(JSON.stringify(msg))
    } else if (diagnosticCommand) {
      logPlaybackDiagnostic('command_not_sent_socket_unavailable', {
        command,
        device_id: deviceIdRef.current,
        websocket_ready_state: ws?.readyState ?? WebSocket.CLOSED,
      })
    }
  }, [scheduleOfflineExpiry, setPlayIntent])

  // Sends what was held while the socket was down, in the order it was given,
  // on the first snapshot after a registration (see offlineFlushPendingRef).
  // A pause or play the OS made is only this device's business while it is
  // still the active one; another device may have taken over meanwhile.
  const flushOfflineCommands = useCallback((ws: WebSocket, state: PlaybackStateSnapshot) => {
    offlineFlushPendingRef.current = false
    window.clearTimeout(offlineQueueTimerRef.current)
    const queued = offlineQueueRef.current
    offlineQueueRef.current = []
    const now = Date.now()
    // A hidden page's timers are throttled, so the expiry may not have run.
    const expired = queued.some((entry) => (
      entry.msg.type !== 'play' && entry.msg.type !== 'pause' && now - entry.at > OFFLINE_COMMAND_TTL_MS
    ))
    if (expired) showToast('Not connected - try again', 'error', 'playback-offline')
    const intent = playIntentRef.current
    for (const { msg } of queued) {
      const playPause = msg.type === 'play' || msg.type === 'pause'
      if (expired && !playPause) continue
      if (playPause && intent?.external && state.active_device_id !== deviceIdRef.current) {
        setPlayIntent(null)
        continue
      }
      if (msg.type === 'seek' && typeof msg.position_seconds === 'number') rememberSeekSent(msg.position_seconds)
      // A held `pause` carries where the element stopped (`pauseMessage`),
      // with no `seek` to that position after it. The server is always at
      // least as new as this client (one image serves both), so there is no
      // fallback for one that ignores the field.
      ws.send(JSON.stringify(msg))
      if (playPause && intent && intent.sentAt == null && intent.command === msg.type) {
        setPlayIntent({ ...intent, sentAt: Date.now() })
      }
    }
    if (queued.length > 0) {
      logPlaybackDiagnostic('offline_commands_flushed', {
        device_id: deviceIdRef.current,
        commands: queued.map((entry) => entry.msg.type).join(','),
        expired,
      })
    }
  }, [setPlayIntent, showToast])

  useEffect(() => {
    let cancelled = false
    let reconnectTimer: number | undefined
    let connectTimer: number | undefined
    // Failed attempts since the last registration: the next one waits
    // 1 s, 2 s, 4 s ... up to 30 s. A short fixed delay would retry a
    // rejected socket every few seconds for ever in every open tab - the
    // pattern an intrusion filter in front of the server bans an address for.
    let reconnectAttempt = 0
    // Set when reconnecting by timer must stop: the session is gone (4401),
    // or another tab of this browser took the device over (4000), which only
    // this tab coming back to the foreground or gaining focus undoes.
    let stopped: 'auth' | 'replaced' | null = null
    let probingSession = false
    // When anything last arrived on the current socket, when the last
    // heartbeat went out on it, and how many heartbeats in a row got nothing
    // back (see the heartbeat below).
    let lastActivityAt = Date.now()
    let lastHeartbeatAt = 0
    let missedHeartbeats = 0

    function scheduleReconnect() {
      window.clearTimeout(reconnectTimer)
      const base = Math.min(RECONNECT_MAX_DELAY_MS, RECONNECT_BASE_DELAY_MS * 2 ** reconnectAttempt)
      reconnectAttempt += 1
      // Up to 20% early, so tabs and devices that lost the server together
      // do not all come back in the same instant.
      reconnectTimer = window.setTimeout(connect, base * (0.8 + Math.random() * 0.2))
    }

    // Drops the current socket (left half-open by a suspended phone, or
    // silent) and connects again at once. Its late close is ignored.
    function replaceSocket(reason: string) {
      const staleSocket = wsRef.current
      if (staleSocket && (staleSocket.readyState === WebSocket.OPEN || staleSocket.readyState === WebSocket.CONNECTING)) {
        connectionGenerationRef.current += 1
        wsRef.current = null
        playbackSocketRegisteredRef.current = false
        setConnected(false)
        deviceIdRef.current = null
        staleSocket.close(4001, reason)
      }
      connect()
    }

    function connect() {
      if (cancelled || stopped === 'auth') return
      if (
        wsRef.current?.readyState === WebSocket.OPEN ||
        wsRef.current?.readyState === WebSocket.CONNECTING
      ) return
      window.clearTimeout(reconnectTimer)
      window.clearTimeout(connectTimer)
      stopped = null
      setSocketReplaced(false)
      const generation = ++connectionGenerationRef.current
      const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
      // The Android app with no connection is its own playback server, for
      // its downloads (native/localPlayback.ts): the same messages and
      // snapshots, so everything below is the same either way.
      const local = isNativeApp() && isOffline()
      const ws = local
        ? (new LocalPlaybackSocket() as unknown as WebSocket)
        : new WebSocket(`${protocol}://${window.location.host}/ws/playback`)
      // A queue changed while offline, to hand to the server on this socket's
      // first snapshot; the snapshots before the server has taken it are not
      // applied (they still name what the server last knew).
      let handingBack: 'undecided' | 'sent' | 'done' = local || !isNativeApp() ? 'done' : 'undecided'
      let handbackTimer: number | undefined
      wsRef.current = ws
      playbackSocketRegisteredRef.current = false
      offlineFlushPendingRef.current = false
      lastPlaybackDiagnosticStateRef.current = null
      let opened = false
      // The revision of the last snapshot accepted on this socket. Kept per
      // socket, so it starts over with every registration: a new socket takes
      // whatever the server has, even a lower revision (a server restored from
      // a backup counts from where that left off).
      let appliedRevision: number | null = null
      lastHeartbeatAt = 0
      missedHeartbeats = 0
      logPlaybackDiagnostic('websocket_connecting', {
        generation,
        visibility: document.visibilityState,
      })
      // A handshake that neither completes nor fails (a network that drops
      // packets) would otherwise block every reconnect: connect() returns
      // early while a socket is CONNECTING. Closing it fails it, and its
      // close schedules the next attempt.
      connectTimer = window.setTimeout(() => {
        if (generation !== connectionGenerationRef.current || ws.readyState !== WebSocket.CONNECTING) return
        logPlaybackDiagnostic('websocket_connect_timeout', { generation })
        ws.close()
      }, CONNECT_TIMEOUT_MS)

      ws.onopen = () => {
        if (cancelled || generation !== connectionGenerationRef.current) return
        opened = true
        window.clearTimeout(connectTimer)
        // The server took the socket: it is there, whatever a check of it
        // still under way comes back with.
        if (!local && isNativeApp()) noteServerAnswered()
        lastActivityAt = Date.now()
        const { name, deviceType } = guessDeviceLabel()
        const shouldResume = readPlaybackResumeFlag()
        logPlaybackDiagnostic('websocket_open', {
          generation,
          device_type: deviceType,
          resume_playback: shouldResume,
        })
        ws.send(JSON.stringify({
          type: 'register_device',
          name,
          device_type: deviceType,
          resume_playback: shouldResume,
          client_token: playbackDeviceToken(),
        }))
      }

      ws.onmessage = async (event) => {
        if (cancelled || generation !== connectionGenerationRef.current) return
        lastActivityAt = Date.now()
        const msg = JSON.parse(event.data)
        // Liveness only: `lastActivityAt` above is all it is for (see the
        // heartbeat below). Not logged, like the heartbeats it answers.
        if (msg.type === 'heartbeat_ack') return
        debugLog('ws.recv', {
          type: msg.type,
          device_id: deviceIdRef.current,
          active_device_id: msg.state?.active_device_id,
          media_item_id: msg.state?.media_item_id,
          is_playing: msg.state?.is_playing,
          position: msg.state?.position_seconds,
          queue_index: msg.state?.queue_index,
          position_changed: msg.position_changed,
          error: msg.type === 'error' ? msg.message : undefined,
        })
        if (msg.type === 'registered') {
          // The Android app changing between its server and its own stand-in
          // (native/localPlayback.ts) is the same device under a new id. Until
          // the new socket's first state arrives, the state held names the
          // old id as the active device; read as it is, this page would be a
          // remote control for that moment and pause the music it is playing.
          const previousId = lastRegisteredDeviceIdRef.current
          const swapped = previousId != null && previousId !== msg.device_id &&
            (previousId === LOCAL_DEVICE_ID || msg.device_id === LOCAL_DEVICE_ID)
          if (swapped) {
            const moveActive = (state: PlaybackStateSnapshot | null) => (
              state && state.active_device_id === previousId ? { ...state, active_device_id: msg.device_id } : state
            )
            lastServerStateRef.current = moveActive(lastServerStateRef.current)
            setSnapshot(moveActive)
          }
          deviceIdRef.current = msg.device_id
          lastRegisteredDeviceIdRef.current = msg.device_id
          playbackSocketRegisteredRef.current = true
          offlineFlushPendingRef.current = true
          reconnectAttempt = 0
          logPlaybackDiagnostic('device_registered', {
            generation,
            device_id: msg.device_id,
            pending_track_ended: pendingTrackEndedRef.current != null,
          })
          setConnected(true)
          forceDeviceIdTick((t) => t + 1)
        } else if (msg.type === 'state') {
          const state: PlaybackStateSnapshot = msg.state
          // A snapshot with a lower revision than one already accepted on this
          // socket is older than what this page shows: dropped. An equal one
          // repeats a state already accepted and is dropped too, except as
          // the server's answer to this device's own track_ended for an item
          // that was no longer current (a replay after a reconnect, or Next
          // pressed elsewhere first): that answer changed nothing, so it keeps
          // the revision, but it is the one message that says the server has
          // moved on from the ended item. It is applied in full like any new
          // snapshot (it names the server's real current item), after the
          // pending end is cleared below.
          const ownTrackEndedReply = msg.source_command === 'track_ended' &&
            msg.source_device_id != null && msg.source_device_id === deviceIdRef.current
          if (appliedRevision != null && (
            state.revision < appliedRevision ||
            (state.revision === appliedRevision && !ownTrackEndedReply)
          )) {
            debugLog('ws.state_dropped_old_revision', { revision: state.revision, applied_revision: appliedRevision })
            return
          }
          const staleEndReply = ownTrackEndedReply && state.revision === appliedRevision
          appliedRevision = state.revision
          if (handingBack === 'undecided') {
            const played = handback()
            const elsewhere = state.is_playing && state.active_device_id != null &&
              state.active_device_id !== deviceIdRef.current &&
              state.devices.some((device) => device.id === state.active_device_id && device.is_connected)
            if (played && !elsewhere) {
              // What played here offline becomes the server's queue, at the
              // place this device is, in one change.
              const live = activeAudioRef.current
              const liveItem = live ? elementItemsRef.current.get(live) : null
              const position = live && liveItem?.id === played.startId && Number.isFinite(live.currentTime)
                ? live.currentTime
                : played.position
              handingBack = 'sent'
              logPlaybackDiagnostic('offline_queue_handed_back', { media_item_id: played.startId, rows: played.ids.length })
              ws.send(JSON.stringify({
                type: 'set_queue_context',
                context: { kind: 'ids', ids: played.ids.slice(0, MAX_IDS_PER_MESSAGE) },
                start_media_item_id: played.startId,
                start_index: played.startIndex,
                position_seconds: position,
                paused: played.paused,
              }))
              if (played.repeat !== state.repeat_mode) ws.send(JSON.stringify({ type: 'set_repeat', mode: played.repeat }))
              // A refusal (an item gone from the library) must not leave the page deaf.
              handbackTimer = window.setTimeout(() => {
                if (handingBack !== 'sent') return
                handingBack = 'done'
                clearHandback()
                replaceSocket('handback unanswered')
              }, 8_000)
              return
            }
            // Something else is playing on another device, or nothing was
            // played offline: the server's state stands.
            handingBack = 'done'
            clearHandback()
          } else if (handingBack === 'sent') {
            const taken = msg.source_command === 'set_queue_context' && msg.source_device_id === deviceIdRef.current
            if (!taken) return
            handingBack = 'done'
            window.clearTimeout(handbackTimer)
            clearHandback()
          }
          if (!local) noteServerState(state)
          const snapshotGeneration = ++snapshotGenerationRef.current
          if (cancelled) return
          const previousState = lastServerStateRef.current
          lastServerStateRef.current = state
          const diagnosticState = {
            device_id: deviceIdRef.current,
            active_device_id: state.active_device_id,
            is_active_device: state.active_device_id === deviceIdRef.current,
            media_item_id: state.media_item_id,
            is_playing: state.is_playing,
            queue_index: state.queue_index,
            queue_length: state.queue_media_item_ids.length,
            queue_context_count: state.queue_context_count,
            repeat_mode: state.repeat_mode,
          }
          const serializedDiagnosticState = JSON.stringify(diagnosticState)
          if (serializedDiagnosticState !== lastPlaybackDiagnosticStateRef.current) {
            lastPlaybackDiagnosticStateRef.current = serializedDiagnosticState
            logPlaybackDiagnostic('state_received', diagnosticState)
          }
          // The server now agrees with the play/pause this device applied
          // locally: snapshots are in charge again.
          const intent = playIntentRef.current
          if (intent?.sentAt != null && state.is_playing === (intent.command === 'play')) {
            setPlayIntent(null)
          }
          // The server's answer to a stale track_ended (see above): it had
          // already moved on from the ended item, so there is nothing left to
          // replay, and this snapshot is not stale for the local advance - it
          // names where the server really is. (Unless it names the very item
          // the pending end is about: then it answered an older end.)
          if (
            staleEndReply &&
            pendingTrackEndedRef.current &&
            state.current_queue_item_id !== pendingEndMetaRef.current.endedQueueItemId
          ) {
            logPlaybackDiagnostic('pending_track_ended_cleared_by_stale_reply', {
              generation,
              device_id: deviceIdRef.current,
              media_item_id: pendingTrackEndedRef.current.media_item_id,
              queue_item_id: pendingEndMetaRef.current.endedQueueItemId,
            })
            pendingTrackEndedRef.current = null
          }
          // A track_ended that could not be sent before the reconnect is
          // replayed only if the server is still on that track and this
          // device is its active one; otherwise it would skip a track.
          // While the server is still on the track this device already
          // finished (e.g. the app was backgrounded and the socket was
          // suspended across a track change), its snapshot is STALE: applying
          // it would put `current` back on the old track and then, once the
          // replayed track_ended advances the server to the next track at
          // 0:00, seek the already-playing next track back to the start.
          // So replay, tell the server where the element really is, keep
          // the local state and let the server's follow-up broadcasts apply.
          const pendingEnd = pendingTrackEndedRef.current
          // "Still on that track" is "still on that queue item" when the ended
          // item is known: with the same track queued twice in a row the
          // server's next item has the same media id, and a pending end kept
          // for it would be replayed after the user went back to the first.
          // With several ends kept (offline across track ends, see
          // PendingEndMeta), the server may still be on any of them: the ends
          // from its row on are replayed, in order.
          const endChain = [...pendingEndMetaRef.current.earlierEndedQueueItemIds, pendingEndMetaRef.current.endedQueueItemId]
          const replayFrom = state.current_queue_item_id != null ? endChain.indexOf(state.current_queue_item_id) : -1
          const serverMovedOn = pendingEnd != null && (pendingEndMetaRef.current.endedQueueItemId != null
            ? replayFrom < 0
            : state.media_item_id !== pendingEnd.media_item_id)
          // An old end is dropped, but not while this device's element is
          // still on the track the local advance moved to: the end is then
          // still true however long the socket was gone (a locked phone's
          // socket can stay suspended past a minute), and dropping it would
          // apply this stale snapshot - the finished track would reload at its
          // last saved position and play its tail again, and the next track
          // would then restart from 0.
          const pendingEndAge = pendingEnd ? Date.now() - pendingEndMetaRef.current.at : 0
          const liveElement = activeAudioRef.current
          const liveElementItem = liveElement ? elementItemsRef.current.get(liveElement) : undefined
          const stillOnLocalAdvance = pendingEnd != null && liveElementItem != null &&
            pendingEndMetaRef.current.advancedToId != null &&
            liveElementItem.id === pendingEndMetaRef.current.advancedToId
          if (pendingEnd && pendingEndAge > PENDING_END_MAX_AGE_MS &&
              (!stillOnLocalAdvance || pendingEndAge > PENDING_END_MAX_AGE_ON_ADVANCE_MS)) {
            pendingTrackEndedRef.current = null
            logPlaybackDiagnostic('pending_track_ended_expired', {
              generation,
              device_id: deviceIdRef.current,
              media_item_id: pendingEnd.media_item_id,
              age_ms: pendingEndAge,
              still_on_local_advance: stillOnLocalAdvance,
            })
          } else if (pendingEnd && deviceIdRef.current != null) {
            if (serverMovedOn) {
              pendingTrackEndedRef.current = null
              logPlaybackDiagnostic('pending_track_ended_dropped_server_advanced', {
                generation,
                device_id: deviceIdRef.current,
                media_item_id: pendingEnd.media_item_id,
              })
            } else if (
              state.active_device_id === deviceIdRef.current &&
              pendingEndMetaRef.current.endedQueueItemId != null &&
              ws.readyState === WebSocket.OPEN &&
              pendingEndMetaRef.current.sentGeneration !== generation &&
              state.repeat_mode !== 'one'
            ) {
              ws.send(JSON.stringify({ type: 'client_diagnostic', event: 'media_ended', ...pendingEnd }))
              // Earlier ends the server has not processed: each moves it on by
              // one row, up to the row of the last end.
              for (const queueItemId of endChain.slice(replayFrom, -1)) {
                ws.send(JSON.stringify({ type: 'track_ended', queue_item_id: queueItemId }))
              }
              // The same queue item as the first attempt: if that one did
              // arrive after all, the server ignores this and answers with
              // its current state (see staleEndReply above).
              ws.send(JSON.stringify(trackEndedMessage()))
              pendingEndMetaRef.current.sentGeneration = generation
              // The server restarts the next track at 0 on track_ended; ignore
              // that echo (it is not another device's seek) while we correct it.
              resyncUntilRef.current = Date.now() + 8_000
              // A position report for the item the element is on (the one the
              // local advance moved to): applied once the replayed end has
              // made it current, ignored if the server went elsewhere.
              const live = activeAudioRef.current
              const liveItem = live ? elementItemsRef.current.get(live) : undefined
              const liveQueueItemId = queueItemIdFor(liveItem?.id)
              if (live && liveItem && liveItem.id !== pendingEnd.media_item_id && liveQueueItemId != null &&
                  live.readyState > 0 && Number.isFinite(live.currentTime) && live.currentTime > 0) {
                rememberSeekSent(live.currentTime)
                ws.send(JSON.stringify({
                  type: 'position_report',
                  queue_item_id: liveQueueItemId,
                  position_seconds: live.currentTime,
                }))
              }
              logPlaybackDiagnostic('pending_track_ended_sent_after_reconnect', {
                generation,
                device_id: deviceIdRef.current,
                media_item_id: pendingEnd.media_item_id,
              })
              // After the end it reports: a Next pressed while the socket was
              // down meant "the track after the one now playing".
              if (offlineFlushPendingRef.current) flushOfflineCommands(ws, state)
              return
            }
          }
          if (offlineFlushPendingRef.current && ws.readyState === WebSocket.OPEN) flushOfflineCommands(ws, state)
          // A broadcast the server sent before it processed this device's
          // track_ended (the echo of the position report sent just before the
          // end, another device's command) still names the track that just
          // ended. Applied in full, `current` would go back to the ended
          // track, the sync effect would reload it at its tail, and the
          // server's next-track snapshot would then restart the next track
          // from 0 - on a locked phone each of those reloads is a deferred
          // play() that can be rejected.
          // While the end is pending and the local advance moved to another
          // track, such a snapshot is stale for track identity: keep `current`,
          // the position and the play state, and apply only what cannot rewind
          // playback (devices and active device, repeat, shuffle, the queue).
          // Its media_item_id/queue_index name the ended track, exactly like
          // the snapshot already held here, so they rewind nothing. Repeat-one
          // and a one-item queue restart the same track and apply in full; so
          // does a server in repeat-one (it stays on that track, see above) or
          // a snapshot handing playback to another device.
          const staleEnd = pendingTrackEndedRef.current
          const advancedToId = pendingEndMetaRef.current.advancedToId
          if (
            staleEnd &&
            deviceIdRef.current != null &&
            state.active_device_id === deviceIdRef.current &&
            state.media_item_id === staleEnd.media_item_id &&
            advancedToId != null &&
            advancedToId !== staleEnd.media_item_id &&
            state.repeat_mode !== 'one'
          ) {
            logPlaybackDiagnostic('stale_snapshot_after_local_advance', {
              generation,
              device_id: deviceIdRef.current,
              media_item_id: staleEnd.media_item_id,
              advanced_to_id: advancedToId,
            })
            setSnapshot((prev) => (prev
              ? { ...state, position_seconds: prev.position_seconds, is_playing: prev.is_playing }
              : state))
            resolveQueue(
              state.queue_media_item_ids,
              state.queue_index,
              () => !cancelled && snapshotGeneration === snapshotGenerationRef.current,
            )
            return
          }
          setSnapshot(state)
          // The active device saves its live position every five seconds.
          // That save is broadcast to every device, including its sender;
          // seeking the sender back to the saved (and therefore slightly
          // older) value would make audio jump backwards on that same cadence.
          // A different active device's seek still needs to be applied.
          // Never while unregistered (a reconnect leaves deviceIdRef null, so
          // every broadcast would look like another device's seek), and keyed to
          // the track it was meant for so it can't land on a later one.
          if (
            msg.position_changed === true &&
            deviceIdRef.current != null &&
            state.active_device_id !== deviceIdRef.current
          ) {
            setPositionCommand({
              seconds: state.position_seconds,
              nonce: snapshotGeneration,
              trackId: state.media_item_id,
            })
          } else if (
            msg.position_changed === true &&
            deviceIdRef.current != null &&
            // Our own position save or track end coming back: the element
            // already moved on from it, however late the echo arrives.
            !(msg.source_device_id === deviceIdRef.current &&
              OWN_ECHO_COMMANDS.has(msg.source_command)) &&
            // `remove_from_queue` and `transfer_to` are broadcast with
            // position_changed but carry no new position, only the last one
            // the active device saved. Once this device has been paused longer
            // than SEEK_ECHO_WINDOW_MS that save no longer looks like its own,
            // and the element would be seeked back to it - up to five seconds. They
            // only move playback when they change what plays: a removal that
            // changes the current queue item, or a transfer that makes this
            // device the active one (the position is then the previous
            // device's). The server's own previous state says which.
            !(msg.source_command === 'remove_from_queue' &&
              previousState != null &&
              previousState.queue_item_ids[previousState.queue_index] === state.queue_item_ids[state.queue_index]) &&
            !(msg.source_command === 'transfer_to' &&
              previousState?.active_device_id === deviceIdRef.current) &&
            isRemoteSeekForActiveDevice(state)
          ) {
            // Another device scrubbed while this one is playing: move the
            // element there. (The check above excludes our own echoes.)
            setPositionCommand({
              seconds: state.position_seconds,
              nonce: snapshotGeneration,
              trackId: state.media_item_id,
            })
          }
          // Resolved independently (not one `await` after the other) so a
          // failure resolving the whole queue - e.g. a transient network
          // blip - can't also block `current` from updating and silently
          // stall playback on whatever track was showing before.
          if (state.media_item_id != null) {
            resolveIds([state.media_item_id])
              .then(([resolvedCurrent]) => {
                if (!cancelled && snapshotGeneration === snapshotGenerationRef.current) {
                  setCurrent(resolvedCurrent ?? null)
                }
              })
              .catch(() => {
                // Best-effort - a stale `current` is recoverable (the next
                // state broadcast retries), unlike leaving it silently unset.
              })
          } else {
            setCurrent(null)
          }
          // Best-effort like `current` above: a queue that could not be
          // looked up is asked for again by the next state broadcast.
          resolveQueue(
            state.queue_media_item_ids,
            state.queue_index,
            () => !cancelled && snapshotGeneration === snapshotGenerationRef.current,
          )
        } else if (msg.type === 'accent_color_changed') {
          // Another device changed the account's accent color - apply it
          // here live, and keep the auth context's copy in sync so a
          // reload doesn't briefly flash the old color. No toast: an
          // unattended device shouldn't nag about a cosmetic change.
          const currentUser = userRef.current
          if (typeof msg.accent_color === 'string' && currentUser) {
            applyAccentColor(msg.accent_color)
            if (currentUser.accent_color !== msg.accent_color) {
              // Functional updater so a concurrent setUser in the same
              // batch (e.g. an in-flight username change) is merged, not
              // clobbered by this handler's stale snapshot of the user.
              setUser((prev) =>
                prev ? { ...prev, accent_color: msg.accent_color } : prev,
              )
            }
          }
        } else if (msg.type === 'audio_settings_changed') {
          // Another device changed the account's levelling/EQ - mirror it
          // here (unless this device has an edit still waiting to be saved).
          applyRemoteAudioSettings(msg as Partial<AudioSettings>)
        } else if (msg.type === 'error') {
          // The server rejects a command (an invalid queue, a device or
          // media item that no longer exists, etc.) by sending this back.
          // It is shown, so a rejected action does not look like it silently
          // did nothing.
          logPlaybackDiagnostic('server_error_received', {
            device_id: deviceIdRef.current,
            message: typeof msg.message === 'string' ? msg.message : 'unknown',
          })
          showToast(typeof msg.message === 'string' ? msg.message : 'Something went wrong.', 'error')
        }
      }

      ws.onclose = (event) => {
        if (generation !== connectionGenerationRef.current) return
        logPlaybackDiagnostic('websocket_closed', {
          generation,
          device_id: deviceIdRef.current,
          code: event.code,
          reason: event.reason,
          was_clean: event.wasClean,
          pending_track_ended: pendingTrackEndedRef.current != null,
        })
        playbackSocketRegisteredRef.current = false
        setConnected(false)
        deviceIdRef.current = null
        window.clearTimeout(connectTimer)
        window.clearTimeout(handbackTimer)
        if (cancelled) return
        // In the Android app a server that is gone means playing on from the
        // downloads: find out now (see the offline subscription below).
        if (!local && isNativeApp()) void checkOffline()
        if (event.code === 4401) {
          // No valid session: the server accepts the socket and closes it
          // with 4401 at once, or closes an open one when the session ends.
          // Retrying cannot work, show the login page.
          stopped = 'auth'
          notifyUnauthorized()
          return
        }
        if (event.code === 4000) {
          // Another tab of this browser registered the same device and the
          // server dropped this one. Reconnecting by timer would take it
          // back, and that tab would do the same: wait to be used again.
          stopped = 'replaced'
          setSocketReplaced(true)
          return
        }
        if (!opened && !probingSession) {
          // A socket refused before it opened (1006): a proxy may turn away
          // a socket without a valid session before accepting it, so ask the
          // API whether the session is still there. A 401 runs the session-expired handler (api.ts) and stops
          // the retries; anything else is a network problem: keep backing off.
          probingSession = true
          api.me()
            .catch((error: unknown) => {
              if (!cancelled && error instanceof ApiError && error.status === 401) {
                stopped = 'auth'
                window.clearTimeout(reconnectTimer)
              }
            })
            .finally(() => {
              probingSession = false
            })
        }
        // Reconnect - a phone locking/backgrounding is far more likely to
        // drop this than a desktop tab, and losing playback control
        // permanently on wake is not acceptable.
        scheduleReconnect()
      }
      ws.onerror = () => {
        logPlaybackDiagnostic('websocket_error', {
          generation,
          device_id: deviceIdRef.current,
          ready_state: ws.readyState,
        })
        ws.close()
      }
    }

    connect()
    // The server answers every heartbeat (on a registered socket) with a
    // `heartbeat_ack` to this socket alone (no snapshot: a
    // non-active device's clock is re-based by the active device's position
    // reports instead), so a healthy socket hears something at least every 20
    // seconds. A socket can stay OPEN on a connection that delivers nothing
    // (a NAT or proxy that dropped it silently, a network change): commands
    // vanish and no close ever comes. Two heartbeats in a row with nothing
    // back, and nothing at all for SOCKET_SILENCE_LIMIT_MS, mean it is gone:
    // replace it. Counted per heartbeat rather than by elapsed time alone so
    // that timers firing late (a machine waking from sleep) are not mistaken
    // for silence before the socket had a chance to deliver. Hidden pages are
    // left alone: their timers are throttled and the return to the
    // foreground replaces the socket anyway.
    const heartbeat = window.setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        if (lastHeartbeatAt > 0 && lastActivityAt < lastHeartbeatAt) missedHeartbeats += 1
        else missedHeartbeats = 0
        if (
          document.visibilityState === 'visible' &&
          missedHeartbeats >= 2 &&
          Date.now() - lastActivityAt >= SOCKET_SILENCE_LIMIT_MS
        ) {
          logPlaybackDiagnostic('websocket_silent_replaced', { silent_ms: Date.now() - lastActivityAt })
          replaceSocket('silent')
          return
        }
        lastHeartbeatAt = Date.now()
      }
      send({ type: 'heartbeat' })
    }, HEARTBEAT_INTERVAL_MS)

    function handleVisibility() {
      if (document.visibilityState !== 'visible') return
      if (stopped === 'replaced') stopped = null
      // A suspended mobile browser can leave a WebSocket reporting OPEN even
      // though its TCP connection can no longer deliver messages. Always
      // replace the foregrounded socket so queue control does not remain
      // attached to that half-open connection.
      replaceSocket('refresh after foreground')
    }
    document.addEventListener('visibilitychange', handleVisibility)

    // A tab whose device was taken over by another tab (4000) takes it back
    // when the user turns to it.
    function handleFocus() {
      if (stopped !== 'replaced') return
      stopped = null
      connect()
    }
    window.addEventListener('focus', handleFocus)

    // The network is back: no need to sit out the rest of a long backoff.
    function handleOnline() {
      if (stopped) return
      connect()
    }
    window.addEventListener('online', handleOnline)

    // The Android app lost its server or found it again: change over to the
    // other kind of socket at once, not after the rest of a backoff.
    const stopWatchingOffline = onOfflineChange(() => {
      if (cancelled || stopped === 'auth') return
      reconnectAttempt = 0
      logPlaybackDiagnostic('playback_server_changed', { offline: isOffline() })
      replaceSocket(isOffline() ? 'server unreachable' : 'server reachable')
    })

    return () => {
      stopWatchingOffline()
      cancelled = true
      connectionGenerationRef.current += 1
      window.clearInterval(heartbeat)
      window.clearTimeout(reconnectTimer)
      window.clearTimeout(connectTimer)
      document.removeEventListener('visibilitychange', handleVisibility)
      window.removeEventListener('focus', handleFocus)
      window.removeEventListener('online', handleOnline)
      wsRef.current?.close()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // "Reconnecting…" once the socket has stayed down for a few seconds on a
  // page the user is looking at. A hidden page's socket is expected to drop,
  // and the replacement on every return to the foreground takes a moment.
  useEffect(() => {
    let timer: number | undefined
    const arm = () => {
      window.clearTimeout(timer)
      setReconnecting(false)
      if (connected || socketReplaced || document.visibilityState !== 'visible') return
      timer = window.setTimeout(() => setReconnecting(true), RECONNECTING_NOTICE_DELAY_MS)
    }
    arm()
    document.addEventListener('visibilitychange', arm)
    return () => {
      window.clearTimeout(timer)
      document.removeEventListener('visibilitychange', arm)
    }
  }, [connected, socketReplaced])

  return { send }
}
