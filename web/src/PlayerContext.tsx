import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react'
import type Hls from 'hls.js'
import { api } from './api'
import { setPlaybackStarting } from './playbackPriority'
import { debugLog, describeMedia } from './clientDebugLog'
import { useAuth } from './AuthContext'
import { publishPreferenceChange, subscribePreferenceChange } from './favoritesBus'
import { useSettings } from './SettingsContext'
import type { StreamQuality } from './settings'
import { audioEffects, DEFAULT_AUDIO_SETTINGS, eqIsFlat, type AudioSettings } from './audioEffects'
import { useToast } from './ToastContext'
import type { MediaItem, PlaybackDevice, PlaybackStateSnapshot, PlaySource, PreferenceState, RadioStation, TrackSort } from './types'
import { logPlaybackDiagnostic, MAX_CHAINED_ENDS, type PendingEndMeta, type PlaybackEndDiagnostic } from './player/diagnostics'
import {
  MEDIA_ERR_ABORTED,
  MEDIA_ERR_DECODE,
  MEDIA_ERR_NETWORK,
  streamFailureIsTransient,
  trackStreamStatus,
  MEDIA_RETRY_DELAYS_MS,
  MEDIA_RETRY_RESET_AFTER_SECONDS,
} from './player/mediaRecovery'
import { KEEP_MEDIA_SESSION_ALIVE, setMediaSessionPosition } from './player/mediaSession'
import { mediaLookupChunks, sameItems } from './player/mediaLookup'
import { deriveOutputState, keepsElementsThroughSocketLoss } from './player/outputState'
import { MAX_IDS_PER_MESSAGE, type PlayIntent } from './player/socketPolicy'
import {
  readRadioResumeStation,
  writePlaybackResumeFlag,
  writeRadioResumeStation,
} from './player/storage'
import { createPlayerTimeStore, PlayerTimeContext, type PlayerTime } from './player/timeStore'
import { createPlayerValueStore, PlayerValueStoreContext } from './playerSelectors'
import { listenStartHint, type ListenStartHint } from './player/playReports'
import { usePlayReports } from './player/usePlayReports'
import { usePlaybackSocket } from './player/usePlaybackSocket'
import { useRadioControls, useRadioStream } from './player/useRadio'
import { useAdaptiveBitrate, useAdaptiveBitrateMonitor } from './player/useAdaptiveBitrate'
import { useHlsVideo, useBackgroundVideoAudio } from './player/useHlsVideo'
import { usePrefetchCache, useStandbyPrefetch, type UpcomingTrackDownload } from './player/usePrefetch'
import { useMediaSessionSync, useMediaSessionKeepAlive } from './player/useMediaSession'
import { useOutputClaim } from './player/useOutputClaim'
import { useAccountSettings } from './player/useAccountSettings'
import { volumeGain } from './player/volumeCurve'
import { isNativeApp } from './native/bridge'
import {
  backWithNativePlayer,
  onNativeTransportCommand,
  setNativeEqualiser,
  setNativeGain,
  setNativeTrackInfo,
} from './native/nativeAudio'

export type { PlayerTime } from './player/timeStore'

/** The player's commands. Their identity never changes (each calls the
 * latest implementation), so a component that only issues commands never
 * re-renders because the player's state moved. */
const PLAYER_ACTION_KEYS = [
  'setVolume',
  'openNowPlaying',
  'closeNowPlaying',
  'playQueue',
  'playQueueIds',
  'playLibraryContext',
  'playNext',
  'addToQueueEnd',
  'playNextMany',
  'addManyToQueueEnd',
  'togglePlayPause',
  'skipNext',
  'skipPrev',
  'seekTo',
  'cycleCurrentPreference',
  'transferTo',
  'toggleShuffle',
  'setShuffle',
  'cycleRepeat',
  'jumpToQueueIndex',
  'removeFromQueue',
  'moveQueueItem',
  'reorderQueueItem',
  'removeDevice',
  'playRadioStation',
  'toggleRadioPlayPause',
  'stopRadio',
  'setAccountAccentColor',
  'updateAudioSettings',
] as const satisfies readonly (keyof PlayerContextValue)[]

export type PlayerActions = Pick<PlayerContextValue, (typeof PLAYER_ACTION_KEYS)[number]>

const EMPTY_IDS: number[] = []
const EMPTY_DEVICES: PlaybackDevice[] = []

export interface PlayerContextValue {
  queue: MediaItem[]
  queueItemIds: number[]
  currentIndex: number
  isPlaying: boolean
  /** The active element is still waiting on audio/video data for `current`. */
  mediaLoading: boolean
  isShuffled: boolean
  repeatMode: 'off' | 'all' | 'one'
  queueContextCount: number
  /** What the queue was started from (an album, artist, playlist, mix or
   * Songs filter), shared by every device; null when it is not known or
   * autoplay has moved on past it. */
  queueOrigin: PlaySource | null
  current: MediaItem | null
  volume: number
  setVolume: (volume: number) => void
  devices: PlaybackDevice[]
  deviceId: number | null
  isActiveDevice: boolean
  connected: boolean
  /** The playback socket has been down for a few seconds and is being retried. */
  reconnecting: boolean
  adaptiveQualityNotice: string | null
  audioRef: React.RefObject<HTMLAudioElement | null>
  videoRef: React.RefObject<HTMLVideoElement | null>
  nowPlayingOpen: boolean
  openNowPlaying: () => void
  closeNowPlaying: () => void
  /** `origin`: what the queue is started from, for Now Playing's
   * "Playing from" link (see queueOrigin). */
  playQueue: (items: MediaItem[], startIndex: number, origin?: PlaySource) => void
  playQueueIds: (ids: number[], startIndex: number, origin?: PlaySource) => void
  /** `order`: the Songs tab's sort, so the queue follows the rows on
   * screen; left out, the server's default order (artist A-Z). */
  playLibraryContext: (
    filters: { q?: string; genre?: string; tag?: string; artist?: string; album?: string },
    startMediaItemId: number,
    order?: { sort: TrackSort; direction: 'asc' | 'desc' },
    origin?: PlaySource,
  ) => void
  playNext: (item: MediaItem) => void
  addToQueueEnd: (item: MediaItem) => void
  /** Queues `items`, in this order, right after the current item: one
   * `add_many_to_queue` message (one state change on the server). */
  playNextMany: (items: MediaItem[]) => void
  /** Appends `items`, in this order, to the end of the queue: one message. */
  addManyToQueueEnd: (items: MediaItem[]) => void
  togglePlayPause: () => void
  skipNext: () => void
  skipPrev: () => void
  seekTo: (seconds: number) => void
  /** Applies the Now Playing heart's next state to the current track.
   * Rejects (after rolling back) when the change failed. */
  cycleCurrentPreference: (next: PreferenceState) => Promise<void>
  transferTo: (deviceId: number) => void
  toggleShuffle: () => void
  setShuffle: (enabled: boolean) => void
  cycleRepeat: () => void
  jumpToQueueIndex: (index: number) => void
  removeFromQueue: (queueItemId: number) => void
  moveQueueItem: (queueItemId: number, direction: -1 | 1) => void
  reorderQueueItem: (queueItemId: number, targetIndex: number) => void
  removeDevice: (deviceId: number) => void
  radioStation: RadioStation | null
  isRadioPlaying: boolean
  radioNowPlaying: string | null
  playRadioStation: (station: RadioStation) => void
  toggleRadioPlayPause: () => void
  stopRadio: () => void
  /** Changes the account-wide accent color: applies it here immediately,
   * saves it to the account, and every other connected device picks it up
   * live over the playback WebSocket. */
  setAccountAccentColor: (color: string) => void
  /** Per-account volume levelling + equaliser (synced across devices). */
  audioSettings: AudioSettings
  /** Optimistic partial update; saved to the account after a short debounce. */
  updateAudioSettings: (patch: Partial<AudioSettings>) => void
}

const PlayerContext = createContext<PlayerContextValue | null>(null)

const PlayerActionsContext = createContext<PlayerActions | null>(null)
// Whether Now Playing is open, on its own: the views' header slots need only
// this, and reading it from the whole player value would re-render every view
// on each pause, play or track change.
const NowPlayingOpenContext = createContext(false)

