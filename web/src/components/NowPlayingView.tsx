import { useCallback, useEffect, useEffectEvent, useId, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { useIsWide } from '../useMediaQuery'
import { NOW_PLAYING_PAGE_HOST_ID, nowPlayingAsPage } from './nowPlayingLazy'
import { api } from '../api'
import { errorMessage } from '../errorMessage'
import { useToast } from '../ToastContext'
import { ArtScrollRootProvider } from '../artScrollRoot'
import { useAuth } from '../AuthContext'
import { formatDuration } from '../formatDuration'
import type { MenuAnchor } from '../menuTrigger'
import { usePlayer, usePlayerActions, usePlayerTime } from '../PlayerContext'
import { usePlayerTimeGetter } from '../player/timeStore'
import { useRenderProbe } from '../useRenderProbe'
import type { Playlist, PlaySource } from '../types'
import { Art } from './Art'
import { RadioIcon } from './BrowseIcons'
import {
  ArrowLeftIcon,
  LyricsIcon,
  MusicNoteIcon,
  PauseIcon,
  PlayIcon,
  RepeatIcon,
  RotateIcon,
  ShuffleIcon,
  SkipBack30Icon,
  SkipForward30Icon,
  SkipNextIcon,
  SkipPrevIcon,
  SpeakerIcon,
  StopIcon,
} from './Icons'
import { LyricsInline, LyricsPanel } from './LyricsPanel'
import { LabelsButton } from './LabelsButton'
import { AlbumLink, LibraryLinks } from './LibraryLinks'
import { MarqueeText } from './MarqueeText'
import { Modal } from './Modal'
import { trackPreference } from '../preference'
import { PreferenceHeart } from './PreferenceHeart'
import { QueueList, QueuePreviousToggle } from './QueueList'
import { useQueueCountLabel } from '../queueCount'
import { SeekTooltip } from './SeekPreview'
import { seekKeyTarget, useSeekPreview } from '../useSeekPreview'
import { TagEditModal } from './TagEditModal'
import { TrackMenuPanel } from './TrackMenuPanel'
import { mediaRef } from '../artEntity'
import { useHasLyrics } from '../useHasLyrics'
import { useIsDesktop } from '../useIsDesktop'
import { MoreButton } from './MoreMenu'
import { listenStatsLine } from '../infoText'
import { askUntilSettled } from '../usePendingAsync'
import { notifyWantListChanged } from '../wantList'
import { useLongPress } from '../useLongPress'

const LYRICS_VIEW_KEY = 'atriolyric.nowplaying.lyrics'

function loadLyricsView(): boolean {
  try {
    return localStorage.getItem(LYRICS_VIEW_KEY) === '1'
  } catch {
    return false
  }
}
// How long the video controls stay up while playing before fading out.
const CONTROLS_HIDE_MS = 10_000
// After a double-tap action (seek or play/pause) taps and pointer movement
// must not bring the controls up for this long - the user is probably about to
// double-tap again, and a stray extra tap shouldn't pop the controls over the
// picture between or after the jumps.
const TAP_GRACE_MS = 900
// Two separate touches within this window are a double tap; anything
// arriving sooner than the minimum gap is a ghost event, not a second tap.
const DOUBLE_TAP_MS = 300
const MIN_TAP_GAP_MS = 80
// Pointer movement smaller than this is jitter, not a request for controls.
const MOVE_REVEAL_PX = 6
const MOVE_REVEAL_WINDOW_MS = 1000
const MOVE_SUPPRESS_MS = 600

function splitArtistTitle(nowPlaying: string): { artist: string; title: string } {
  const sep = nowPlaying.indexOf(' - ')
  if (sep === -1) return { artist: 'Unknown artist', title: nowPlaying }
  return { artist: nowPlaying.slice(0, sep), title: nowPlaying.slice(sep + 3) }
}

// Per song for the session: skipping back and forth shouldn't refetch, and
// a failed lookup is remembered as "nothing to show". Only a settled answer
// is kept: while the server is still fetching the song's numbers (`pending`)
// they are asked for again (askUntilSettled) as long as the song is showing.
const statsLineCache = new Map<number, string | null>()

/** "1.2M plays · 340K listeners on ListenBrainz" under the artist line;
 * renders nothing until (and unless) ListenBrainz has numbers for the song. */
function ListenStatsLine({ mediaItemId }: { mediaItemId: number }) {
  const [line, setLine] = useState<string | null>(() => statsLineCache.get(mediaItemId) ?? null)
  useEffect(() => {
    if (statsLineCache.has(mediaItemId)) return
    const controller = new AbortController()
    askUntilSettled(
      (signal) => api.trackInfo(mediaItemId, false, { signal }),
      controller.signal,
      (info) => setLine(listenStatsLine(info.playcount, info.listeners)),
    ).then(
      (info) => {
        if (!info.pending) statsLineCache.set(mediaItemId, listenStatsLine(info.playcount, info.listeners))
      },
      () => {
        if (!controller.signal.aborted) statsLineCache.set(mediaItemId, null)
      },
    )
    return () => controller.abort()
  }, [mediaItemId])
  return line ? <div className="nowplaying-stats">{line}</div> : null
}

/**
 * Now Playing as a page (desktop and tablet): drawn into the host over the
 * app's <main>, so the sidebar, the top bar and the side panel stay. Not a
 * dialog - nothing behind it is inert - but it keeps the sheet's ways out:
 * its Back button, the browser's Back (the history entry) and Escape.
 */
function NowPlayingPage({
  onClose,
  className,
  contentRef,
  children,
}: {
  onClose: () => void
  className: string
  contentRef: RefObject<HTMLDivElement | null>
  children: ReactNode
}) {
  const host = document.getElementById(NOW_PLAYING_PAGE_HOST_ID)
  const close = useEffectEvent(() => onClose())
  useEffect(() => {
    contentRef.current?.focus({ preventScroll: true })
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key !== 'Escape' || event.defaultPrevented) return
      // A dialog or menu opened from the page takes Escape for itself.
      if (document.querySelector('dialog[open], [aria-modal="true"], [role="menu"], [role="listbox"]')) return
      close()
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [contentRef])
  if (!host) return null
  return createPortal(
    <div className="nowplaying-page">
      <div ref={contentRef} className={className} role="region" aria-label="Now playing" tabIndex={-1}>
        {children}
      </div>
    </div>,
    host,
  )
}

/** Now Playing's "Playing from" line: what kind of thing the queue was
 * started from, and its name. A Songs-tab start names its filter. */
function describeQueueOrigin(source: PlaySource): { kind: string; name: string } {
  if (source.kind === 'playlist') {
    const autoKind = source.payload.auto_kind
    return { kind: autoKind === 'mix' ? 'Mix' : 'Playlist', name: source.label }
  }
  if (source.kind === 'library') {
    const filters = (source.payload.filters ?? {}) as { q?: string; genre?: string; tag?: string }
    const filter = filters.genre ?? filters.tag ?? (filters.q ? `"${filters.q}"` : null)
    return { kind: 'Songs', name: filter ? `Songs · ${filter}` : 'All Songs' }
  }
  const kinds: Record<string, string> = { album: 'Album', artist: 'Artist', mix: 'Mix', radio: 'Radio' }
  return { kind: kinds[source.kind] ?? 'Queue', name: source.label }
}

/** Now Playing's seek row: the elapsed time, the slider and the length. The
 * only part of the view that shows the position, so the only part a position
 * tick (about four a second) re-renders - like the player bar's ProgressSeek. */
function NowPlayingSeek() {
  const { seekTo } = usePlayerActions()
  const { currentTime, duration } = usePlayerTime()
  const seekProgressPct = duration > 0 ? Math.min(100, (currentTime / duration) * 100) : 0
  const {
    trackRef: seekTrackRef,
    tooltipRef: seekTooltipRef,
    preview: seekPreview,
    setPreview: setSeekPreview,
    offset: seekTooltipOffset,
    valueAt: seekValueAt,
  } = useSeekPreview(duration)

  function showSeekPreview(clientX: number) {
    const next = seekValueAt(clientX)
    if (next) setSeekPreview(next)
  }

  // Same keyboard steps as the player bar's slider: 5 s, 30 s for the page keys.
  function handleSeekKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    const nextTime = seekKeyTarget(event.key, currentTime, duration)
    if (nextTime == null) return
    event.preventDefault()
    seekTo(nextTime)
  }

  // Hover shows the time for mice; touch shows it while a finger is down.
  const seekPreviewHandlers = {
    onPointerEnter: (e: React.PointerEvent) => {
      if (e.pointerType !== 'touch') showSeekPreview(e.clientX)
    },
    onPointerMove: (e: React.PointerEvent) => showSeekPreview(e.clientX),
    onPointerDown: (e: React.PointerEvent) => showSeekPreview(e.clientX),
    onPointerUp: (e: React.PointerEvent) => {
      if (e.pointerType === 'touch') setSeekPreview(null)
    },
    onPointerLeave: () => setSeekPreview(null),
    onPointerCancel: () => setSeekPreview(null),
  }

  return (
    <div className="nowplaying-seek">
      <span className="player-time">{formatDuration(currentTime)}</span>
      <div className="seek-track" ref={seekTrackRef} {...seekPreviewHandlers}>
        <div className="seek-track-bg">
          <div className="seek-fill" style={{ width: `${seekProgressPct}%` }} />
        </div>
        <input
          type="range"
          min={0}
          max={duration || 0}
          value={Math.min(currentTime, duration || 0)}
          onChange={(e) => seekTo(Number(e.target.value))}
          onKeyDown={handleSeekKeyDown}
          aria-label="Seek"
          aria-valuetext={`${formatDuration(seekPreview?.time ?? currentTime)} of ${formatDuration(duration)}`}
        />
        {seekPreview && (
          <SeekTooltip preview={seekPreview} offset={seekTooltipOffset} tooltipRef={seekTooltipRef} />
        )}
      </div>
      <span className="player-time">{formatDuration(duration)}</span>
    </div>
  )
}