export function PlayerProvider({ children }: { children: ReactNode }) {
  const { settings } = useSettings()
  const { showToast } = useToast()
  const { user, setUser } = useAuth()
  // The WebSocket handler is a long-lived closure; reading `user` through a
  // ref keeps its accent-color logic from acting on a stale account object
  // after login/logout without re-creating the socket.
  const userRef = useRef(user)
  userRef.current = user
  const audioRef = useRef<HTMLAudioElement>(null)
  const preloadAudioRef = useRef<HTMLAudioElement>(null)
  const activeAudioRef = useRef<HTMLAudioElement | null>(null)
  // In the Android app the two music elements are played by native players
  // (see native/nativeAudio.ts). A layout effect, so it is in place before
  // any effect below gives an element a source.
  useLayoutEffect(() => {
    if (!isNativeApp()) return
    if (audioRef.current) backWithNativePlayer(audioRef.current, 'a')
    if (preloadAudioRef.current) backWithNativePlayer(preloadAudioRef.current, 'b')
    // Radio, and a video's sound while the app is in the background: played
    // natively too, so they carry on with the screen off.
    if (radioAudioRef.current) backWithNativePlayer(radioAudioRef.current, 'r')
    if (bgAudioRef.current) backWithNativePlayer(bgAudioRef.current, 'v')
    audioEffects.setNativeSink({ equaliser: setNativeEqualiser, gain: setNativeGain })
    setNativeTrackInfo((url, mediaId) => {
      if (url.includes('/api/radio/')) {
        const station = radioStationRef.current
        return station ? { title: station.name, artist: 'Radio' } : null
      }
      const item = mediaCacheRef.current.get(mediaId)
      if (!item) return null
      return {
        title: item.title,
        artist: item.artist,
        album: item.album,
        artUrl: item.kind === 'track' ? api.artUrl(item.id) : api.thumbnailUrl(item.id),
      }
    })
    return onNativeTransportCommand((command) => mediaSessionCommandRef.current(command))
  }, [])
  const videoRef = useRef<HTMLVideoElement>(null)
  // Mobile browsers pause a <video> once the page is hidden, so while hidden
  // a video's sound is carried by this audio-only element instead.
  const bgAudioRef = useRef<HTMLAudioElement>(null)
  const videoBackgroundedRef = useRef(false)
  const hlsRef = useRef<Hls | null>(null)
  // Bumped on every video source change so a slow answer can't attach late.
  const videoLoadRef = useRef(0)
  const playingRef = useRef(false)
  // Pauses/plays the app triggers itself (source swaps, hand-offs, quality
  // switches) must not be mistaken for the OS pausing the media (headphones
  // unplugged, another app taking audio focus). Anything inside this window
  // is ours; see `noteExternalTransition`.
  const appMediaChangeUntilRef = useRef(0)
  const isActiveDeviceRef = useRef(false)
  const radioAudioRef = useRef<HTMLAudioElement>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const deviceIdRef = useRef<number | null>(null)
  const mediaCacheRef = useRef<Map<number, MediaItem>>(new Map())
  const connectionGenerationRef = useRef(0)
  // When the pending track_ended was last written to a socket (connection generation) and created.
  // It is only cleared once the server is seen on another track: a write to a
  // suspended mobile socket can look successful and never arrive.
  // `advancedToId` is the track the local advance moved to (null if it did not
  // move), so a late snapshot for the ended track can be told apart.
  // `endedQueueItemId` is the queue item that ended, captured at `ended` and
  // sent with every track_ended for this end (the reconnect replay included),
  // so the server advances at most once for it. `advancedToQueueItemId` is the
  // queue item the local advance moved to: what the element plays until the
  // server names it. Either is null when it could not be told for certain.
  // `earlierEndedQueueItemIds` are the rows that ended before this one while
  // the server had not yet been seen past them (a phone offline across
  // several track ends), oldest first: the reconnect replays the ends from
  // the server's current row on, so it arrives where the element is.
  const pendingEndMetaRef = useRef<PendingEndMeta>({
    sentGeneration: null,
    at: 0,
    advancedToId: null,
    endedQueueItemId: null,
    advancedToQueueItemId: null,
    earlierEndedQueueItemIds: [],
  })
  // Where a non-active device's displayed position started counting from.
  const remoteClockRef = useRef<{ seconds: number; at: number; playing: boolean }>({ seconds: 0, at: 0, playing: false })
  // Mobile browsers may suspend or close the WebSocket while the screen is
  // locked even though the media element continues playing. Keep a natural
  // end event until the replacement socket has registered, otherwise the
  // queue stops at the lock-screen boundary.
  const playbackSocketRegisteredRef = useRef(false)
  const pendingTrackEndedRef = useRef<PlaybackEndDiagnostic | null>(null)
  // The last state the server sent, applied or not: what a broadcast changed.
  const lastServerStateRef = useRef<PlaybackStateSnapshot | null>(null)
  // The device id of the last registration, kept across a dropped socket
  // (deviceIdRef is cleared on close) so a page that was the active device
  // still acts on its own element while the control plane is down.
  const lastRegisteredDeviceIdRef = useRef<number | null>(null)
  const [playIntent, setPlayIntentState] = useState<PlayIntent | null>(null)
  // The lock-screen handlers are registered once per track; they call the
  // current play/pause/next/prev logic through this.
  const mediaSessionCommandRef = useRef<(command: 'play' | 'pause' | 'next' | 'prev') => void>(() => {})
  // Recovery from a network or decode error on the playing element: the last
  // position its track reached while healthy (the reload resumes there), and
  // the reloads used so far for that track, the one waiting, and where the
  // last one resumed from. Reloading is done by `reloadMediaRef` so a delayed
  // retry runs with the current render's state.
  const lastGoodPositionRef = useRef<{ trackId: number; seconds: number } | null>(null)
  // The track whose failed stream is being asked for its status (see
  // handlePlaybackMediaError), so a repeated error event asks only once.
  const mediaStatusCheckRef = useRef<number | null>(null)
  const mediaRetryRef = useRef<{ trackId: number | null; attempts: number; timer: number | undefined; resumedAt: number | null }>({
    trackId: null,
    attempts: 0,
    timer: undefined,
    resumedAt: null,
  })
  const reloadMediaRef = useRef<(userInitiated: boolean) => void>(() => {})
  // The same for the radio element. `wanted` is whether the user wants the
  // station playing (a pause or stop ends the retries).
  const radioRetryRef = useRef<{ attempts: number; timer: number | undefined; stallTimer: number | undefined; playingSince: number | null; lastPosition: number; wanted: boolean }>({
    attempts: 0,
    timer: undefined,
    stallTimer: undefined,
    playingSince: null,
    lastPosition: 0,
    wanted: false,
  })
  const playIntentRef = useRef<PlayIntent | null>(null)

  const [snapshot, setSnapshot] = useState<PlaybackStateSnapshot | null>(null)
  const [current, setCurrent] = useState<MediaItem | null>(null)
  // `queue` is what playback works from (the next track, the prefetch, the
  // local advance at a track end); `listedQueue` is what the queue lists show.
  // They are the same array except while a long queue is still being looked
  // up (see resolveQueue): playback then already has the rows around the
  // playing one, while the lists keep the last complete queue - their rows
  // are matched to the server's queue items by position, which a queue with
  // rows still missing would get wrong.
  const [queue, setQueue] = useState<MediaItem[]>([])
  const [listedQueue, setListedQueue] = useState<MediaItem[]>([])
  // The position and duration live outside React state (see PlayerTime):
  // a tick notifies the components reading them and re-renders nothing else,
  // not even this provider.
  // Called through the ref so the many effects that set them need not list them.
  const [timeStore] = useState(createPlayerTimeStore)
  const timeStoreRef = useRef(timeStore)
  // True while the active element has been asked for a track but hasn't
  // started playing it yet (a cold disk can leave this for several seconds).
  const [mediaLoading, setMediaLoading] = useState(false)
  const [volume, setVolumeState] = useState(1)
  // Which item each media element has loaded, so volume levelling uses the gain
  // of the track in *that* element - the standby element holds the next one.
  const elementItemsRef = useRef(new WeakMap<HTMLMediaElement, MediaItem>())
  // The stream URL each element was told to play. An element loaded from an
  // in-memory copy has a `blob:` src, so `element.src` alone can no longer say
  // which track it holds.
  const elementSrcRef = useRef(new WeakMap<HTMLMediaElement, string>())
  // stream URL -> blob: URL of the fully downloaded track, oldest first.
  const trackBlobsRef = useRef(new Map<string, string>())
  const keepAliveAudioRef = useRef<HTMLAudioElement>(null)
  const prefetchRef = useRef<{ src: string; controller: AbortController } | null>(null)
  // The gain the slider position stands for (see volumeCurve.ts), not the
  // position itself: every element's volume is set from this.
  const volumeRef = useRef(volumeGain(volume))
  const [connected, setConnected] = useState(false)
  const [reconnecting, setReconnecting] = useState(false)
  // The server closed this page's socket because another tab of the same
  // browser (the same device, as far as the server can tell) registered.
  // That tab is the output now. Unlike a network dropout, which leaves a
  // playing element alone, this page must fall silent at once, or both tabs
  // would play the same queue a few seconds apart, with no way to stop this
  // one from the other. The ref is for callbacks.
  const [socketReplaced, setSocketReplacedState] = useState(false)
  const socketReplacedRef = useRef(false)
  const setSocketReplaced = useCallback((replaced: boolean) => {
    socketReplacedRef.current = replaced
    setSocketReplacedState(replaced)
  }, [])
  const [, forceDeviceIdTick] = useState(0)
  const [radioStation, setRadioStation] = useState<RadioStation | null>(() => readRadioResumeStation())
  // For the native notification's text (see the layout effect above).
  const radioStationRef = useRef(radioStation)
  radioStationRef.current = radioStation
  const radioResumeRequestedRef = useRef(radioStation != null)
  const [isRadioPlaying, setIsRadioPlaying] = useState(false)
  const [radioNowPlaying, setRadioNowPlaying] = useState<string | null>(null)
  const [nowPlayingOpen, setNowPlayingOpenState] = useState(isNowPlayingEntry)
  const [positionCommand, setPositionCommand] = useState<{ seconds: number; nonce: number; trackId: number | null } | null>(null)
  const [adaptiveQuality, setAdaptiveQuality] = useState<{
    trackId: number
    quality: StreamQuality | null
  } | null>(null)
  // An upgrade is decided mid-track but only applied from the next track on
  // (switching mid-song would stall it). `afterTrackId` is the track it was
  // decided on; that track keeps its quality.
  const [adaptiveNextQuality, setAdaptiveNextQuality] = useState<{
    afterTrackId: number
    quality: StreamQuality | null
  } | null>(null)
  const adaptiveUpgradeStartRef = useRef<{ fromTrackId: number; quality: StreamQuality | null } | null>(null)
  const qualitySwitchPositionRef = useRef<{ trackId: number; seconds: number } | null>(null)
  const adaptiveUpgradeProbeRef = useRef<{ trackId: number; quality: StreamQuality | null } | null>(null)
  const adaptiveRejectedUpgradesRef = useRef<{
    trackId: number
    qualities: Array<StreamQuality | null>
  } | null>(null)

  // Whether this page is the output (see outputState.ts), as of this render:
  // what the effects and the hooks below act on.
  const outputState = deriveOutputState({
    connected,
    socketReplaced,
    deviceId: deviceIdRef.current,
    lastDeviceId: lastRegisteredDeviceIdRef.current,
    activeDeviceId: snapshot?.active_device_id,
  })
  const isActiveDevice = outputState === 'active'

  isActiveDeviceRef.current = isActiveDevice
  // For timers that name the playing queue item (see `queueItemIdFor`): a
  // video element is not tracked in `elementItemsRef`, it plays `current`.
  const currentMediaIdRef = useRef<number | null>(null)
  currentMediaIdRef.current = current?.id ?? null

  function cacheItems(items: MediaItem[]) {
    for (const item of items) mediaCacheRef.current.set(item.id, item)
  }

  // Volume levelling + equaliser live on the account. State is optimistic;
  // edits (a slider drag fires many) are saved after a short debounce.
  const [audioSettings, setAudioSettings] = useState<AudioSettings>(DEFAULT_AUDIO_SETTINGS)

  const { setAccountAccentColor, applyRemoteAudioSettings, updateAudioSettings } = useAccountSettings({
    user,
    setUser,
    showToast,
    audioSettings,
    setAudioSettings,
  })

  // The ids being asked for right now, so a second snapshot arriving while a
  // long queue is still being looked up waits for those answers instead of
  // asking for the same rows again.
  const mediaLookupsRef = useRef(new Map<number, Promise<boolean>>())

  // Fetches the items of `ids` that are not cached, a bounded number per
  // request (see mediaLookupChunks), the rows around `aroundIndex` first;
  // `onFirstChunk` runs once those are in and more are to come. One request
  // after the other: they share the connection with the track being
  // started. A request that fails costs only its own rows - what the others
  // brought stays cached, and the missing rows are asked for again by the
  // next lookup (the next snapshot).
  // Resolves false if any request failed.
  const loadItems = useCallback(async (ids: number[], aroundIndex = 0, onFirstChunk?: () => void): Promise<boolean> => {
    const cache = mediaCacheRef.current
    const lookups = mediaLookupsRef.current
    let complete = true
    const chunks = mediaLookupChunks(ids, (id) => cache.has(id), aroundIndex)
    for (const [position, chunk] of chunks.entries()) {
      const waits = new Set<Promise<boolean>>()
      const fresh: number[] = []
      for (const id of chunk) {
        const pending = lookups.get(id)
        if (pending) waits.add(pending)
        else if (!cache.has(id)) fresh.push(id)
      }
      if (fresh.length > 0) {
        const request: Promise<boolean> = api.getMediaByIds(fresh).then(
          (items) => {
            // Two lookups can overlap (a snapshot resolves its current track
            // and its queue at once): keep the copy cached first, so a track
            // is always the same object. A second copy replacing it would make
            // the next snapshot hand `current` a new object for the same track,
            // which would re-run every effect keyed on it (see the buffering
            // monitor's source start).
            for (const item of items) if (!cache.has(item.id)) cache.set(item.id, item)
            return true
          },
          () => false,
        ).finally(() => {
          for (const id of fresh) if (lookups.get(id) === request) lookups.delete(id)
        })
        for (const id of fresh) lookups.set(id, request)
        waits.add(request)
      }
      if ((await Promise.all(waits)).includes(false)) complete = false
      if (position === 0 && chunks.length > 1) onFirstChunk?.()
    }
    return complete
  }, [])

  const cachedItems = useCallback((ids: number[]): MediaItem[] => (
    ids
      .map((id) => mediaCacheRef.current.get(id))
      .filter((item): item is MediaItem => item != null)
  ), [])

  // Rejects when a request failed: the callers keep what
  // they show and the next snapshot asks again.
  const resolveIds = useCallback(async (ids: number[]): Promise<MediaItem[]> => {
    if (!(await loadItems(ids))) throw new Error('media lookup failed')
    return cachedItems(ids)
  }, [cachedItems, loadItems])

  // A snapshot's queue. Playback gets the rows around the playing one as soon
  // as their request is in, and again when the rest is; the lists get the
  // queue once it is complete. If a request failed, playback still gets every
  // row that did arrive (so the next track is there to prefetch and to advance
  // to) and the lists keep what they had until a later snapshot's lookup
  // fills the gap. `wanted` says
  // whether this snapshot is still the latest.
  // `queueLookupCompleteRef`: whether `queue` came from a lookup that got
  // every request answered (see advanceLocalTrackAfterEnd).
  const queueLookupCompleteRef = useRef(true)
  const resolveQueue = useCallback((ids: number[], index: number, wanted: () => boolean) => {
    const publish = (complete: boolean) => {
      if (!wanted()) return
      queueLookupCompleteRef.current = complete
      const items = cachedItems(ids)
      setQueue((prev) => (sameItems(prev, items) ? prev : items))
      if (complete) setListedQueue((prev) => (sameItems(prev, items) ? prev : items))
    }
    void loadItems(ids, index, () => publish(false)).then(publish)
  }, [cachedItems, loadItems])

  // The queue item a local element playing `mediaId` is on, or null when that
  // cannot be told for certain - then a position report is skipped, and a
  // track_ended or seek falls back as `messageQueueItemIdFor` says. The id
  // matters because the same track can be queued twice, and because after a
  // local advance the element is on the next item while the last server state
  // still names the one that ended: that item's id with the next track's time
  // would land on the wrong row.
  //  1. While a track end is pending and the server still names the item that
  //     ended, the element is on the item the local advance moved to (the same
  //     row again for repeat-one), if it plays that track; otherwise unknown.
  //  2. Else the server's current item, if it is that track.
  //  3. Else the track's row if it is queued exactly once (a remote skip the
  //     element has not followed yet: the server ignores a non-current id).
  function queueItemIdFor(mediaId: number | null | undefined): number | null {
    const state = lastServerStateRef.current
    if (mediaId == null || state == null) return null
    const ended = pendingTrackEndedRef.current
    const meta = pendingEndMetaRef.current
    if (ended && (meta.endedQueueItemId != null
      ? state.current_queue_item_id === meta.endedQueueItemId
      : state.media_item_id === ended.media_item_id)) {
      return meta.advancedToId === mediaId ? meta.advancedToQueueItemId : null
    }
    if (state.media_item_id === mediaId) return state.current_queue_item_id
    const index = state.queue_media_item_ids.indexOf(mediaId)
    if (index < 0 || state.queue_media_item_ids.indexOf(mediaId, index + 1) >= 0) return null
    return state.queue_item_ids[index] ?? null
  }

  // The queue row a track_ended or seek about `mediaId` names - the server
  // requires one on both. `queueItemIdFor`'s answer, else the last server
  // state's current row when that state's current item is `mediaId` (with the
  // same song queued twice, the row the server says is playing). Null when
  // `mediaId` is not the server's current item at all: a message about it
  // would be about something the server no longer plays, so none is sent.
  function messageQueueItemIdFor(mediaId: number | null | undefined): number | null {
    const known = queueItemIdFor(mediaId)
    if (known != null) return known
    const state = lastServerStateRef.current
    return mediaId != null && state != null && state.media_item_id === mediaId ? state.current_queue_item_id : null
  }

  // The pending end's track_ended (a pending end always has its row, see
  // handleTrackEnded). Refs only, so the socket handler (created once) can
  // call it too.
  function trackEndedMessage(): Record<string, unknown> {
    return { type: 'track_ended', queue_item_id: pendingEndMetaRef.current.endedQueueItemId }
  }

  // The track a media element holds: audio elements are tracked per element
  // (the standby holds the next track); the video and its background audio
  // element play `current`.
  function elementMediaId(element: HTMLMediaElement): number | null {
    const tracked = elementItemsRef.current.get(element)?.id
    const isVideoElement = element === videoRef.current || element === bgAudioRef.current
    return tracked ?? (isVideoElement ? currentMediaIdRef.current : null)
  }

  function elementQueueItemId(element: HTMLMediaElement): number | null {
    return queueItemIdFor(elementMediaId(element))
  }

  // True while this page is the device producing the sound - including while
  // its socket is down, when `isActiveDevice` (which needs `connected`) is
  // false although the element is still playing (see the sync effect's
  // dropout comment).
  // Never while another tab has taken this device over.
  // `active` is the last render's (what the effects have acted on); the
  // dropout is read from the refs as they are now, because the callbacks
  // asking run between renders: a socket that just closed, or the last state
  // the server sent, has not reached a render yet.
  const outputsLocally = useCallback(() => (
    isActiveDeviceRef.current || deriveOutputState({
      connected: playbackSocketRegisteredRef.current,
      socketReplaced: socketReplacedRef.current,
      deviceId: deviceIdRef.current,
      lastDeviceId: lastRegisteredDeviceIdRef.current,
      activeDeviceId: lastServerStateRef.current?.active_device_id,
    }) === 'dropout'
  ), [])

  // What the user (or the OS) last asked for wins over the server until the
  // server confirms it (see PlayIntent); the play button shows it at once.
  const effectiveIsPlaying = playIntent ? playIntent.command === 'play' : !!snapshot?.is_playing

  // What started the next listen (see usePlayReports): set by the commands
  // that change the current item from this page.
  const listenStartRef = useRef<ListenStartHint | null>(null)
  const { noteListening, noteListenEnded, noteListenFailed } = usePlayReports({
    connected,
    current,
    outputsLocally,
    elementQueueItemId,
    videoRef,
    bgAudioRef,
    activeAudioRef,
    videoBackgroundedRef,
    elementItemsRef,
    playing: effectiveIsPlaying,
    serverMediaId: snapshot?.media_item_id ?? null,
    activeDeviceId: snapshot?.active_device_id ?? null,
    lastRegisteredDeviceIdRef,
    listenStartRef,
  })

  // Every few seconds, what each media element is doing - the gaps and jumps
  // between these lines are how a restart shows up in the server log.
  useEffect(() => {
    const timer = window.setInterval(() => {
      for (const [name, ref] of [['active', activeAudioRef], ['primary', audioRef], ['preload', preloadAudioRef], ['bg', bgAudioRef], ['video', videoRef]] as const) {
        const el = ref.current
        if (el && (el.currentSrc || !el.paused)) {
          debugLog('media.heartbeat', { role: name, ...describeMedia(el), ws_state: wsRef.current?.readyState ?? null })
        }
      }
    }, 10000)
    return () => window.clearInterval(timer)
  }, [])

  const { send } = usePlaybackSocket({
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
  })

  useEffect(() => {
    if (!positionCommand || !isActiveDevice || !current) return
    // Meant for another track (or not resolved yet): wait for that track, and
    // drop it once the server has moved elsewhere.
    if (positionCommand.trackId != null && positionCommand.trackId !== current.id) {
      if (snapshot?.media_item_id !== positionCommand.trackId) setPositionCommand(null)
      return
    }
    // One-shot: re-running this on an isActiveDevice / current change would
    // seek back to a stale position (e.g. after unlocking the phone).
    setPositionCommand(null)
    const element = current.kind === 'track'
      ? activeAudioRef.current
      : (videoBackgroundedRef.current ? bgAudioRef.current : videoRef.current)
    if (element) {
      element.currentTime = positionCommand.seconds
      timeStoreRef.current.setCurrentTime(positionCommand.seconds)
    }
  }, [positionCommand, isActiveDevice, current, snapshot?.media_item_id])

  const { stopRadio, reloadRadio, toggleRadioPlayPause, handleRadioFailure, noteRadioWaiting, noteRadioProgress } = useRadioControls({
    radioAudioRef,
    radioRetryRef,
    radioResumeRequestedRef,
    radioStation,
    setRadioStation,
    setIsRadioPlaying,
    setRadioNowPlaying,
    showToast,
  })

  const isVideo = current?.kind !== 'track'
  const { streamBitrate, nextTrackBitrate, adaptiveQualityNotice } = useAdaptiveBitrate({
    isVideo,
    current,
    settings,
    adaptiveQuality,
    adaptiveNextQuality,
    setAdaptiveQuality,
    setAdaptiveNextQuality,
    adaptiveUpgradeStartRef,
    qualitySwitchPositionRef,
    adaptiveUpgradeProbeRef,
    adaptiveRejectedUpgradesRef,
  })

  const setVolume = useCallback((nextVolume: number) => {
    setVolumeState(Math.max(0, Math.min(1, nextVolume)))
  }, [])

  // Levelling factor for element.volume: 1 when the Web Audio chain carries
  // the gain (or levelling is off); a cut-only factor on the plain path.
  // Only the two music <audio> elements may be routed through Web Audio.
  const levelFactor = useCallback((element: HTMLMediaElement) => {
    // Not through Web Audio in the Android app: the sound comes from the
    // native players there, which apply the same settings themselves.
    const canRoute = !isNativeApp() && (element === audioRef.current || element === preloadAudioRef.current)
    return audioEffects.applyElement(element, elementItemsRef.current.get(element), canRoute)
  }, [])

  const applyElementVolume = useCallback((element: HTMLMediaElement) => {
    element.volume = volumeRef.current * levelFactor(element)
  }, [levelFactor])

  const noteElementItem = useCallback((element: HTMLMediaElement, item: MediaItem) => {
    elementItemsRef.current.set(element, item)
    applyElementVolume(element)
  }, [applyElementVolume])

  // Hand the settings to the audio engine; it routes elements through Web
  // Audio only when needed, and resumes its context when the page returns.
  useEffect(() => {
    audioEffects.configure(audioSettings)
    audioEffects.setReadyCallback(() => {
      if (audioRef.current) applyElementVolume(audioRef.current)
      if (preloadAudioRef.current) applyElementVolume(preloadAudioRef.current)
    })
    if (!audioSettings.volume_leveling && eqIsFlat(audioSettings.eq_gains)) return
    audioEffects.resume()
  }, [audioSettings, applyElementVolume])

  const { reportMediaError, hlsErrorHandler, loadVideoSource } = useHlsVideo({
    wsRef,
    elementItemsRef,
    current,
    showToast,
    videoLoadRef,
    hlsRef,
    playingRef,
    videoBackgroundedRef,
  })

  const { findTrackBlob, rememberTrackBlob } = usePrefetchCache({
    trackBlobsRef,
    prefetchRef,
    current,
    queue,
    snapshot,
  })

  // Point an element at a track, preferring the in-memory copy when there is
  // one so no request is made. Callers still call load() themselves.
  // The video element can't just take `src = streamUrl`: a file whose codecs
  // the browser can't decode (HEVC, 10-bit, AC-3 audio, ...) is served as HLS
  // instead, which needs hls.js (or Safari's native HLS). The server says
  // which applies, so the source is attached once that answer is in. Callers
  // keep their usual `setElementSrc` + `load()` + `currentTime = start`
  // sequence; the start position they leave on the element is read back here.
  const setElementSrc = useCallback((element: HTMLMediaElement, streamUrl: string) => {
    elementSrcRef.current.set(element, streamUrl)
    // `src` may be a blob: URL; this says which stream the element holds.
    element.dataset.streamSrc = streamUrl
    if (element === videoRef.current) {
      void loadVideoSource(element as HTMLVideoElement, streamUrl)
      return
    }
    element.src = findTrackBlob(streamUrl) ?? streamUrl
  }, [findTrackBlob, loadVideoSource])

  const elementHasSrc = useCallback((element: HTMLMediaElement, streamUrl: string) => {
    return elementSrcRef.current.get(element) === streamUrl || element.src.endsWith(streamUrl)
  }, [])

  useEffect(() => {
    playingRef.current = effectiveIsPlaying
  }, [effectiveIsPlaying])

  useEffect(() => {
    volumeRef.current = volumeGain(volume)
    if (!activeAudioRef.current) activeAudioRef.current = audioRef.current
    if (audioRef.current) applyElementVolume(audioRef.current)
    if (preloadAudioRef.current) applyElementVolume(preloadAudioRef.current)
    if (videoRef.current) applyElementVolume(videoRef.current)
    if (radioAudioRef.current) radioAudioRef.current.volume = volumeRef.current
  }, [applyElementVolume, volume, audioSettings])

  // Keep the reload marker in sync with the canonical server state. It is
  // deliberately scoped to the active device, so opening a second tab while
  // another device is playing does not steal that device on its first load.
  useEffect(() => {
    if (current && isActiveDevice) writePlaybackResumeFlag(snapshot?.is_playing === true)
    else if (snapshot?.is_playing === false) writePlaybackResumeFlag(false)
  }, [current, isActiveDevice, snapshot?.is_playing])

  useEffect(() => {
    if (radioStation && (isRadioPlaying || radioResumeRequestedRef.current)) {
      writeRadioResumeStation(radioStation)
    }
    else writeRadioResumeStation(null)
  }, [isRadioPlaying, radioStation])

  // `pagehide` is the last reliable opportunity to send the live media
  // position before a reload. The server also receives a position every five
  // seconds, so this is best-effort and does not block navigation. Keeping
  // the marker only in sessionStorage means closing the tab cannot create a
  // resume request in a future tab.
  useEffect(() => {
    function savePlaybackBeforePageHide() {
      const radioElement = radioAudioRef.current
      if (radioStation && radioElement && !radioElement.paused) {
        writeRadioResumeStation(radioStation)
        writePlaybackResumeFlag(false)
        return
      }
      writeRadioResumeStation(null)
      const element = current?.kind === 'track' ? activeAudioRef.current : videoRef.current
      const shouldResume = Boolean(isActiveDevice && snapshot?.is_playing && element && !element.paused)
      writePlaybackResumeFlag(shouldResume)
      // A position report, like the five-second save below.
      const queueItemId = shouldResume && element ? elementQueueItemId(element) : null
      if (element && queueItemId != null) {
        send({ type: 'position_report', queue_item_id: queueItemId, position_seconds: element.currentTime }, { queue: false })
      }
    }
    window.addEventListener('pagehide', savePlaybackBeforePageHide)
    return () => window.removeEventListener('pagehide', savePlaybackBeforePageHide)
  }, [current?.kind, isActiveDevice, radioStation, send, snapshot?.is_playing])

  // Only the active device renders real media locally - everyone else just
  // reflects server state without playing anything. A track uses the audio
  // element, a video (music video or custom video) uses the video element -
  // whichever one applies gets driven, and the other is stopped, so only
  // one is ever actually producing sound. Starting playback here also stops
  // any radio station playing locally - one device can only usefully
  // produce one stream at a time.
  useEffect(() => {
    const audio = audioRef.current
    const preloadAudio = preloadAudioRef.current
    const video = videoRef.current
    if (!audio || !preloadAudio || !video) return
    if (!activeAudioRef.current) activeAudioRef.current = audio
    appMediaChangeUntilRef.current = Date.now() + 1000
    // A play/pause this device already applied to its element outranks a
    // snapshot that still says the opposite because the server has not
    // processed it yet - above all the reconnect's first snapshot after the
    // OS paused the element while the socket was down: playing it again would
    // restart the music from the speaker once the headphones are unplugged.
    const intent = playIntentRef.current
    const wantPlaying = intent ? intent.command === 'play' : snapshot?.is_playing === true
    if (!isActiveDevice || !current) {
      // A dropped WebSocket is a lost *control plane*, not a transport
      // change. A locked phone's socket is suspended routinely (doze) while
      // its media element is still producing sound, and pausing here would
      // kill playback outright: when the sync hidden advance (see
      // `advanceLocalTrackAfterEnd`) has just started the next track, the
      // teardown's pause() aborts its in-flight play() and the rest of the
      // queue never plays. Only an actual hand-off must tear the
      // elements down: on a device transfer `connected` stays true and the
      // snapshot points at another device, whereas a drop means `connected`
      // is false. While hidden the user has no UI to recover from a
      // needless stop, so leave the element that is already playing alone and
      // let the next snapshot (or the user) decide. The same applies when
      // visible: returning to the foreground deliberately replaces the
      // socket (see `handleVisibility`), which momentarily reads as a drop
      // and would otherwise pause audio on every app resume.
      // Not when another tab of this browser took the device over
      // (`socketReplaced`): that is a hand-off too, to a page the server
      // cannot tell from this one.
      const dropoutKeepsPlaying = keepsElementsThroughSocketLoss(outputState, {
        hasCurrent: current != null,
        wantPlaying,
      })
      if (!dropoutKeepsPlaying) {
        audio.pause()
        preloadAudio.pause()
        video.pause()
      }
      return
    }
    let activeEl: HTMLMediaElement = isVideo ? video : activeAudioRef.current
    let inactiveEl: HTMLMediaElement = isVideo
      ? audio
      : activeAudioRef.current === audio ? preloadAudio : audio
    inactiveEl.pause()
    if (isVideo) {
      preloadAudio.pause()
    } else {
      // A song is taking over: the video (and its hls.js instance, which
      // keeps feeding the element) must stop producing sound for good.
      video.pause()
      if (video.dataset.streamSrc || hlsRef.current) {
        videoLoadRef.current += 1
        hlsRef.current?.destroy()
        hlsRef.current = null
        video.removeAttribute('src')
        delete video.dataset.streamSrc
        elementSrcRef.current.delete(video)
        video.load()
      }
    }

    const src = api.streamUrl(current.id, isVideo ? undefined : streamBitrate)
    // If the next track has already been fetched by the standby audio
    // element, promote it instead of throwing that buffer away and starting
    // a second request from the beginning.
    // Mobile browsers associate lock-screen/background playback with the
    // element that received the user's play gesture. Keep that same element
    // while hidden; promoting a second element can make the OS remove the
    // media notification after the first background transition. The visible
    // page can still promote the warmed element for fast manual skips.
    if (
      !isVideo &&
      document.visibilityState === 'visible' &&
      !document.hidden &&
      inactiveEl instanceof HTMLAudioElement &&
      elementHasSrc(inactiveEl, src) &&
      // Not when the active element already holds this track (a track that
      // ended offline moved it there, see advanceLocalTrackAfterEnd): the
      // standby may hold it too, from a preload the dropped network failed,
      // and promoting that would leave the track silent - its error is never
      // retried, as it was not the active element.
      !elementHasSrc(activeEl, src)
    ) {
      activeAudioRef.current = inactiveEl
      activeEl = inactiveEl
      inactiveEl = activeAudioRef.current === audio ? preloadAudio : audio
      inactiveEl.pause()
      // A standby whose own load failed (a blip while it was warming up)
      // stays in its error state: nothing retried it, as it was not the
      // active element then, so promoting it would leave the server on
      // "playing" with no sound and no end of track. Ask for the stream again; if
      // that fails too, it now fails as the active element and is retried.
      if (activeEl.error) activeEl.load()
    }
    // Read before noteElementItem/load(): load() resets currentTime to 0.
    const elementWasPlayingThisTrack =
      elementItemsRef.current.get(activeEl)?.id === current.id && activeEl.readyState > 0
    const livePosition = elementWasPlayingThisTrack && Number.isFinite(activeEl.currentTime)
      ? activeEl.currentTime
      : null
    noteElementItem(activeEl, current)
    if (!elementHasSrc(activeEl, src)) {
      setElementSrc(activeEl, src)
      // Said here, not left to the element's `loadstart`: the effect that
      // downloads the next track runs before any media event arrives, and
      // would find nothing starting and begin its whole-file download
      // alongside this track's stream.
      setPlaybackStarting(true)
      activeEl.load()
      const qualitySwitchPosition = qualitySwitchPositionRef.current
      // When the element is already part-way through this very track (a
      // quality swap, or a re-render that only landed once a locked phone
      // woke up) its own clock is the truth. The server position can be
      // seconds stale or even 0 - a hidden page cannot report it - and the
      // position captured when the switch was decided is older still, so
      // seeking to either jumps playback back.
      // The snapshot's position only counts when it is for this track: after a
      // local advance onto a next track the standby had not preloaded, the
      // snapshot is still the ended track's and its last saved position (near
      // that track's end) would become the new track's start - a shorter
      // track would then end at once and be skipped.
      const startPosition = livePosition ?? (qualitySwitchPosition?.trackId === current.id
        ? qualitySwitchPosition.seconds
        : snapshot?.media_item_id === current.id ? snapshot.position_seconds : 0)
      activeEl.currentTime = startPosition
      if (qualitySwitchPosition?.trackId === current.id) {
        qualitySwitchPositionRef.current = null
        // Swapping the stream mid-track cuts the old audio and briefly
        // decodes the new stream from the wrong place before the seek lands,
        // which is heard as a stutter and a burst of static. Mute across the
        // swap, seek again once the new stream can honour it, and fade back
        // in only when it is genuinely playing from the right position.
        const swapped = activeEl
        swapped.volume = 0
        const restore = () => {
          const target = volumeRef.current * levelFactor(swapped)
          const steps = 8
          let step = 0
          const timer = window.setInterval(() => {
            step += 1
            swapped.volume = Math.min(1, target * (step / steps))
            if (step >= steps) window.clearInterval(timer)
          }, 20)
        }
        const onMetadata = () => {
          if (Math.abs(swapped.currentTime - startPosition) > 0.25) swapped.currentTime = startPosition
        }
        swapped.addEventListener('loadedmetadata', onMetadata, { once: true })
        const onPlaying = () => {
          if (swapped.seeking || swapped.currentTime < startPosition - 1.5) return
          swapped.removeEventListener('playing', onPlaying)
          swapped.removeEventListener('timeupdate', onPlaying)
          // Let a few decoded frames pass before un-muting.
          window.setTimeout(restore, 120)
        }
        swapped.addEventListener('playing', onPlaying)
        swapped.addEventListener('timeupdate', onPlaying)
        // Never leave the element silent if the events above never settle.
        window.setTimeout(() => {
          swapped.removeEventListener('playing', onPlaying)
          swapped.removeEventListener('timeupdate', onPlaying)
          if (swapped.volume === 0) restore()
        }, 4000)
      }
      // Reset the displayed position/duration immediately rather than
      // waiting for the new element's first timeupdate/loadedmetadata event
      // - otherwise the switched-away-from element's last known values (e.g.
      // a track's time/length) stay on screen until the new element (video
      // in particular can be slow to buffer) actually starts reporting.
      timeStoreRef.current.setCurrentTime(startPosition)
      // Seed a duration immediately from the library's own probed value -
      // some containers (a moov/metadata atom at the end of the file) don't
      // let the browser report a real duration until it's buffered enough,
      // which otherwise shows as a stuck "0:00".
      timeStoreRef.current.setDuration(current.duration_seconds ?? 0)
      // Tracks and music videos are reported once listened to (see
      // noteListening): recording on load would miss every track the standby
      // or a hidden advance had already loaded, count a quality switch twice
      // and a paused reload as a play. A plain video is reported once, when
      // loaded, starting now.
      if (current.kind === 'video') {
        api.recordPlayed(current.id, { started_at: new Date().toISOString() }).catch(() => {
          // Best-effort - history is a nicety, not blocking playback.
        })
      }
    }
    if (wantPlaying && isVideo && videoBackgroundedRef.current) {
      // The background audio element is carrying this video's sound.
      stopRadio()
      activeEl.pause()
    } else if (wantPlaying) {
      stopRadio()
      activeEl.play().catch((error: unknown) => {
        logPlaybackDiagnostic('play_promise_rejected', {
          device_id: deviceIdRef.current,
          media_item_id: current.id,
          is_active_device: isActiveDevice,
          paused_after_rejection: activeEl.paused,
          error_name: error instanceof Error ? error.name : 'unknown',
          error_message: error instanceof Error ? error.message : String(error),
        })
        // `current` and `snapshot` update in two separate renders (the
        // latter resolves the media item async), so this effect can fire
        // twice in a row for the same track - .load()-ing again aborts an
        // in-flight .play() and rejects its promise even though playback
        // genuinely succeeds moments later. Only treat this as a real
        // failure (most commonly a browser autoplay policy blocking a
        // programmatic .play() with no user gesture yet, e.g. right after
        // a refresh) if the element is still actually paused when this
        // handler runs - otherwise a superseded/aborted attempt would
        // wrongly stomp a playback that already succeeded.
        // Keep the server in the playing state when autoplay is blocked.
        // The user can retry local playback with the play button.
      })
    } else {
      activeEl.pause()
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outputState, isVideo, current?.id, noteElementItem, snapshot?.is_playing, streamBitrate])

  // The length (and position) shown must follow the track on the active
  // device. A manual Next onto the preloaded standby is promoted above without
  // a reload, so the source-change branch that seeds the duration never runs,
  // and the standby's `loadedmetadata` fired earlier while it was inactive and
  // was ignored: without this the seek bar would keep the previous track's
  // length, and a seek computed as fraction x length would land in the wrong
  // place. Runs after the
  // sync effect (declared after it), so the active element is already the
  // promoted one. An element still loading (readyState 0) knows neither
  // figure yet; the library's probed length stands in until `durationchange`.
  useEffect(() => {
    if (!isActiveDevice || !current) return
    const element = current.kind === 'track' ? activeAudioRef.current : videoRef.current
    const holdsTrack = element != null &&
      elementItemsRef.current.get(element)?.id === current.id &&
      element.readyState > 0
    timeStoreRef.current.setDuration(holdsTrack && Number.isFinite(element.duration) && element.duration > 0
      ? element.duration
      : current.duration_seconds ?? 0)
    if (holdsTrack && Number.isFinite(element.currentTime)) timeStoreRef.current.setCurrentTime(element.currentTime)
  }, [current, isActiveDevice])

  // Set below, once the adaptive monitor (declared after the prefetch, whose
  // effects must keep running first) has returned it.
  const upcomingDownloadRef = useRef<(download: UpcomingTrackDownload) => void>(() => {})
  useStandbyPrefetch({
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
  })

  const { noteUpcomingDownload } = useAdaptiveBitrateMonitor({
    settings,
    isActiveDevice,
    isVideo,
    current,
    activeAudioRef,
    adaptiveRejectedUpgradesRef,
    adaptiveUpgradeProbeRef,
    adaptiveUpgradeStartRef,
    qualitySwitchPositionRef,
    streamBitrate,
    setAdaptiveNextQuality,
    setAdaptiveQuality,
    snapshot,
    showToast,
  })
  upcomingDownloadRef.current = noteUpcomingDownload

  // Non-active devices have no ticking media element to read a live
  // position from - fall back to the last broadcast position, then keep it
  // advancing locally while the active device plays (it only reports every
  // few seconds). Each new snapshot (including the echo of a seek made here)
  // re-bases the clock.
  useEffect(() => {
    if (isActiveDevice) return
    const base = snapshot?.position_seconds ?? 0
    const limit = current?.duration_seconds ?? 0
    const playing = snapshot?.is_playing === true && snapshot.active_device_id != null
    remoteClockRef.current = { seconds: base, at: Date.now(), playing }
    timeStoreRef.current.setCurrentTime(base)
    timeStoreRef.current.setDuration(limit)
    if (!playing) return
    const id = window.setInterval(() => {
      const clock = remoteClockRef.current
      const elapsed = clock.playing ? (Date.now() - clock.at) / 1000 : 0
      const next = clock.seconds + elapsed
      timeStoreRef.current.setCurrentTime(limit > 0 ? Math.min(limit, next) : next)
    }, 250)
    return () => window.clearInterval(id)
  }, [isActiveDevice, snapshot, current?.duration_seconds])

  // Periodically persist the active device's real position so a transfer,
  // reconnect, or another device's view lands close to the right spot.
  useEffect(() => {
    if (!isActiveDevice) return
    const id = window.setInterval(() => {
      const el = isVideo
        ? (videoBackgroundedRef.current ? bgAudioRef.current : videoRef.current)
        : activeAudioRef.current
      if (el && !el.paused) {
        // A position save is stale by the next one: never held for a reconnect.
        // It names the queue item the element is playing, which right after
        // a local advance is not the one the last snapshot names; when that
        // cannot be told this tick's save is skipped (see queueItemIdFor).
        const queueItemId = elementQueueItemId(el)
        if (queueItemId != null) {
          send({ type: 'position_report', queue_item_id: queueItemId, position_seconds: el.currentTime }, { queue: false })
        }
        if (!isVideo) setMediaSessionPosition(el.duration, el.currentTime)
      }
    }, 5000)
    return () => window.clearInterval(id)
  }, [isActiveDevice, isVideo, send])

  useMediaSessionSync({
    current,
    outputState,
    snapshot,
    send,
    mediaSessionCommandRef,
    activeAudioRef,
    videoRef,
  })

  useBackgroundVideoAudio({
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
  })

  useMediaSessionKeepAlive({
    current,
    outputState,
    snapshot,
    keepAliveAudioRef,
  })

  useRadioStream({
    radioAudioRef,
    radioStation,
    radioRetryRef,
    radioResumeRequestedRef,
    setIsRadioPlaying,
    setRadioNowPlaying,
    showToast,
  })

  // Shared with PlayerBar - the "Now Playing" overlay is a history entry
  // (URL unchanged) so the hardware/browser back button, and this state's
  // own close button, both just pop it rather than navigating whatever page
  // is underneath. Lives here (not local to PlayerBar) so the persistent
  // video element below can also react to whether it's actually visible.
  const openNowPlaying = useCallback(() => {
    // On a desktop Now Playing is a page beside the player bar / side panel,
    // whose art still opens it: a second press must not stack another entry.
    if (window.history.state?.nowPlaying === true) return
    window.history.pushState({ nowPlaying: true }, '', NOW_PLAYING_HASH)
    setNowPlayingOpenState(true)
  }, [])
  const closeNowPlaying = useCallback(() => {
    if (nowPlayingOpen) window.history.back()
  }, [nowPlayingOpen])
  useEffect(() => {
    // Back closes it; Forward (back onto its entry) opens it again.
    function handlePopState() {
      setNowPlayingOpenState(isNowPlayingEntry())
    }
    // The app navigating elsewhere (the sidebar, while the desktop page is
    // open) replaces Now Playing's entry and asks it to close.
    function handleClose() {
      setNowPlayingOpenState(false)
    }
    window.addEventListener('popstate', handlePopState)
    window.addEventListener(CLOSE_NOW_PLAYING_EVENT, handleClose)
    return () => {
      window.removeEventListener('popstate', handlePopState)
      window.removeEventListener(CLOSE_NOW_PLAYING_EVENT, handleClose)
    }
  }, [])

  const { primeClaimedPlayback } = useOutputClaim({
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
  })

  const playQueue = useCallback(
    (items: MediaItem[], startIndex: number, origin?: PlaySource) => {
      cacheItems(items)
      const start = items[startIndex]
      if (!start) return
      listenStartRef.current = listenStartHint('user', start.id, true)
      primeClaimedPlayback(start.id)
      send({
        type: 'set_queue_context',
        context: { kind: 'ids', ids: items.map((i) => i.id) },
        start_media_item_id: start.id,
        start_index: startIndex,
        ...(origin ? { origin } : {}),
      })
    },
    [primeClaimedPlayback, send],
  )

  const playQueueIds = useCallback(
    (ids: number[], startIndex: number, origin?: PlaySource) => {
      const start = ids[startIndex]
      if (start == null) return
      listenStartRef.current = listenStartHint('user', start, true)
      primeClaimedPlayback(start)
      send({
        type: 'set_queue_context',
        context: { kind: 'ids', ids },
        start_media_item_id: start,
        start_index: startIndex,
        ...(origin ? { origin } : {}),
      })
    },
    [primeClaimedPlayback, send],
  )

  const playLibraryContext = useCallback(
    (
      filters: { q?: string; genre?: string; tag?: string; artist?: string; artist_is_null?: boolean; album?: string },
      startMediaItemId: number,
      order?: { sort: TrackSort; direction: 'asc' | 'desc' },
      origin?: PlaySource,
    ) => {
      // Windowed server-side (app.playback._ensure_window) - the client
      // never holds/sends the whole matching set, just the filter that
      // defines it, so this works identically whether it matches a dozen
      // tracks or the whole 100k-track library.
      listenStartRef.current = listenStartHint('user', startMediaItemId, true)
      primeClaimedPlayback(startMediaItemId)
      send({
        type: 'set_queue_context',
        context: { kind: 'library', filters, ...(order ? { sort: order.sort, direction: order.direction } : {}) },
        start_media_item_id: startMediaItemId,
        ...(origin ? { origin } : {}),
      })
    },
    [primeClaimedPlayback, send],
  )

  const playNext = useCallback(
    (item: MediaItem) => {
      cacheItems([item])
      send({ type: 'add_to_queue', media_item_id: item.id, mode: 'next' })
    },
    [send],
  )

  const addToQueueEnd = useCallback(
    (item: MediaItem) => {
      cacheItems([item])
      send({ type: 'add_to_queue', media_item_id: item.id, mode: 'end' })
    },
    [send],
  )

  const queueMany = useCallback(
    (items: MediaItem[], mode: 'next' | 'end') => {
      if (items.length === 0) return
      cacheItems(items)
      const ids = items.map((item) => item.id)
      const parts: number[][] = []
      for (let start = 0; start < ids.length; start += MAX_IDS_PER_MESSAGE) {
        parts.push(ids.slice(start, start + MAX_IDS_PER_MESSAGE))
      }
      // Each "next" part lands right after the current item, ahead of the
      // parts already sent, so they go last part first.
      if (mode === 'next') parts.reverse()
      for (const part of parts) send({ type: 'add_many_to_queue', media_item_ids: part, mode })
    },
    [send],
  )
  const playNextMany = useCallback((items: MediaItem[]) => queueMany(items, 'next'), [queueMany])
  const addManyToQueueEnd = useCallback((items: MediaItem[]) => queueMany(items, 'end'), [queueMany])

  // A user gesture can re-authorize playback after a device transfer or a
  // browser autoplay denial. Starting the media here (rather than waiting
  // for the WebSocket state update/effect) keeps that gesture authorization
  // available to the element.
  const retryLocalPlaybackFromGesture = useCallback((seekToSnapshot = false) => {
    if (!current) return
    const element = isVideo ? videoRef.current : activeAudioRef.current ?? audioRef.current
    if (!element) return
    if (seekToSnapshot) {
      const src = api.streamUrl(current.id, isVideo ? undefined : streamBitrate)
      if (!elementHasSrc(element, src)) {
        noteElementItem(element, current)
        setElementSrc(element, src)
        element.load()
      }
      try {
        // An element already holding this track knows the real position;
        // the snapshot's can be seconds stale (or 0 right after a track change).
        const holdsTrack = elementItemsRef.current.get(element)?.id === current.id &&
          element.readyState > 0 && elementHasSrc(element, src)
        // ...and is only this track's when it names this track (see the sync effect).
        if (!holdsTrack) element.currentTime = snapshot?.media_item_id === current.id ? snapshot.position_seconds : 0
      } catch {
        // Some media elements do not accept a seek until metadata arrives;
        // the normal active-device effect applies the saved position later.
      }
    }
    element.play().catch(() => {})
  }, [current, elementHasSrc, isVideo, noteElementItem, setElementSrc, snapshot?.media_item_id, snapshot?.position_seconds, streamBitrate])

  // Play and pause act on this device's own element first and are sent after,
  // so in the gap after every return to the foreground (the socket is
  // replaced) and while a locked phone's socket is suspended, Pause - on
  // screen or on the lock screen - still pauses the music. The command is
  // held until the socket is back (see
  // `send`), and the PlayIntent it records keeps snapshots from undoing it.
  const setLocalPlayback = useCallback((command: 'play' | 'pause') => {
    if (!current || !outputsLocally()) return
    appMediaChangeUntilRef.current = Date.now() + 1000
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = command === 'play' ? 'playing' : 'paused'
    if (command === 'pause') {
      for (const element of [activeAudioRef.current, videoRef.current, bgAudioRef.current]) element?.pause()
      return
    }
    stopRadio()
    if (isVideo && videoBackgroundedRef.current) {
      bgAudioRef.current?.play().catch(() => {})
      return
    }
    // play() on an element in the error state does nothing: load it again.
    if ((isVideo ? videoRef.current : activeAudioRef.current)?.error) {
      reloadMediaRef.current(true)
      return
    }
    retryLocalPlaybackFromGesture(true)
  }, [current, isVideo, outputsLocally, retryLocalPlaybackFromGesture, stopRadio])

  // `pause` always carries `position_seconds` (the server rejects one
  // without the key): where the element of the device producing the sound
  // stopped - the server's own position is only as fresh as the last position
  // report, up to five seconds old. Only an element that has its track's
  // metadata and no error knows its position; otherwise, and on a remote, it
  // is null and the server keeps its saved position.
  const pauseMessage = useCallback((): Record<string, unknown> => {
    if (!current || !outputsLocally()) return { type: 'pause', position_seconds: null }
    const element = current.kind === 'track'
      ? activeAudioRef.current
      : (videoBackgroundedRef.current ? bgAudioRef.current : videoRef.current)
    if (
      element && !element.error && element.readyState >= HTMLMediaElement.HAVE_METADATA &&
      (element !== activeAudioRef.current || elementItemsRef.current.get(element)?.id === current.id) &&
      Number.isFinite(element.currentTime)
    ) {
      return { type: 'pause', position_seconds: element.currentTime }
    }
    return { type: 'pause', position_seconds: null }
  }, [current, outputsLocally])

  const togglePlayPause = useCallback(() => {
    // A device transfer can leave the new device's media element paused when
    // the browser rejects automatic playback. Retry locally from this user
    // gesture instead of sending a pause command that stops the queue.
    if (effectiveIsPlaying && outputsLocally()) {
      const element = isVideo ? videoRef.current : activeAudioRef.current
      if (element?.paused && !element.error) {
        retryLocalPlaybackFromGesture()
        return
      }
    }
    const command = effectiveIsPlaying ? 'pause' : 'play'
    setLocalPlayback(command)
    send(command === 'pause' ? pauseMessage() : { type: command })
  }, [effectiveIsPlaying, isVideo, outputsLocally, pauseMessage, retryLocalPlaybackFromGesture, send, setLocalPlayback])

  mediaSessionCommandRef.current = (command) => {
    if (command === 'next') listenStartRef.current = listenStartHint('auto')
    else if (command === 'prev') listenStartRef.current = listenStartHint('user')
    if (command === 'play' || command === 'pause') setLocalPlayback(command)
    send(command === 'pause' ? pauseMessage() : { type: command })
  }

  // Loads the current track (or direct-play video) into its element again
  // after an error and resumes where it was. An errored element stays dead:
  // play() on it, or selecting the same queue entry, changes nothing while
  // the server goes on saying "playing" and the queue stalls. Always
  // from the stream: if the in-memory copy (prefetchTrackBlob) is what
  // failed, loading it again fails the same way, so it is dropped.
  reloadMediaRef.current = (userInitiated) => {
    const retry = mediaRetryRef.current
    window.clearTimeout(retry.timer)
    retry.timer = undefined
    if (!current || !outputsLocally()) return
    const element = current.kind === 'track' ? activeAudioRef.current : videoRef.current
    if (!element) return
    // hls.js runs its own recovery (hlsErrorHandler).
    if (element === videoRef.current && hlsRef.current) return
    if (userInitiated || retry.trackId !== current.id) {
      mediaRetryRef.current = { trackId: current.id, attempts: 0, timer: undefined, resumedAt: null }
    }
    // The quality this track plays at now: an adaptive downgrade is kept, and
    // an upgrade queued for the next track stays queued (see streamBitrate).
    const src = api.streamUrl(current.id, current.kind === 'track' ? streamBitrate : undefined)
    const lastGood = lastGoodPositionRef.current
    const position = lastGood?.trackId === current.id
      ? lastGood.seconds
      : Number.isFinite(element.currentTime) ? element.currentTime : 0
    mediaRetryRef.current.resumedAt = position
    appMediaChangeUntilRef.current = Date.now() + 1000
    noteElementItem(element, current)
    if (element === videoRef.current) {
      setElementSrc(element, src)
    } else {
      const failedBlob = element.src.startsWith('blob:') ? element.src : null
      elementSrcRef.current.set(element, src)
      element.dataset.streamSrc = src
      element.src = src
      if (failedBlob) {
        for (const [stream, blob] of trackBlobsRef.current) {
          if (blob !== failedBlob) continue
          trackBlobsRef.current.delete(stream)
          URL.revokeObjectURL(blob)
        }
      }
    }
    element.load()
    try {
      element.currentTime = position
    } catch {
      // Not seekable before metadata on some engines: see below.
    }
    // Engines differ on keeping a position set before the new source's
    // metadata is in; seek again once it is (as for a quality switch).
    const reloaded = element
    reloaded.addEventListener('loadedmetadata', () => {
      if (elementItemsRef.current.get(reloaded)?.id !== current.id) return
      if (Math.abs(reloaded.currentTime - position) > 0.25) reloaded.currentTime = position
    }, { once: true })
    logPlaybackDiagnostic('media_reload', {
      device_id: deviceIdRef.current,
      media_item_id: current.id,
      attempt: mediaRetryRef.current.attempts,
      user_initiated: userInitiated,
      position: Number(position.toFixed(3)),
    })
    // Only if it is meant to be playing: the user may have paused meanwhile.
    if (userInitiated || playingRef.current) element.play().catch(() => {})
  }

  // A new track gets a full retry budget; a reload waiting for the old one is
  // cancelled.
  const currentId = current?.id ?? null
  useEffect(() => {
    window.clearTimeout(mediaRetryRef.current.timer)
    mediaRetryRef.current = { trackId: currentId, attempts: 0, timer: undefined, resumedAt: null }
  }, [currentId])

  useEffect(() => () => {
    window.clearTimeout(radioRetryRef.current.timer)
    window.clearTimeout(radioRetryRef.current.stallTimer)
  }, [])

  // The connection coming back: do not sit out the rest of the wait.
  useEffect(() => {
    const onOnline = () => {
      if (mediaRetryRef.current.timer != null) reloadMediaRef.current(false)
    }
    window.addEventListener('online', onOnline)
    return () => {
      window.removeEventListener('online', onOnline)
      window.clearTimeout(mediaRetryRef.current.timer)
    }
  }, [])

  // Remembers how far the playing element got with its own track while it
  // was healthy, for a reload to resume from, and gives the track its retry
  // budget back once it has played on for a while after a reload.
  function noteMediaProgress(element: HTMLMediaElement) {
    if (!current || element.error || element.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return
    if (elementItemsRef.current.get(element)?.id !== current.id || !Number.isFinite(element.currentTime)) return
    lastGoodPositionRef.current = { trackId: current.id, seconds: element.currentTime }
    const retry = mediaRetryRef.current
    if (
      retry.trackId === current.id && retry.attempts > 0 && retry.timer == null &&
      retry.resumedAt != null && element.currentTime > retry.resumedAt + MEDIA_RETRY_RESET_AFTER_SECONDS
    ) {
      mediaRetryRef.current = { ...retry, attempts: 0, resumedAt: null }
    }
  }

  const skipNext = useCallback(() => {
    // The queue picks what comes next: not a track the user chose.
    listenStartRef.current = listenStartHint('auto')
    if (snapshot?.is_playing && isActiveDevice) {
      const element = isVideo ? videoRef.current : activeAudioRef.current
      if (element?.paused) retryLocalPlaybackFromGesture()
    }
    send({ type: 'next' })
  }, [isActiveDevice, isVideo, retryLocalPlaybackFromGesture, send, snapshot?.is_playing])
  // Going back to a track is choosing it.
  const skipPrev = useCallback(() => {
    listenStartRef.current = listenStartHint('user')
    send({ type: 'prev' })
  }, [send])

  const seekTo = useCallback(
    (seconds: number) => {
      const el = isVideo ? videoRef.current : activeAudioRef.current
      if (isActiveDevice && el) el.currentTime = seconds
      // As a remote the position shown is the active device's: restart the
      // local clock from the requested spot until its broadcast confirms.
      else remoteClockRef.current = { ...remoteClockRef.current, seconds, at: Date.now() }
      timeStoreRef.current.setCurrentTime(seconds)
      // Named for the item being scrubbed - this element's on the active
      // device, the shown (current) one on a remote - so a seek that reaches
      // the server after it moved on (held while offline) is ignored there
      // instead of landing on another track. An item the server is not on
      // any more is not sent at all (see messageQueueItemIdFor).
      const mediaId = isActiveDevice && el ? elementMediaId(el) : currentMediaIdRef.current
      const queueItemId = messageQueueItemIdFor(mediaId)
      if (queueItemId == null) {
        logPlaybackDiagnostic('seek_not_sent_item_not_current', {
          device_id: deviceIdRef.current,
          media_item_id: mediaId,
          server_media_item_id: lastServerStateRef.current?.media_item_id ?? null,
        })
        return
      }
      send({ type: 'seek', position_seconds: seconds, queue_item_id: queueItemId })
    },
    [send, isActiveDevice, isVideo],
  )

  // Patches one track's heart state everywhere this context keeps a copy
  // of it: the media cache, the current track and the queue. Disliking
  // never skips or removes anything - it only changes the heart.
  const applyPreference = useCallback((id: number, state: PreferenceState) => {
    const patch = (item: MediaItem): MediaItem => ({
      ...item,
      is_favorite: state === 'liked',
      is_disliked: state === 'disliked',
    })
    const cached = mediaCacheRef.current.get(id)
    if (cached) mediaCacheRef.current.set(id, patch(cached))
    setCurrent((prev) => (prev && prev.id === id ? patch(prev) : prev))
    setQueue((prev) => prev.map((item) => (item.id === id ? patch(item) : item)))
    setListedQueue((prev) => prev.map((item) => (item.id === id ? patch(item) : item)))
  }, [])

  const cycleCurrentPreference = useCallback(
    async (next: PreferenceState) => {
      if (!current) return
      const { id, title } = current
      const previous: PreferenceState = current.is_disliked
        ? 'disliked'
        : current.is_favorite
          ? 'liked'
          : 'none'
      applyPreference(id, next)
      try {
        await api.setTrackPreference(id, next)
      } catch (err) {
        applyPreference(id, previous)
        showToast(`Couldn't update "${title}".`, 'error')
        throw err
      }
      publishPreferenceChange(id, next)
    },
    [current, applyPreference, showToast],
  )

  // A track liked/disliked from a track list (or anywhere else) should
  // update the Now Playing heart and the queue instantly too, without
  // waiting for this device's own next state refresh.
  useEffect(() => subscribePreferenceChange(applyPreference), [applyPreference])

  const transferTo = useCallback((deviceId: number) => {
    // A transfer selected in this browser is itself a user gesture. Prime
    // playback now so the browser doesn't reject the later play() issued
    // after the server's asynchronous active-device broadcast.
    if (deviceId === deviceIdRef.current && !isActiveDevice && snapshot?.is_playing) {
      stopRadio()
      retryLocalPlaybackFromGesture(true)
    }
    send({ type: 'transfer_to', device_id: deviceId })
  }, [isActiveDevice, retryLocalPlaybackFromGesture, send, snapshot?.is_playing, stopRadio])

  const toggleShuffle = useCallback(
    () => send({ type: 'set_shuffle', enabled: !snapshot?.is_shuffled }),
    [send, snapshot?.is_shuffled],
  )
  const setShuffle = useCallback(
    (enabled: boolean) => send({ type: 'set_shuffle', enabled }),
    [send],
  )
  const cycleRepeat = useCallback(() => {
    const order = ['off', 'all', 'one'] as const
    const current = snapshot?.repeat_mode ?? 'off'
    const next = order[(order.indexOf(current) + 1) % order.length]
    send({ type: 'set_repeat', mode: next })
  }, [send, snapshot?.repeat_mode])
  // Returns the track playback moved to (the same id for a restart) and its
  // queue item (null if the server's queue does not line up with `queue` at
  // that index), or null when it did not move.
  const advanceLocalTrackAfterEnd = useCallback((element: HTMLMediaElement): { id: number; queueItemId: number | null } | null => {
    appMediaChangeUntilRef.current = Date.now() + 1000
    if (!current || current.kind !== 'track' || queue.length === 0) return null
    const currentIndex = queue.findIndex((item) => item.id === current.id)
    if (currentIndex < 0) return null
    const queueIndex = snapshot?.queue_index != null && queue[snapshot.queue_index]?.id === current.id
      ? snapshot.queue_index
      : currentIndex
    const repeatMode = snapshot?.repeat_mode ?? 'off'
    // Repeat-all wraps to the first row only when `queue` is the whole queue:
    // while a long queue is still being looked up, or a part of it could not
    // be (see resolveQueue), its first row is not the queue's first and its
    // last may not be the last - the server's answer to track_ended decides.
    const nextIndex = repeatMode === 'one'
      ? queueIndex
      : queueIndex + 1 < queue.length
        ? queueIndex + 1
        : repeatMode === 'all' && queueLookupCompleteRef.current ? 0 : -1
    if (nextIndex < 0) return null
    const next = queue[nextIndex]
    if (!next) return null
    // `queue` holds only the items that resolved, so it can be shorter than
    // the server's lists: trust the index only where they agree.
    const queueItemId = snapshot?.queue_media_item_ids[nextIndex] === next.id
      ? snapshot.queue_item_ids[nextIndex] ?? null
      : null
    if (next.id === current.id) {
      element.currentTime = 0
      element.play().catch(() => {})
      return { id: current.id, queueItemId }
    }
    // This page is producing the sound but cannot tell the server (see
    // outputsLocally): the sync effect will not move the elements meanwhile.
    const socketDown = !isActiveDeviceRef.current && outputsLocally()
    let handedOff = false
    // Visible page with the next track already buffered in the standby
    // element: hand over here too, in the `ended` call itself. Leaving the
    // promotion to the playback-sync effect would put a React render between
    // one track ending and the next starting - an audible gap on albums meant
    // to run together. The effect then finds `activeAudioRef` and `src`
    // already matching and neither reloads nor seeks. A standby that isn't
    // ready yet is left for the effect (or, with the
    // socket down, for the synchronous move below). In the Android app the
    // hand-off is tried hidden too: the standby is a native player, which
    // throttled page timers cannot stall, and the native side may already
    // have started it on its own (an armed standby, see armNativeNext) -
    // loading the next track into the ended element as well would play it
    // twice.
    if (next.kind === 'track' && (document.visibilityState === 'visible' || isNativeApp())) {
      const audio = audioRef.current
      const preloadAudio = preloadAudioRef.current
      const standby = element === audio ? preloadAudio : element === preloadAudio ? audio : null
      if (
        standby &&
        elementHasSrc(standby, api.streamUrl(next.id, nextTrackBitrate)) &&
        standby.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA
      ) {
        handedOff = true
        activeAudioRef.current = standby
        standby.play().catch((error: unknown) => {
          logPlaybackDiagnostic('gapless_handoff_play_rejected', {
            device_id: deviceIdRef.current,
            media_item_id: next.id,
            error_name: error instanceof Error ? error.name : 'unknown',
            error_message: error instanceof Error ? error.message : String(error),
          })
        })
        element.pause()
      }
    }
    // A backgrounded page (screen locked) is the one case where the element
    // must be moved *synchronously*, in this same call stack as the native
    // `ended` event. The state updates below still run, and the playback-sync
    // effect then sees `element.src` already matching and skips reloading, so
    // a hidden advance and a visible advance converge on the same end state.
    // While hidden the effect deliberately keeps the element the OS
    // lock-screen notification was bound to (see the element-promotion
    // comment there); leaving the swap to the effect would mean it happens
    // only after React re-renders, and Chrome throttles a hidden tab's timers
    // and tasks progressively the longer it stays hidden - by the second or
    // third transition that deferred play() is rejected and nothing retries
    // it. The same goes for a visible page whose socket is down (no network):
    // `isActiveDevice` needs the socket, so the sync effect leaves the
    // elements alone until the reconnect, and without this move the page would
    // go quiet at the end of the track it was playing - unless the standby was
    // buffered for the hand-off above, and then only for one track, as the
    // standby is not refilled while the socket is down. Do not make this run
    // unconditionally: the visible path promotes the preloaded standby
    // element for a gapless manual skip, and taking it over would throw that
    // buffer away - so the hand-off goes first.
    if (next.kind === 'track' && !handedOff && (document.visibilityState === 'hidden' || socketDown)) {
      // Prefer the in-memory copy of the next track (see prefetchTrackBlob):
      // re-requesting the stream here needs the network at the one moment a
      // locked phone may not have it, and a request that never completes
      // leaves this element silent and costs the page its media notification.
      const nextSrc = api.streamUrl(next.id, nextTrackBitrate)
      noteElementItem(element, next)
      setElementSrc(element, nextSrc)
      element.load()
      element.play().catch((error: unknown) => {
        logPlaybackDiagnostic('local_advance_play_rejected', {
          device_id: deviceIdRef.current,
          media_item_id: next.id,
          error_name: error instanceof Error ? error.name : 'unknown',
          error_message: error instanceof Error ? error.message : String(error),
        })
      })
    }
    // The hand-offs above pick the next track's source at `nextTrackBitrate`
    // (what was prefetched), but a track change otherwise resets an adaptive
    // downgrade, so the sync effect would resolve a different URL, reload the
    // element and restart the track that had just begun. Carry the downgrade
    // across so both agree.
    if (next.kind === 'track' && settings.adaptiveBitrateEnabled && nextTrackBitrate !== settings.maxBitrateKbps) {
      setAdaptiveQuality({ trackId: next.id, quality: nextTrackBitrate })
    }
    cacheItems([next])
    setCurrent(next)
    timeStoreRef.current.setCurrentTime(0)
    timeStoreRef.current.setDuration(next.duration_seconds ?? 0)
    return { id: next.id, queueItemId }
  }, [current, elementHasSrc, noteElementItem, outputsLocally, queue, setElementSrc, settings.adaptiveBitrateEnabled, settings.maxBitrateKbps, snapshot?.queue_index, snapshot?.queue_item_ids, snapshot?.queue_media_item_ids, snapshot?.repeat_mode, nextTrackBitrate])
  const handleTrackEnded = useCallback((element: HTMLMediaElement) => {
    const diagnostic: PlaybackEndDiagnostic = {
      media_item_id: current?.id ?? null,
      current_time: Number.isFinite(element.currentTime) ? Number(element.currentTime.toFixed(3)) : null,
      duration: Number.isFinite(element.duration) ? Number(element.duration.toFixed(3)) : null,
      paused: element.paused,
      ready_state: element.readyState,
      visibility: document.visibilityState === 'hidden' ? 'hidden' : 'visible',
      is_active_device: isActiveDevice,
    }
    // The queue item that ended, read before this end replaces the previous
    // pending one: when two ends come in a row before the server answers, the
    // element's item is the one the previous local advance moved to.
    const endedMediaId = elementMediaId(element)
    const endedQueueItemId = messageQueueItemIdFor(endedMediaId)
    if (endedQueueItemId == null) {
      // The element was playing something the server is no longer on (or
      // not on yet): its end is stale and track_ended, which must name the
      // row that ended, is not sent - it would skip whatever the server is
      // playing instead. No local advance either (it would follow this
      // page's outdated queue). Follow the server's last state instead, as
      // for its answer to a stale track_ended: `current` moves to the
      // server's item and the sync effect loads and plays it there.
      const serverState = lastServerStateRef.current
      logPlaybackDiagnostic('track_ended_not_sent_item_not_current', {
        ...diagnostic,
        device_id: deviceIdRef.current,
        element_media_item_id: endedMediaId,
        server_media_item_id: serverState?.media_item_id ?? null,
      })
      if (serverState) {
        setSnapshot(serverState)
        if (serverState.media_item_id == null) setCurrent(null)
        else {
          resolveIds([serverState.media_item_id])
            .then(([resolved]) => {
              if (lastServerStateRef.current === serverState) setCurrent(resolved ?? null)
            })
            .catch(() => {
              // The next state broadcast retries, as for any snapshot.
            })
        }
      }
      return
    }
    // An earlier end the server has not been seen past yet stays in the
    // chain: replacing it would lose it, and a phone that played on through
    // two track ends offline would go back to the first track on the reconnect.
    const previousEnd = pendingTrackEndedRef.current != null ? pendingEndMetaRef.current : null
    const earlierEndedQueueItemIds = previousEnd?.endedQueueItemId != null && previousEnd.endedQueueItemId !== endedQueueItemId
      ? [...previousEnd.earlierEndedQueueItemIds, previousEnd.endedQueueItemId].slice(-MAX_CHAINED_ENDS)
      : []
    pendingTrackEndedRef.current = diagnostic
    pendingEndMetaRef.current = {
      sentGeneration: null,
      at: Date.now(),
      advancedToId: null,
      endedQueueItemId,
      advancedToQueueItemId: null,
      earlierEndedQueueItemIds,
    }
    logPlaybackDiagnostic('media_ended_event', {
      ...diagnostic,
      device_id: deviceIdRef.current,
      websocket_ready_state: wsRef.current?.readyState ?? WebSocket.CLOSED,
      socket_registered: playbackSocketRegisteredRef.current,
    })
    const ws = wsRef.current
    // The media pipeline must not wait for a mobile-suspended WebSocket to
    // advance a locally-known queue. Reconcile the server state in parallel
    // through the track-ended message below.
    const advanced = advanceLocalTrackAfterEnd(element)
    pendingEndMetaRef.current.advancedToId = advanced?.id ?? null
    pendingEndMetaRef.current.advancedToQueueItemId = advanced?.queueItemId ?? null
    if (playbackSocketRegisteredRef.current && ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'client_diagnostic', event: 'media_ended', ...diagnostic }))
      ws.send(JSON.stringify(trackEndedMessage()))
      pendingEndMetaRef.current.sentGeneration = connectionGenerationRef.current
      logPlaybackDiagnostic('track_ended_sent', {
        device_id: deviceIdRef.current,
        media_item_id: diagnostic.media_item_id,
        queue_item_id: endedQueueItemId,
      })
    } else {
      logPlaybackDiagnostic('track_ended_waiting_for_socket', {
        device_id: deviceIdRef.current,
        media_item_id: diagnostic.media_item_id,
        websocket_ready_state: ws?.readyState ?? WebSocket.CLOSED,
      })
    }
  }, [advanceLocalTrackAfterEnd, current?.id, isActiveDevice, resolveIds])
  const jumpToQueueIndex = useCallback(
    (index: number) => {
      // Normally a remote control for whichever device is playing. With no
      // connected active device anywhere, claim playback so it isn't silent.
      const activeId = snapshot?.active_device_id ?? null
      const activeOnline = snapshot?.devices.some((d) => d.id === activeId && d.is_connected) ?? false
      if (!activeOnline && deviceIdRef.current != null) send({ type: 'transfer_to', device_id: deviceIdRef.current })
      listenStartRef.current = listenStartHint('user', lastServerStateRef.current?.queue_media_item_ids[index] ?? null)
      send({ type: 'jump_to_index', index })
    },
    [send, snapshot?.active_device_id, snapshot?.devices],
  )
  const removeFromQueue = useCallback(
    (queueItemId: number) => send({ type: 'remove_from_queue', queue_item_id: queueItemId }),
    [send],
  )
  const moveQueueItem = useCallback(
    (queueItemId: number, direction: -1 | 1) =>
      send({ type: 'move_queue_item', queue_item_id: queueItemId, direction }),
    [send],
  )
  const reorderQueueItem = useCallback(
    (queueItemId: number, targetIndex: number) =>
      send({ type: 'reorder_queue_item', queue_item_id: queueItemId, target_index: targetIndex }),
    [send],
  )
  const removeDevice = useCallback(
    (deviceId: number) => send({ type: 'remove_device', device_id: deviceId }),
    [send],
  )

  const playRadioStation = useCallback(
    (station: RadioStation) => {
      // Only pause the *local* track sound if this device is the one
      // actually producing it - other devices' playback is unaffected.
      if (isActiveDevice && snapshot?.is_playing) send(pauseMessage())
      radioResumeRequestedRef.current = true
      // The station already selected: the load effect will not run again,
      // so reconnect it here - choosing it again is how a user retries one
      // that failed or went quiet.
      if (radioStation?.id === station.id) {
        window.clearTimeout(radioRetryRef.current.timer)
        radioRetryRef.current = { ...radioRetryRef.current, attempts: 0, timer: undefined, wanted: true }
        reloadRadio(station)
        return
      }
      setRadioStation(station)
    },
    [isActiveDevice, pauseMessage, radioStation?.id, reloadRadio, snapshot?.is_playing, send],
  )

  // Read through a ref by the stable wrappers in `actions`; refreshed on
  // every render so a command always runs the current implementation.
  const actionImpls: PlayerActions = {
    setVolume,
    openNowPlaying,
    closeNowPlaying,
    playQueue,
    playQueueIds,
    playLibraryContext,
    playNext,
    addToQueueEnd,
    playNextMany,
    addManyToQueueEnd,
    togglePlayPause,
    skipNext,
    skipPrev,
    seekTo,
    cycleCurrentPreference,
    transferTo,
    toggleShuffle,
    setShuffle,
    cycleRepeat,
    jumpToQueueIndex,
    removeFromQueue,
    moveQueueItem,
    reorderQueueItem,
    removeDevice,
    playRadioStation,
    toggleRadioPlayPause,
    stopRadio,
    setAccountAccentColor,
    updateAudioSettings,
  }
  const actionImplsRef = useRef(actionImpls)
  actionImplsRef.current = actionImpls
  const actions = useMemo(
    () =>
      Object.fromEntries(
        PLAYER_ACTION_KEYS.map((key) => [
          key,
          (...args: unknown[]) => (actionImplsRef.current[key] as (...a: unknown[]) => unknown)(...args),
        ]),
      ) as unknown as PlayerActions,
    [],
  )

  // Built from the fields it exposes, not from `snapshot`: a broadcast that
  // only moves the position (the active device's five-second save) changes
  // none of them, so it re-renders no consumer.
  // Every snapshot carries new arrays; these keep the previous ones (by
  // content) so an unchanged queue or device list is not a change.
  const queueItemIdsKey = (snapshot?.queue_item_ids ?? EMPTY_IDS).join(',')
  const queueItemIds = useMemo(
    () => (queueItemIdsKey ? queueItemIdsKey.split(',').map(Number) : EMPTY_IDS),
    [queueItemIdsKey],
  )
  const devicesKey = JSON.stringify(snapshot?.devices ?? EMPTY_DEVICES)
  const devices = useMemo(() => JSON.parse(devicesKey) as PlaybackDevice[], [devicesKey])
  const currentIndex = snapshot?.queue_index ?? 0
  const isShuffled = snapshot?.is_shuffled ?? false
  const repeatMode = snapshot?.repeat_mode ?? 'off'
  const queueContextCount = snapshot?.queue_context_count ?? 0
  // Every snapshot is a new object: keyed on its text so the value only
  // changes when the origin does.
  const queueOriginKey = JSON.stringify(snapshot?.queue_origin ?? null)
  const queueOrigin = useMemo(() => JSON.parse(queueOriginKey) as PlaySource | null, [queueOriginKey])
  const deviceId = deviceIdRef.current
  const value = useMemo<PlayerContextValue>(
    () => ({
      ...actions,
      queue: listedQueue,
      queueItemIds,
      currentIndex,
      isPlaying: effectiveIsPlaying,
      mediaLoading,
      isShuffled,
      repeatMode,
      queueContextCount,
      queueOrigin,
      current,
      volume,
      devices,
      deviceId,
      isActiveDevice,
      connected,
      reconnecting,
      adaptiveQualityNotice,
      audioRef,
      videoRef,
      nowPlayingOpen,
      radioStation,
      isRadioPlaying,
      radioNowPlaying,
      audioSettings,
    }),
    [
      actions,
      listedQueue,
      queueItemIds,
      currentIndex,
      effectiveIsPlaying,
      mediaLoading,
      isShuffled,
      repeatMode,
      queueContextCount,
      queueOrigin,
      current,
      volume,
      devices,
      deviceId,
      isActiveDevice,
      connected,
      reconnecting,
      adaptiveQualityNotice,
      nowPlayingOpen,
      radioStation,
      isRadioPlaying,
      radioNowPlaying,
      audioSettings,
    ],
  )

  // Published before paint, so a component reading one field through
  // usePlayerSelector never shows an older value than one reading the context.
  const [valueStore] = useState(() => createPlayerValueStore(value))
  useLayoutEffect(() => valueStore.set(value), [value, valueStore])

  // A media element firing `error` (a 404, a network failure, a codec it
  // can't decode) is surfaced, so the track/video does not just sit stuck
  // instead of ever reaching "playing", with nothing telling the user why.
  // Only the active device's own element error is worth
  // surfacing - a background/inactive element never gets a real `src`.
  function noteExternalTransition(event: 'pause' | 'play', element: HTMLMediaElement) {
    if (Date.now() < appMediaChangeUntilRef.current) return
    // Also while the socket is down: a pause made then (headphones unplugged
    // on a locked phone) must not be lost, or the reconnect's snapshot, still
    // saying playing, would make the sync effect play it again from the
    // speaker. `send` holds it until the socket is back.
    if (!outputsLocally()) return
    // A video whose sound has moved to the background element is ours.
    if (element === videoRef.current && videoBackgroundedRef.current) return
    // Debounced: a swap or seek can pause and resume within a few ms.
    window.setTimeout(() => {
      if (Date.now() < appMediaChangeUntilRef.current || !outputsLocally()) return
      const live = videoRef.current === element ? element : activeAudioRef.current
      if (live !== element) return
      if (event === 'pause') {
        // `ended` also pauses; that has its own track_ended path.
        if (!element.paused || element.ended || element.error || element.readyState === 0) return
        if (!playingRef.current) return
        logPlaybackDiagnostic('external_pause_detected', { device_id: deviceIdRef.current })
        // Where it stopped travels with the pause, also when the pause is
        // held for the socket (see flushOfflineCommands).
        send({
          type: 'pause',
          position_seconds: Number.isFinite(element.currentTime) ? element.currentTime : null,
        }, { external: true })
      } else {
        if (element.paused || playingRef.current) return
        logPlaybackDiagnostic('external_play_detected', { device_id: deviceIdRef.current })
        send({ type: 'play' }, { external: true })
      }
    }, 250)
  }

  function handlePlaybackMediaEvent(event: string, element: HTMLMediaElement) {
    if (element !== activeAudioRef.current && element !== videoRef.current) return
    if (event === 'play' || event === 'loadstart') audioEffects.resume()
    if (event === 'pause' || event === 'play') noteExternalTransition(event, element)
    if (event === 'loadstart' || event === 'waiting') {
      setMediaLoading(true)
      setPlaybackStarting(true)
    } else if (event === 'emptied' && element.getAttribute('src')) {
      // load() on an element that had a source: the start of the next load,
      // not the end of one. Releasing the hold here would let the next track's
      // download in ahead of the track being started.
    } else if (event === 'timeupdate' && element.readyState < 3) {
      // load() also resets the clock to 0, which fires this with nothing to play yet.
    } else if (['playing', 'canplay', 'canplaythrough', 'pause', 'error', 'emptied', 'ended'].includes(event)) {
      setMediaLoading(false)
      setPlaybackStarting(false)
    } else if (['loadeddata', 'timeupdate', 'stalled', 'suspend'].includes(event)) {
      // Data arriving or the clock advancing means it is no longer starting;
      // `stalled`/`suspend` mean the browser stopped fetching, so holding art
      // and the next-track prefetch back would only wait on a stream that is
      // not coming. The loading spinner is left to canplay/playing/waiting.
      setPlaybackStarting(false)
    }
    // Fires every ~250 ms: it only releases the hold, it is not worth a log line.
    if (event === 'timeupdate') return
    logPlaybackDiagnostic(`media_${event}`, {
      device_id: deviceIdRef.current,
      media_item_id: current?.id ?? null,
      is_active_device: isActiveDevice,
      paused: element.paused,
      current_time: Number.isFinite(element.currentTime) ? Number(element.currentTime.toFixed(3)) : null,
      duration: Number.isFinite(element.duration) ? Number(element.duration.toFixed(3)) : null,
      ready_state: element.readyState,
      network_state: element.networkState,
      visibility: document.visibilityState,
      error_code: element.error?.code ?? null,
    })
  }

  function handlePlaybackMediaError(element: HTMLMediaElement) {
    handlePlaybackMediaEvent('error', element)
    const buffered = element.buffered
    reportMediaError({
      source: element === videoRef.current ? 'video_element' : 'audio_element',
      media_item_id: current?.id ?? null,
      error_code: element.error?.code ?? null,
      current_time: Number.isFinite(element.currentTime) ? Number(element.currentTime.toFixed(3)) : null,
      buffered_end: buffered.length ? Number(buffered.end(buffered.length - 1).toFixed(3)) : null,
      ready_state: element.readyState,
      network_state: element.networkState,
    })
    if (!current || !outputsLocally()) return
    // With no network at all (a phone that moved on to a next track it had
    // no copy of), a stream that cannot be fetched is reported by Chromium as
    // "not supported": it is the network, not the file.
    const offline = navigator.onLine === false
    const errorCode = element.error?.code ?? null
    const code = offline && errorCode != null && errorCode !== MEDIA_ERR_ABORTED ? MEDIA_ERR_NETWORK : errorCode
    if (code == null) {
      // A plain `error` event with no MediaError: hls.js giving up on a
      // video after its own recovery (loadVideoSource's `fail`).
      noteListenFailed()
      showToast(`Couldn't play "${current.title}" - the file may be missing or unsupported.`, 'error')
      return
    }
    // hls.js handles errors of the video it is feeding (hlsErrorHandler).
    if (element === videoRef.current && hlsRef.current) return
    // The fetch was aborted - by the app replacing the source.
    if (code === MEDIA_ERR_ABORTED) return
    if (code !== MEDIA_ERR_NETWORK && code !== MEDIA_ERR_DECODE) {
      // "Not supported". That is what a file the browser cannot play reads
      // as, and a missing one (404) - but also a stream that could not be
      // fetched at all while the phone still calls itself online: a lift on
      // mobile data, Wi-Fi with no internet behind it, the proxy answering
      // 502 while the server restarts. Treating those as a broken file would
      // leave the queue stuck on "playing" with a message nobody sees on a
      // locked phone. So ask the stream once what it answers now.
      const failed = current
      const streamUrl = elementSrcRef.current.get(element) ?? element.currentSrc
      const unplayable = () => {
        // Another attempt cannot help. The server is left as it was.
        noteListenFailed()
        showToast(`"${failed.title}" can't be played (missing or unsupported file).`, 'error')
      }
      if (!streamUrl || streamUrl.startsWith('blob:')) {
        unplayable() // a copy already in memory: nothing was fetched
        return
      }
      if (mediaStatusCheckRef.current === failed.id) return // already asking
      mediaStatusCheckRef.current = failed.id
      void trackStreamStatus(streamUrl).then((status) => {
        if (mediaStatusCheckRef.current === failed.id) mediaStatusCheckRef.current = null
        if (currentMediaIdRef.current !== failed.id) return // the queue moved on while asking
        logPlaybackDiagnostic('media_error_stream_status', {
          device_id: deviceIdRef.current,
          media_item_id: failed.id,
          status,
        })
        if (!streamFailureIsTransient(status)) {
          unplayable()
          return
        }
        // Not if it is playing again by now (the user pressed Play, which
        // reloads a failed element): a reload would interrupt it.
        const playingAgain =
          element.error == null && !element.paused && element.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA
        if (!playingAgain) retryPlaybackMedia(failed, MEDIA_ERR_NETWORK, errorCode, false)
      })
      return
    }
    retryPlaybackMedia(current, code, errorCode, offline)
  }

  // A dropped connection or a corrupt stretch: reload it, a few times,
  // waiting longer each time (MEDIA_RETRY_DELAYS_MS).
  function retryPlaybackMedia(current: MediaItem, code: number, errorCode: number | null, offline: boolean) {
    let retry = mediaRetryRef.current
    if (retry.trackId !== current.id) {
      window.clearTimeout(retry.timer)
      retry = { trackId: current.id, attempts: 0, timer: undefined, resumedAt: null }
      mediaRetryRef.current = retry
    }
    // The same failure reported twice, or already given up on.
    if (retry.timer != null || retry.attempts > MEDIA_RETRY_DELAYS_MS.length) return
    if (offline) {
      // Nothing to count against the track while there is no network: wait
      // for it (the `online` event reloads at once), and look again now and
      // then in case that event never comes. Giving up here would pause
      // playback for good, so the track would not resume when the network
      // came back.
      retry.timer = window.setTimeout(() => {
        mediaRetryRef.current.timer = undefined
        reloadMediaRef.current(false)
      }, MEDIA_RETRY_DELAYS_MS[MEDIA_RETRY_DELAYS_MS.length - 1])
      logPlaybackDiagnostic('media_retry_waiting_for_network', {
        device_id: deviceIdRef.current,
        media_item_id: current.id,
        error_code: errorCode,
      })
      return
    }
    if (retry.attempts < MEDIA_RETRY_DELAYS_MS.length) {
      const delay = MEDIA_RETRY_DELAYS_MS[retry.attempts]
      retry.attempts += 1
      retry.timer = window.setTimeout(() => {
        mediaRetryRef.current.timer = undefined
        reloadMediaRef.current(false)
      }, delay)
      logPlaybackDiagnostic('media_retry_scheduled', {
        device_id: deviceIdRef.current,
        media_item_id: current.id,
        error_code: code,
        attempt: retry.attempts,
        delay_ms: delay,
      })
      return
    }
    // Out of retries: say why once, and stop claiming to play - here and on
    // the other devices. No skipping on: with the network gone that would
    // only burn through the queue.
    retry.attempts += 1
    noteListenFailed()
    showToast(code === MEDIA_ERR_NETWORK
      ? `Lost connection while playing "${current.title}".`
      : `"${current.title}" can't be played (missing or unsupported file).`, 'error')
    setLocalPlayback('pause')
    send(pauseMessage())
  }

  return (
    <PlayerTimeContext value={timeStore}>
    <PlayerActionsContext value={actions}>
    <PlayerContext value={value}>
    <PlayerValueStoreContext value={valueStore}>
    <NowPlayingOpenContext value={nowPlayingOpen}>
      {children}
      <audio
        ref={audioRef}
        data-player-element="main"
        preload="auto"
        onLoadStart={(e) => handlePlaybackMediaEvent('loadstart', e.currentTarget)}
        onTimeUpdate={(e) => {
          handlePlaybackMediaEvent('timeupdate', e.currentTarget)
          if (!isVideo && e.currentTarget === activeAudioRef.current) noteMediaProgress(e.currentTarget)
          noteListening(e.currentTarget)
          if (isActiveDevice && !isVideo && e.currentTarget === activeAudioRef.current) timeStoreRef.current.setCurrentTime(e.currentTarget.currentTime)
        }}
        onLoadedMetadata={(e) => {
          handlePlaybackMediaEvent('loadedmetadata', e.currentTarget)
          if (isActiveDevice && !isVideo && e.currentTarget === activeAudioRef.current && Number.isFinite(e.currentTarget.duration)) {
            timeStoreRef.current.setDuration(e.currentTarget.duration)
          }
        }}
        onDurationChange={(e) => {
          if (isActiveDevice && !isVideo && e.currentTarget === activeAudioRef.current && Number.isFinite(e.currentTarget.duration)) {
            timeStoreRef.current.setDuration(e.currentTarget.duration)
          }
        }}
        onCanPlay={(e) => handlePlaybackMediaEvent('canplay', e.currentTarget)}
        onPlay={(e) => handlePlaybackMediaEvent('play', e.currentTarget)}
        onPlaying={(e) => handlePlaybackMediaEvent('playing', e.currentTarget)}
        onPause={(e) => handlePlaybackMediaEvent('pause', e.currentTarget)}
        onWaiting={(e) => handlePlaybackMediaEvent('waiting', e.currentTarget)}
        onStalled={(e) => handlePlaybackMediaEvent('stalled', e.currentTarget)}
        onLoadedData={(e) => handlePlaybackMediaEvent('loadeddata', e.currentTarget)}
        onCanPlayThrough={(e) => handlePlaybackMediaEvent('canplaythrough', e.currentTarget)}
        onSuspend={(e) => handlePlaybackMediaEvent('suspend', e.currentTarget)}
        onEmptied={(e) => handlePlaybackMediaEvent('emptied', e.currentTarget)}
        onEnded={(e) => {
          if (e.currentTarget === activeAudioRef.current) {
            noteListenEnded(e.currentTarget)
            handleTrackEnded(e.currentTarget)
          } else logPlaybackDiagnostic('inactive_audio_ended_ignored', {
            device_id: deviceIdRef.current,
            active_device_id: snapshot?.active_device_id ?? null,
            media_item_id: current?.id ?? null,
          })
        }}
        onError={(e) => e.currentTarget === activeAudioRef.current && handlePlaybackMediaError(e.currentTarget)}
      />
      <audio
        ref={preloadAudioRef}
        data-player-element="standby"
        preload="auto"
        aria-hidden="true"
        tabIndex={-1}
        onLoadStart={(e) => handlePlaybackMediaEvent('loadstart', e.currentTarget)}
        onTimeUpdate={(e) => {
          handlePlaybackMediaEvent('timeupdate', e.currentTarget)
          if (!isVideo && e.currentTarget === activeAudioRef.current) noteMediaProgress(e.currentTarget)
          noteListening(e.currentTarget)
          if (isActiveDevice && !isVideo && e.currentTarget === activeAudioRef.current) timeStoreRef.current.setCurrentTime(e.currentTarget.currentTime)
        }}
        onLoadedMetadata={(e) => {
          handlePlaybackMediaEvent('loadedmetadata', e.currentTarget)
          if (isActiveDevice && !isVideo && e.currentTarget === activeAudioRef.current && Number.isFinite(e.currentTarget.duration)) {
            timeStoreRef.current.setDuration(e.currentTarget.duration)
          }
        }}
        onDurationChange={(e) => {
          if (isActiveDevice && !isVideo && e.currentTarget === activeAudioRef.current && Number.isFinite(e.currentTarget.duration)) {
            timeStoreRef.current.setDuration(e.currentTarget.duration)
          }
        }}
        onCanPlay={(e) => handlePlaybackMediaEvent('canplay', e.currentTarget)}
        onPlay={(e) => handlePlaybackMediaEvent('play', e.currentTarget)}
        onPlaying={(e) => handlePlaybackMediaEvent('playing', e.currentTarget)}
        onPause={(e) => handlePlaybackMediaEvent('pause', e.currentTarget)}
        onWaiting={(e) => handlePlaybackMediaEvent('waiting', e.currentTarget)}
        onStalled={(e) => handlePlaybackMediaEvent('stalled', e.currentTarget)}
        onLoadedData={(e) => handlePlaybackMediaEvent('loadeddata', e.currentTarget)}
        onCanPlayThrough={(e) => handlePlaybackMediaEvent('canplaythrough', e.currentTarget)}
        onSuspend={(e) => handlePlaybackMediaEvent('suspend', e.currentTarget)}
        onEmptied={(e) => handlePlaybackMediaEvent('emptied', e.currentTarget)}
        onEnded={(e) => {
          if (e.currentTarget === activeAudioRef.current) {
            noteListenEnded(e.currentTarget)
            handleTrackEnded(e.currentTarget)
          } else logPlaybackDiagnostic('inactive_audio_ended_ignored', {
            device_id: deviceIdRef.current,
            active_device_id: snapshot?.active_device_id ?? null,
            media_item_id: current?.id ?? null,
          })
        }}
        onError={(e) => e.currentTarget === activeAudioRef.current && handlePlaybackMediaError(e.currentTarget)}
      />
      {/* Always mounted (not just while Now Playing is open) so a video
          keeps playing in the background, audio-only, the same way a track
          does - positioned off-screen by default via CSS, and repositioned
          on top of NowPlayingView's video placeholder while that's open and
          showing a video (see NowPlayingView's placeholder-sync effect). */}
      <video
        ref={videoRef}
        data-player-element="video"
        className="nowplaying-video-el"
        playsInline
        onTimeUpdate={(e) => {
          handlePlaybackMediaEvent('timeupdate', e.currentTarget)
          if (isVideo) noteMediaProgress(e.currentTarget)
          noteListening(e.currentTarget)
          if (isActiveDevice && isVideo) timeStoreRef.current.setCurrentTime(e.currentTarget.currentTime)
        }}
        onLoadedMetadata={(e) => {
          handlePlaybackMediaEvent('loadedmetadata', e.currentTarget)
          if (isActiveDevice && isVideo && Number.isFinite(e.currentTarget.duration)) {
            timeStoreRef.current.setDuration(e.currentTarget.duration)
          }
        }}
        onDurationChange={(e) => {
          if (isActiveDevice && isVideo && Number.isFinite(e.currentTarget.duration)) {
            timeStoreRef.current.setDuration(e.currentTarget.duration)
          }
        }}
        onLoadStart={(e) => handlePlaybackMediaEvent('loadstart', e.currentTarget)}
        onCanPlay={(e) => handlePlaybackMediaEvent('canplay', e.currentTarget)}
        onPlay={(e) => handlePlaybackMediaEvent('play', e.currentTarget)}
        onPlaying={(e) => handlePlaybackMediaEvent('playing', e.currentTarget)}
        onPause={(e) => handlePlaybackMediaEvent('pause', e.currentTarget)}
        onWaiting={(e) => handlePlaybackMediaEvent('waiting', e.currentTarget)}
        onStalled={(e) => handlePlaybackMediaEvent('stalled', e.currentTarget)}
        onLoadedData={(e) => handlePlaybackMediaEvent('loadeddata', e.currentTarget)}
        onCanPlayThrough={(e) => handlePlaybackMediaEvent('canplaythrough', e.currentTarget)}
        onSuspend={(e) => handlePlaybackMediaEvent('suspend', e.currentTarget)}
        onEmptied={(e) => handlePlaybackMediaEvent('emptied', e.currentTarget)}
        onEnded={(e) => {
          noteListenEnded(e.currentTarget)
          handleTrackEnded(e.currentTarget)
        }}
        onError={(e) => handlePlaybackMediaError(e.currentTarget)}
      />
      <audio
        ref={bgAudioRef}
        aria-hidden="true"
        onTimeUpdate={(e) => {
          if (isActiveDevice && isVideo) timeStoreRef.current.setCurrentTime(e.currentTarget.currentTime)
          noteListening(e.currentTarget)
        }}
        onEnded={(e) => {
          noteListenEnded(e.currentTarget)
          handleTrackEnded(e.currentTarget)
        }}
      />
      <audio
        ref={radioAudioRef}
        onPlay={() => setIsRadioPlaying(true)}
        onPlaying={() => noteRadioProgress(true)}
        onTimeUpdate={() => noteRadioProgress(false)}
        onPause={() => setIsRadioPlaying(false)}
        onWaiting={noteRadioWaiting}
        onStalled={noteRadioWaiting}
        onEnded={() => handleRadioFailure('ended')}
        onError={() => handleRadioFailure('error')}
      />
      {KEEP_MEDIA_SESSION_ALIVE && (
        <audio ref={keepAliveAudioRef} loop aria-hidden="true" tabIndex={-1} data-keepalive="true" />
      )}
    </NowPlayingOpenContext>
    </PlayerValueStoreContext>
    </PlayerContext>
    </PlayerActionsContext>
    </PlayerTimeContext>
  )
}