export function NowPlayingView({
  onClose,
  playlists,
  onPlaylistsChanged,
  onOpenArtist,
  onOpenAlbum,
  onOpenLabel,
  onOpenPlaySource,
}: {
  onClose: () => void
  playlists: Playlist[]
  onPlaylistsChanged?: () => void
  onOpenArtist?: (artist: string) => void
  onOpenAlbum?: (artist: string | null, album: string) => void
  onOpenLabel?: (label: { genre?: string; tag?: string }) => void
  onOpenPlaySource?: (source: PlaySource) => void
}) {
  const { user } = useAuth()
  const {
    current,
    isPlaying,
    isShuffled,
    repeatMode,
    volume,
    setVolume,
    videoRef,
    togglePlayPause,
    skipNext,
    skipPrev,
    seekTo,
    cycleCurrentPreference,
    toggleShuffle,
    cycleRepeat,
    radioStation,
    isRadioPlaying,
    radioNowPlaying,
    toggleRadioPlayPause,
    stopRadio,
    adaptiveQualityNotice,
    queueOrigin,
  } = usePlayer()
  // The position is not read here: a tick would re-render this whole view
  // (and the queue under it) about four times a second. The seek row shows it
  // (NowPlayingSeek); the handlers that need it ask when they run.
  const getPlayerTime = usePlayerTimeGetter()
  useRenderProbe('nowplaying')
  const artMenuId = useId()
  const [showPrevious, setShowPrevious] = useState(false)
  const [artMenuOpen, setArtMenuOpen] = useState(false)
  const [artMenuAnchor, setArtMenuAnchor] = useState<MenuAnchor | null>(null)
  const [lyricsOpen, setLyricsOpen] = useState(false)
  // Radio's "+ Want list" while its request is out: a second tap must not
  // add the song twice.
  const [addingToWantList, setAddingToWantList] = useState(false)
  const { showToast } = useToast()
  const [lyricsInArt, setLyricsInArt] = useState(loadLyricsView)
  const [editingTagsOpen, setEditingTagsOpen] = useState(false)
  const hasLyrics = useHasLyrics(current?.id, current?.kind === 'track')
  const isDesktop = useIsDesktop()
  const isWide = useIsWide()
  const asPage = nowPlayingAsPage(isDesktop, current, radioStation != null)
  const nowPlayingContentRef = useRef<HTMLDivElement>(null)
  const sidePaneRef = useRef<HTMLDivElement>(null)
  const swipeStartRef = useRef<{ x: number; y: number } | null>(null)
  const videoPlaceholderRef = useRef<HTMLDivElement>(null)
  const isVideo = current?.kind !== 'track'
  // The lyrics toggle (desktop) and the Lyrics tab (phone) are offered only
  // for a song known to have lyrics. Shown, they take the lower half of the
  // left column on a desktop and the tab under the controls on a phone; the
  // choice is remembered either way (lyricsInArt).
  const lyricsAvailable = !isVideo && hasLyrics === true
  const lyricsShown = lyricsInArt && lyricsAvailable
  const queueCountLabel = useQueueCountLabel()
  const sideTabsId = useId()
  // Video gets a SNAP-style full-bleed layout: the video fills the screen (on
  // a desktop, the left column - Up next stays beside it), controls float
  // over a scrim and fade out while it plays, and taps on the picture act as
  // zones (see handleVideoTap).
  const videoMode = isVideo
  const [controlsHidden, setControlsHidden] = useState(false)
  const [isFullscreen, setIsFullscreen] = useState(false)
  const controlsTimer = useRef<number | null>(null)
  const tapTimer = useRef<number | null>(null)
  // Brief, translucent confirmation of a double-tap action over the picture.
  const [tapFlash, setTapFlash] = useState<{ kind: 'back' | 'forward' | 'play' | 'pause'; n: number } | null>(null)
  const flashTimer = useRef<number | null>(null)
  // Close this overlay (a history entry) first, then navigate once the pop
  // has settled, so Back from the destination doesn't land on the old page.
  const closeThen = (go: () => void) => {
    window.addEventListener('popstate', go, { once: true })
    onClose()
  }
  // Kept between renders: it is a prop of the memoised queue list.
  const openArtist = useMemo(
    () => onOpenArtist && ((artist: string) => {
      window.addEventListener('popstate', () => onOpenArtist(artist), { once: true })
      onClose()
    }),
    [onOpenArtist, onClose],
  )
  const volumeControl = (
    <label className="nowplaying-volume" title="Volume">
      <SpeakerIcon />
      <input
        type="range"
        min={0}
        max={1}
        step={0.01}
        value={volume}
        onChange={(e) => setVolume(Number(e.target.value))}
        aria-label="Volume"
        aria-valuetext={`${Math.round(volume * 100)}%`}
      />
    </label>
  )


  const lastTapActionAt = useRef(0)
  const controlsHiddenRef = useRef(false)
  const hiddenAtDownRef = useRef(false)
  const moveRevealAt = useRef(0)
  const moveSuppressUntil = useRef(0)
  const lastPointerPos = useRef<{ x: number; y: number } | null>(null)
  const originalOrientationRef = useRef<'portrait' | 'landscape' | null>(null)
  const touchSeqRef = useRef(0)
  const handledSeqRef = useRef(0)
  const lastTapAtRef = useRef(0)
  const withinTapGrace = () => Date.now() - lastTapActionAt.current < TAP_GRACE_MS

  // Hiding also cancels the pending auto-hide so no stale timer outlives it.
  const hideControls = useCallback(() => {
    controlsHiddenRef.current = true
    if (controlsTimer.current != null) window.clearTimeout(controlsTimer.current)
    controlsTimer.current = null
    setControlsHidden(true)
  }, [])

  const armHideTimer = useCallback(() => {
    if (controlsTimer.current != null) window.clearTimeout(controlsTimer.current)
    controlsTimer.current = null
    if (videoMode && isPlaying) {
      controlsTimer.current = window.setTimeout(hideControls, CONTROLS_HIDE_MS)
    }
  }, [videoMode, isPlaying, hideControls])

  const showControls = useCallback(() => {
    controlsHiddenRef.current = false
    setControlsHidden(false)
    armHideTimer()
  }, [armHideTimer])

  // Pointer movement (mouse hover, touch jitter) brings the controls up, but
  // only for a real movement, and it is remembered so the click that follows
  // is read against what the user saw BEFORE moving, not the revealed state.
  function handleOverlayPointerMove(e: React.PointerEvent) {
    if (withinTapGrace() || Date.now() < moveSuppressUntil.current) return
    const last = lastPointerPos.current
    lastPointerPos.current = { x: e.clientX, y: e.clientY }
    if (last && Math.hypot(e.clientX - last.x, e.clientY - last.y) < MOVE_REVEAL_PX) return
    if (controlsHiddenRef.current) moveRevealAt.current = Date.now()
    showControls()
  }

  // Controls stay up while paused and start their auto-hide countdown on play.
  // Right after a double-tap action (including the play/pause it may cause)
  // they are left as they were.
  useEffect(() => {
    if (withinTapGrace()) armHideTimer()
    else showControls()
    return () => {
      if (controlsTimer.current != null) window.clearTimeout(controlsTimer.current)
    }
  }, [armHideTimer, showControls])

  useEffect(() => () => {
    if (tapTimer.current != null) window.clearTimeout(tapTimer.current)
    if (flashTimer.current != null) window.clearTimeout(flashTimer.current)
  }, [])

  function flashAction(kind: 'back' | 'forward' | 'play' | 'pause') {
    setTapFlash((previous) => ({ kind, n: (previous?.n ?? 0) + 1 }))
    if (flashTimer.current != null) window.clearTimeout(flashTimer.current)
    flashTimer.current = window.setTimeout(() => setTapFlash(null), 650)
  }

  useEffect(() => {
    function onChange() {
      setIsFullscreen(!!document.fullscreenElement)
      // A rotate-button lock must not outlive full screen, or the app stays
      // stuck in one orientation afterwards. Restore the orientation the
      // device was in before the first rotate, then release the lock.
      if (!document.fullscreenElement) {
        const original = originalOrientationRef.current
        originalOrientationRef.current = null
        try {
          const current = screen.orientation.type.startsWith('landscape') ? 'landscape' : 'portrait'
          if (original && original !== current) {
            screen.orientation
              .lock(original)
              .catch(() => {})
              .finally(() => {
                try {
                  screen.orientation.unlock()
                } catch {
                  // Not supported - nothing was locked.
                }
              })
          } else {
            screen.orientation.unlock()
          }
        } catch {
          // Not supported (desktop, iOS) - nothing was locked.
        }
      }
    }
    document.addEventListener('fullscreenchange', onChange)
    return () => document.removeEventListener('fullscreenchange', onChange)
  }, [])

  // Single tap toggles the controls; double tap acts on the third of the
  // picture that was tapped: left -30s, middle play/pause, right +30s.
  function handleVideoPointerDown() {
    touchSeqRef.current += 1
    // A hover that just revealed the controls counts as "still hidden": the
    // tap that follows is the user's request to show them, not to hide again.
    hiddenAtDownRef.current =
      controlsHiddenRef.current || Date.now() - moveRevealAt.current < MOVE_REVEAL_WINDOW_MS
  }

  function handleVideoTap(e: React.MouseEvent<HTMLDivElement>) {
    // Ghost taps: a click that belongs to a touch we already handled, or that
    // lands almost on top of the previous tap, is not a new tap.
    const now = Date.now()
    if (touchSeqRef.current === handledSeqRef.current) return
    if (now - lastTapAtRef.current < MIN_TAP_GAP_MS) return
    handledSeqRef.current = touchSeqRef.current
    lastTapAtRef.current = now
    const rect = e.currentTarget.getBoundingClientRect()
    const third = rect.width / 3
    const x = e.clientX - rect.left
    if (tapTimer.current != null) {
      window.clearTimeout(tapTimer.current)
      tapTimer.current = null
      lastTapActionAt.current = Date.now()
      const { currentTime, duration } = getPlayerTime()
      if (x < third) {
        seekTo(Math.max(0, currentTime - 30))
        flashAction('back')
      } else if (x >= third * 2) {
        seekTo(Math.min(duration || currentTime + 30, currentTime + 30))
        flashAction('forward')
      } else {
        togglePlayPause()
        // Show what the tap did: playing now means it paused, and vice versa.
        flashAction(isPlaying ? 'pause' : 'play')
      }
    } else {
      tapTimer.current = window.setTimeout(() => {
        tapTimer.current = null
        // A lone tap during a run of double-taps (between jumps, or one too
        // many) is not a request for the controls.
        if (withinTapGrace()) return
        if (hiddenAtDownRef.current) showControls()
        else {
          hideControls()
          // Jitter right after hiding must not bring them straight back.
          moveSuppressUntil.current = Date.now() + MOVE_SUPPRESS_MS
        }
      }, DOUBLE_TAP_MS)
    }
  }

  function skipSeconds(delta: number) {
    const { currentTime, duration } = getPlayerTime()
    const target = currentTime + delta
    seekTo(Math.max(0, duration > 0 ? Math.min(duration, target) : target))
    showControls()
  }

  function toggleLyricsInArt() {
    setLyricsView(!lyricsInArt)
  }

  function setLyricsView(next: boolean) {
    setLyricsInArt(next)
    try {
      localStorage.setItem(LYRICS_VIEW_KEY, next ? '1' : '0')
    } catch {
      // Storage unavailable - the choice just won't be remembered.
    }
  }

  function toggleFullscreen() {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {})
    else document.documentElement.requestFullscreen().catch(() => {})
  }

  async function rotateScreen() {
    try {
      const landscape = screen.orientation.type.startsWith('landscape')
      // Remember the orientation we started from (only the first rotate of a
      // fullscreen session) so leaving full screen can put it back.
      if (originalOrientationRef.current == null) {
        originalOrientationRef.current = landscape ? 'landscape' : 'portrait'
      }
      // Orientation lock only works in full screen, so enter it first.
      if (!document.fullscreenElement) {
        await document.documentElement.requestFullscreen()
      }
      // 'landscape' / 'portrait' (not the *-primary variants) keep the
      // orientation locked to the axis but let the sensor pick the direction,
      // so turning the phone the other way up flips the video 180 degrees.
      await screen.orientation.lock(landscape ? 'portrait' : 'landscape')
    } catch {
      // Orientation lock is unsupported or refused (desktop, iOS) - ignore.
      if (!document.fullscreenElement) originalOrientationRef.current = null
    }
  }

  function handleOverlayFocus(e: React.FocusEvent<HTMLDivElement>) {
    let keyboard = false
    try {
      keyboard = (e.target as Element).matches(':focus-visible')
    } catch {
      // A browser without :focus-visible: treat every focus as keyboard focus.
      keyboard = true
    }
    if (keyboard && controlsHiddenRef.current) showControls()
  }

  function handleOverlayPointerDown(e: React.PointerEvent<HTMLDivElement>) {
    if (e.pointerType !== 'touch' || e.button !== 0) return
    swipeStartRef.current = { x: e.clientX, y: e.clientY }
  }

  function handleOverlayPointerUp(e: React.PointerEvent<HTMLDivElement>) {
    const start = swipeStartRef.current
    swipeStartRef.current = null
    if (!start || e.pointerType !== 'touch') return

    const deltaX = e.clientX - start.x
    const deltaY = e.clientY - start.y
    const content = nowPlayingContentRef.current
    const contentIsAtTop = !content || content.scrollTop <= 1
    if (contentIsAtTop && deltaY >= 72 && deltaY > Math.abs(deltaX) * 1.25) onClose()
  }

  // Right-click/long-press on the art (or video placeholder) opens the
  // same track menu a Songs-tab row offers - play-next/queue/generate/
  // add-to-playlist/edit-tags for whatever's currently playing.
  const artPress = useLongPress((_: void, point) => {
    setArtMenuAnchor({ x: point.x, y: point.y })
    setArtMenuOpen(true)
  })

  function handleArtContextMenu(e: React.MouseEvent) {
    e.preventDefault()
    setArtMenuAnchor({ x: e.clientX, y: e.clientY })
    setArtMenuOpen(true)
  }
  // The actual playing <video> element lives outside this component (in
  // PlayerProvider, so it survives navigation and keeps playing in the
  // background) - while this view is open and showing a video, sync its
  // fixed position/size onto this placeholder's spot so it visually reads
  // as if it were rendered right here.
  useEffect(() => {
    if (!isVideo || !current) return
    const video = videoRef.current
    const placeholder = videoPlaceholderRef.current
    if (!video || !placeholder) return
    function sync() {
      const rect = placeholder!.getBoundingClientRect()
      video!.style.borderRadius = videoMode ? '0' : ''
      // Full-bleed: slip under the overlay (z 100) so its controls draw on top.
      video!.style.zIndex = videoMode ? '99' : ''
      video!.style.top = `${rect.top}px`
      video!.style.left = `${rect.left}px`
      video!.style.width = `${rect.width}px`
      video!.style.height = `${rect.height}px`
    }
    sync()
    const observer = new ResizeObserver(sync)
    observer.observe(placeholder)
    window.addEventListener('scroll', sync, true)
    window.addEventListener('resize', sync)
    return () => {
      observer.disconnect()
      window.removeEventListener('scroll', sync, true)
      window.removeEventListener('resize', sync)
      video.style.top = ''
      video.style.left = ''
      video.style.width = ''
      video.style.height = ''
      video.style.borderRadius = ''
      video.style.zIndex = ''
    }
  }, [isVideo, videoMode, current, videoRef])

  const backButton = (
    <button className="nowplaying-close" onClick={onClose}>
      <ArrowLeftIcon aria-hidden="true" /> Back
    </button>
  )

  if (radioStation) {
    async function addToWantList() {
      if (!radioNowPlaying || addingToWantList) return
      const { artist, title } = splitArtistTitle(radioNowPlaying)
      setAddingToWantList(true)
      try {
        await api.addWantListItem(artist, title, radioStation!.name)
        notifyWantListChanged()
        // Nothing else on this screen shows that it worked.
        showToast(`Added "${title}" by ${artist} to your Want list.`, 'success')
      } catch (error) {
        showToast(errorMessage(error, `Could not add "${title}" to your Want list.`), 'error')
      } finally {
        setAddingToWantList(false)
      }
    }

    const radioBody = (
        <div className="nowplaying-layout nowplaying-layout-radio">
          <div className="nowplaying-main">
            {backButton}
            <div className="nowplaying-art-menu-wrap is-audio">
              <Art mediaItemId={null} className="art-nowplaying" fallback={<RadioIcon />} />
            </div>
            <div className="nowplaying-head">
              <div className="nowplaying-titles">
                <MarqueeText always className="nowplaying-title" text={radioStation.name} />
                <MarqueeText
                  always
                  className="nowplaying-subtitle"
                  text={radioNowPlaying ?? (radioStation.description || 'Live radio')}
                />
                <div className="nowplaying-album">
                  <span className="badge">Live radio</span>
                </div>
              </div>
            </div>
            <div className="nowplaying-ctl">
              <div className="nowplaying-controls">
                <div className="nowplaying-controls-main">
                  <button
                    className="nowplaying-play-btn"
                    onClick={toggleRadioPlayPause}
                    aria-label={isRadioPlaying ? 'Pause' : 'Play'}
                  >
                    {isRadioPlaying ? <PauseIcon /> : <PlayIcon />}
                  </button>
                </div>
              </div>
              <div className="nowplaying-radio-actions">
                <button onClick={() => void addToWantList()} disabled={!radioNowPlaying || addingToWantList}>
                  + Want list
                </button>
                <button
                  className="radio-stop-btn"
                  onClick={() => {
                    stopRadio()
                    onClose()
                  }}
                  aria-label="Stop"
                >
                  <StopIcon aria-hidden="true" /> Stop
                </button>
              </div>
              <div className="nowplaying-chips">{volumeControl}</div>
            </div>
          </div>
        </div>
    )
    if (asPage) {
      return (
        <NowPlayingPage onClose={onClose} className="nowplaying-content nowplaying-page-content" contentRef={nowPlayingContentRef}>
          {radioBody}
        </NowPlayingPage>
      )
    }
    return (
      <Modal
        variant="sheet"
        title={radioStation.name}
        onClose={onClose}
        backdropClassName="nowplaying-overlay"
        backdropProps={{
          onPointerDown: handleOverlayPointerDown,
          onPointerUp: handleOverlayPointerUp,
          onPointerCancel: () => {
            swipeStartRef.current = null
          },
        }}
        className="nowplaying-content"
        panelRef={nowPlayingContentRef}
        inertSelector=".app-shell"
      >
        {radioBody}
      </Modal>
    )
  }

  if (!current) {
    // Opened from the Now playing tab with nothing on.
    const emptyBody = (
      <div className="nowplaying-layout nowplaying-layout-radio">
        <div className="nowplaying-main">
          {backButton}
          <p className="nowplaying-empty">Nothing playing — pick a track to get started.</p>
        </div>
      </div>
    )
    if (asPage) {
      return (
        <NowPlayingPage onClose={onClose} className="nowplaying-content nowplaying-page-content" contentRef={nowPlayingContentRef}>
          {emptyBody}
        </NowPlayingPage>
      )
    }
    return (
      <Modal
        variant="sheet"
        title="Now playing"
        onClose={onClose}
        backdropClassName="nowplaying-overlay"
        className="nowplaying-content"
        panelRef={nowPlayingContentRef}
        inertSelector=".app-shell"
      >
        {emptyBody}
      </Modal>
    )
  }

  const phoneTab: 'queue' | 'lyrics' = lyricsShown ? 'lyrics' : 'queue'
  const lyricsInMain = isDesktop && lyricsShown
  // The volume stands upright beside the artwork (with desktop lyrics too). A
  // video fills that space, so then it goes in a column to the right of the
  // controls instead.
  const uprightVolume = !isVideo
  // Art in the queue loads as rows near the edges of the area that scrolls
  // them: the Up next column on a desktop, the whole sheet on a phone.
  const artScrollRoot = isDesktop ? sidePaneRef : nowPlayingContentRef
  const origin = queueOrigin && !isVideo ? describeQueueOrigin(queueOrigin) : null
  const originLink = origin && queueOrigin && (
    <button
      type="button"
      className="nowplaying-origin"
      disabled={!onOpenPlaySource}
      onClick={() => onOpenPlaySource && closeThen(() => onOpenPlaySource(queueOrigin))}
      title={onOpenPlaySource ? `Open ${origin.name}` : undefined}
    >
      <span className="nowplaying-origin-kind">Playing from {origin.kind.toLowerCase()}</span>{' '}
      <span className="nowplaying-origin-name">{origin.name}</span>
    </button>
  )

  const queue = (
    <QueueList
      onOpenArtist={openArtist}
      showPrevious={showPrevious}
      scrollRef={isDesktop ? sidePaneRef : nowPlayingContentRef}
      scrollToCurrent={isDesktop}
    />
  )
  const previousToggle = <QueuePreviousToggle open={showPrevious} onToggle={() => setShowPrevious((open) => !open)} />

  function handleTabKeyDown(e: React.KeyboardEvent<HTMLButtonElement>) {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
    e.preventDefault()
    const next = phoneTab === 'queue'
    setLyricsView(next)
    document.getElementById(`${sideTabsId}-${next ? 'lyrics' : 'queue'}-tab`)?.focus()
  }

  const body = (
    <>
      <ArtScrollRootProvider value={artScrollRoot}>
      {/* A wide page leaves Up next to the side panel beside it. */}
      <div
        className={`nowplaying-layout${lyricsInMain ? ' has-lyrics' : ''}${asPage && isWide ? ' no-side' : ''}`}
      >
        <div className="nowplaying-main">
          {backButton}

          {/* What the queue was started from: opens it. */}
          {originLink}

          {/* On a phone the volume runs upright beside the artwork. */}
          <div className={`nowplaying-art-row${uprightVolume ? ' has-volume' : ''}`}>
          <div
            className={`nowplaying-art-menu-wrap ${isVideo ? 'is-video' : 'is-audio'}`}
            onContextMenu={handleArtContextMenu}
            onPointerDown={(e) => artPress.start(e, undefined)}
            onPointerUp={artPress.cancel}
            onPointerLeave={artPress.cancel}
            onPointerCancel={artPress.cancel}
          >
            {isVideo ? (
              // Tap zones over the picture are a touch gesture (double-tap to
              // seek or pause); the same actions are buttons in the controls.
              // oxlint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions
              <div
                ref={videoPlaceholderRef}
                className="nowplaying-video-placeholder"
                onPointerDown={videoMode ? handleVideoPointerDown : undefined}
                onClick={videoMode ? handleVideoTap : undefined}
              />
            ) : (
              <Art mediaItemId={current.id} artRef={mediaRef(current)} className="art-nowplaying" fallback={<MusicNoteIcon />} full />
            )}
            {isVideo && videoMode && tapFlash && (
              <div
                key={tapFlash.n}
                className={`video-flash video-flash-${tapFlash.kind}`}
                aria-hidden="true"
                data-testid="video-flash"
              >
                <span className="video-flash-badge">
                  {tapFlash.kind === 'play' && <PlayIcon />}
                  {tapFlash.kind === 'pause' && <PauseIcon />}
                  {tapFlash.kind === 'back' && (
                    <SkipBack30Icon />
                  )}
                  {tapFlash.kind === 'forward' && (
                    <SkipForward30Icon />
                  )}
                </span>
              </div>
            )}
            {artMenuOpen && artMenuAnchor && (
              <TrackMenuPanel
                track={current}
                playlists={playlists}
                isAdmin={!!user?.is_admin}
                anchor={artMenuAnchor}
                menuId={artMenuId}
                alsoInside={['.nowplaying-art-menu-wrap']}
                onClose={() => setArtMenuOpen(false)}
                onViewLyrics={hasLyrics ? () => setLyricsOpen(true) : undefined}
                onEditTags={user?.is_admin ? () => setEditingTagsOpen(true) : undefined}
                onPlaylistsChanged={onPlaylistsChanged}
              />
            )}
          </div>
            {uprightVolume && <div className="nowplaying-volume-upright">{volumeControl}</div>}
          </div>

          <div className="nowplaying-head">
            <div className="nowplaying-titles">
              <MarqueeText always className="nowplaying-title" text={current.title} />
              <MarqueeText always className="nowplaying-subtitle">
                {current.kind === 'video' ? (
                  current.performer ?? 'Video'
                ) : (
                  <LibraryLinks artist={current.artist} artists={current.artists} onArtist={openArtist} />
                )}
              </MarqueeText>
              {current.kind !== 'video' && current.album && (
                <MarqueeText always className="nowplaying-album">
                  <AlbumLink
                    album={current.album}
                    albumOwner={current.album_artist ?? current.artist ?? null}
                    onAlbum={onOpenAlbum && ((artist, album) => closeThen(() => onOpenAlbum(artist, album)))}
                  />
                </MarqueeText>
              )}
            </div>
            <PreferenceHeart
              state={trackPreference(current)}
              onChange={cycleCurrentPreference}
              subject={current.title}
              className="nowplaying-toggle-btn nowplaying-heart"
              activeClassName="active"
            />
            <MoreButton
              label={current.title}
              className="nowplaying-more"
              open={artMenuOpen}
              menuId={artMenuId}
              onOpen={(a) => {
                setArtMenuAnchor(a)
                setArtMenuOpen(true)
              }}
            />
          </div>

          <div className="nowplaying-ctl">
            {adaptiveQualityNotice && (
              <div className="nowplaying-quality-notice" role="status">
                {adaptiveQualityNotice}
              </div>
            )}

            <NowPlayingSeek />

            {/* Transport, chips and the plays line, with the volume upright
                in a column to the right (an empty column of the same width on
                the left keeps play centred). On a phone the volume stands
                beside the artwork instead. */}
            <div className={`nowplaying-ctl-body${uprightVolume ? '' : ' has-side-volume'}`}>
              {!uprightVolume && <span className="nowplaying-ctl-spacer" aria-hidden="true" />}
              <div className="nowplaying-ctl-main">
                <div className="nowplaying-controls">
                  {videoMode && (
                    <div className="nowplaying-video-tools">
                      <button onClick={() => skipSeconds(-30)} aria-label="Back 30 seconds" title="Back 30 seconds">
                        <SkipBack30Icon width="1.5em" height="1.5em" />
                      </button>
                      <button onClick={rotateScreen} aria-label="Rotate screen" title="Rotate screen">
                        <RotateIcon width="1.3em" height="1.3em" />
                      </button>
                      <button
                        onClick={toggleFullscreen}
                        aria-label={isFullscreen ? 'Exit full screen' : 'Full screen'}
                        title={isFullscreen ? 'Exit full screen' : 'Full screen'}
                      >
                        <svg viewBox="0 0 24 24" width="1.3em" height="1.3em" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                          {isFullscreen
                            ? <><path d="M8 3v5H3"/><path d="M21 8h-5V3"/><path d="M3 16h5v5"/><path d="M16 21v-5h5"/></>
                            : <><path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/></>}
                        </svg>
                      </button>
                      <button onClick={() => skipSeconds(30)} aria-label="Forward 30 seconds" title="Forward 30 seconds">
                        <SkipForward30Icon width="1.5em" height="1.5em" />
                      </button>
                    </div>
                  )}
                  <div className="nowplaying-controls-main">
                    <button
                      className={`nowplaying-toggle-btn ${isShuffled ? 'active' : ''}`}
                      onClick={toggleShuffle}
                      aria-label={isShuffled ? 'Shuffle on' : 'Shuffle off'}
                      aria-pressed={isShuffled}
                    >
                      <ShuffleIcon />
                    </button>
                    <button onClick={skipPrev} aria-label="Previous">
                      <SkipPrevIcon />
                    </button>
                    <button
                      className="nowplaying-play-btn"
                      onClick={togglePlayPause}
                      aria-label={isPlaying ? 'Pause' : 'Play'}
                    >
                      {isPlaying ? <PauseIcon /> : <PlayIcon />}
                    </button>
                    <button onClick={skipNext} aria-label="Next">
                      <SkipNextIcon />
                    </button>
                    <button
                      className={`nowplaying-toggle-btn ${repeatMode !== 'off' ? 'active' : ''}`}
                      onClick={cycleRepeat}
                      aria-label={
                        repeatMode === 'off' ? 'Repeat off' : repeatMode === 'all' ? 'Repeat all' : 'Repeat one'
                      }
                      aria-pressed={repeatMode !== 'off'}
                    >
                      <RepeatIcon mode={repeatMode} />
                    </button>
                  </div>
                </div>

                <div className="nowplaying-chips">
                  {isDesktop && lyricsAvailable && (
                    <button
                      type="button"
                      className={`nowplaying-chip nowplaying-lyrics-toggle ${lyricsInArt ? 'active' : ''}`}
                      onClick={toggleLyricsInArt}
                      aria-pressed={lyricsInArt}
                    >
                      <LyricsIcon aria-hidden="true" /> Lyrics
                    </button>
                  )}
                  {current.kind !== 'video' && (
                    <div className="nowplaying-labels">
                      <LabelsButton
                        key={current.id}
                        title={`${current.title} · genres/tags`}
                        target={{ media_item_id: current.id }}
                        onPick={
                          onOpenLabel &&
                          ((label) => {
                            closeThen(() => onOpenLabel(label))
                          })
                        }
                      />
                    </div>
                  )}
                </div>

                {current.kind === 'track' && <ListenStatsLine key={current.id} mediaItemId={current.id} />}
              </div>
              {!uprightVolume && <div className="nowplaying-volume-side">{volumeControl}</div>}
            </div>
          </div>

          {lyricsInMain && (
            <div className="nowplaying-lyrics-pane">
              <LyricsInline track={current} />
            </div>
          )}
        </div>

        {/* Up next: a column of its own on a desktop (beside the video too),
            under the controls on a phone, where it shares a tab bar with the
            lyrics. A phone's full-bleed video has no room for it. */}
        {(isDesktop || !videoMode) && !(asPage && isWide) && (
          <section className="nowplaying-side" aria-label={isDesktop || !lyricsAvailable ? 'Up next' : undefined}>
            {isDesktop || !lyricsAvailable ? (
              <div className="nowplaying-side-head">
                <h3 className="nowplaying-side-title">Up next</h3>
                <span className="nowplaying-side-count">{queueCountLabel}</span>
                {previousToggle}
              </div>
            ) : (
              <div className="nowplaying-side-head nowplaying-tabs" role="tablist" aria-label="Up next and lyrics">
                {(['queue', 'lyrics'] as const).map((tab) => (
                  <button
                    key={tab}
                    type="button"
                    role="tab"
                    id={`${sideTabsId}-${tab}-tab`}
                    aria-selected={phoneTab === tab}
                    aria-controls={`${sideTabsId}-panel`}
                    tabIndex={phoneTab === tab ? 0 : -1}
                    className={`nowplaying-tab ${phoneTab === tab ? 'active' : ''}`}
                    onClick={() => setLyricsView(tab === 'lyrics')}
                    onKeyDown={handleTabKeyDown}
                  >
                    {tab === 'queue' ? 'Up next' : 'Lyrics'}
                  </button>
                ))}
                {phoneTab === 'queue' && <span className="nowplaying-side-count">{queueCountLabel}</span>}
              </div>
            )}
            <div
              className="nowplaying-side-pane"
              ref={sidePaneRef}
              {...(!isDesktop && lyricsAvailable
                ? { id: `${sideTabsId}-panel`, role: 'tabpanel', 'aria-labelledby': `${sideTabsId}-${phoneTab}-tab` }
                : {})}
            >
              {!isDesktop && lyricsAvailable && phoneTab === 'lyrics' ? (
                <div className="nowplaying-lyrics-tab">
                  <LyricsInline track={current} />
                </div>
              ) : (
                <>
                  {!isDesktop && <div className="nowplaying-side-previous">{previousToggle}</div>}
                  {queue}
                </>
              )}
            </div>
          </section>
        )}
      </div>
      </ArtScrollRootProvider>
      {lyricsOpen && <LyricsPanel track={current} onClose={() => setLyricsOpen(false)} onOpenArtist={onOpenArtist} />}
      {editingTagsOpen && (
        <TagEditModal
          items={[current]}
          onClose={() => setEditingTagsOpen(false)}
          onSaved={() => setEditingTagsOpen(false)}
        />
      )}
    </>
  )
  if (asPage) {
    return (
      <NowPlayingPage onClose={onClose} className="nowplaying-content nowplaying-page-content" contentRef={nowPlayingContentRef}>
        {body}
      </NowPlayingPage>
    )
  }
  return (
    <Modal
      variant="sheet"
      title={current.title}
      onClose={onClose}
      backdropClassName={`nowplaying-overlay${videoMode ? ' video-mode' : ''}${videoMode && controlsHidden ? ' controls-hidden' : ''}`}
      backdropProps={{
        onPointerMove: videoMode ? handleOverlayPointerMove : undefined,
        onPointerDown: handleOverlayPointerDown,
        onPointerUp: handleOverlayPointerUp,
        onPointerCancel: () => {
          swipeStartRef.current = null
        },
        // Hidden video controls are still in the tab order: reaching one with
        // the keyboard brings them back (a mouse press on one cannot happen
        // while they are hidden, and a tap's focus must not undo a tap).
        onFocus: videoMode ? handleOverlayFocus : undefined,
      }}
      className="nowplaying-content"
      panelRef={nowPlayingContentRef}
      inertSelector=".app-shell"
    >
      {body}
    </Modal>
  )
}