/** The player's state and commands, without the position (see
 * usePlayerTime). Re-renders when the track, play state, queue, devices or
 * settings change - never on a position tick. */
/** Dispatched on window by App's navigation to close Now Playing without a
 * history pop (its entry is replaced by the new page instead). */
export const CLOSE_NOW_PLAYING_EVENT = 'atriolyric:close-now-playing'

/** Now Playing's address: its history entry (opened over the page under it)
 * and the Now playing tab. */
export const NOW_PLAYING_HASH = '#/now-playing'

/** True when the current history entry is Now Playing's. */
export function isNowPlayingEntry(): boolean {
  return window.history.state?.nowPlaying === true || window.location.hash.startsWith(NOW_PLAYING_HASH)
}

export function usePlayer(): PlayerContextValue {
  const ctx = useContext(PlayerContext)
  if (!ctx) throw new Error('usePlayer must be used within PlayerProvider')
  return ctx
}

/** Whether Now Playing is open; re-renders the caller only when that changes. */
export function useNowPlayingOpen(): boolean {
  return useContext(NowPlayingOpenContext)
}

/** Only the player's commands. Never re-renders the caller. */
export function usePlayerActions(): PlayerActions {
  const ctx = useContext(PlayerActionsContext)
  if (!ctx) throw new Error('usePlayerActions must be used within PlayerProvider')
  return ctx
}

/** The playback position and duration; re-renders the caller on every tick. */
export function usePlayerTime(): PlayerTime {
  const store = useContext(PlayerTimeContext)
  if (!store) throw new Error('usePlayerTime must be used within PlayerProvider')
  return useSyncExternalStore(store.subscribe, store.get)
}
