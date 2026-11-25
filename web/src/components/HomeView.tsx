import { Fragment, Suspense, useEffect, useEffectEvent, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { api } from '../api'
import { HeaderActionsPortal, TopBarTitlePortal } from '../headerControlsPortal'
import { errorMessage } from '../errorMessage'
import { useToast } from '../ToastContext'
import { useIsDesktop } from '../useIsDesktop'
import { SEARCH_DEBOUNCE_MS } from '../useDebouncedValue'
import { usePlayerActions } from '../PlayerContext'
import type { ExploreTrack, LibrarySearchResult, MediaItem, MixPlaylist, PlaySourceEntry, Playlist, RadioStation } from '../types'
import { Art } from './Art'
import { ErrorState } from './ErrorState'
import { LibraryLinks } from './LibraryLinks'
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  CloseIcon,
  CustomiseIcon,
  DiscIcon,
  ExternalLinkIcon,
  HeartIcon,
  MicIcon,
  MusicNoteIcon,
  PauseIcon,
  PlayIcon,
  RefreshIcon,
  SearchIcon,
  ShuffleIcon,
  SparkleIcon,
} from './Icons'
import { RadioIcon } from './BrowseIcons'
import { useMediaQuery } from '../useMediaQuery'
import { MixCollage } from './MixCollage'
import { CollectionDownloadMark } from '../native/DownloadMark'
import { TrackList } from './TrackList'
import { TrackMenuPanel } from './TrackMenuPanel'
import { useLongPressMenu } from '../useLongPressMenu'
import { HelpTip } from './HelpTip'
import { useSettings } from '../SettingsContext'
import { type HomeSectionId, visibleHomeSections } from '../settings'
import { lazyView } from '../lazyView'
import type { Tab } from '../nav'

// Only shown on demand, so its code loads when first opened.
const HomeSectionsDialog = lazyView(() => import('./HomeSectionsEditor').then((m) => m.HomeSectionsDialog))
const SpecifyMixDialog = lazyView(() => import('./SpecifyMixDialog').then((m) => m.SpecifyMixDialog))

const MAX_HOME_ITEMS = 13
// Explore previews still being fetched by the server: ask again this often, this many times.
const EXPLORE_POLL_MS = 3000
const EXPLORE_POLL_MAX = 10

/** Specified mixes are the temporary extras the user asked for by name, so
 * they always stay visible even once the featured mixes have filled the rail:
 * only non-specified mixes count against the cap. The server sorts specified
 * mixes last, so filtering in order keeps the default layout intact. */
// Remembered horizontal offset per rail (module-level, so it survives Home
// unmounting while an album/mix/artist is open). Coming back restores it.
const railScrollLeft = new Map<string, number>()

// Mouse and trackpad users get arrows and click-and-drag on the rails; a
// touch screen already swipes them natively, so it gets neither.
const FINE_POINTER_QUERY = '(hover: hover) and (pointer: fine)'
// Past this many pixels a press on a rail is a drag, not a click on a card.
const DRAG_CLICK_THRESHOLD = 5

function RailScroll({ railKey, children }: { railKey: string; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  const restored = useRef(false)
  const finePointer = useMediaQuery(FINE_POINTER_QUERY)
  const [canPrev, setCanPrev] = useState(false)
  const [canNext, setCanNext] = useState(false)
  const [dragging, setDragging] = useState(false)
  const frame = useRef(0)
  const drag = useRef<{ pointerId: number; startX: number; startLeft: number; moved: boolean } | null>(null)
  // Set when a drag ends past the threshold: the click the browser fires
  // next must not open the card the drag started on.
  const swallowClick = useRef(false)

  // Kept in a ref (not an Effect Event) because the rAF callback and the
  // scroll handler call it too, not only the effect below.
  const updateEnds = useRef(() => {})
  useLayoutEffect(() => {
    updateEnds.current = () => {
      const el = ref.current
      if (!el || !finePointer) return
      setCanPrev(el.scrollLeft > 1)
      setCanNext(el.scrollLeft + el.clientWidth < el.scrollWidth - 1)
    }
  }, [finePointer])

  function scheduleUpdate() {
    if (frame.current) return
    frame.current = requestAnimationFrame(() => {
      frame.current = 0
      updateEnds.current()
    })
  }

  // Re-attempt after every render until the content is wide enough to reach
  // the saved offset (items may still be loading on first mount).
  useLayoutEffect(() => {
    const el = ref.current
    const saved = railScrollLeft.get(railKey) ?? 0
    if (!el || restored.current || saved <= 0) return
    el.scrollLeft = saved
    if (Math.abs(el.scrollLeft - saved) < 2) restored.current = true
  })

  // Which ends are off-screen changes with the rail's size and its content
  // (cards loading in), not only with scrolling.
  useEffect(() => {
    const el = ref.current
    if (!el || !finePointer) return
    updateEnds.current()
    const observer = new ResizeObserver(() => scheduleUpdate())
    observer.observe(el)
    for (const child of Array.from(el.children)) observer.observe(child)
    return () => {
      observer.disconnect()
      cancelAnimationFrame(frame.current)
      frame.current = 0
    }
  }, [finePointer, children])

  function scrollByPage(direction: 1 | -1) {
    const el = ref.current
    if (!el) return
    el.scrollBy({ left: direction * el.clientWidth * 0.85, behavior: 'smooth' })
  }

  function endDrag(e: React.PointerEvent<HTMLDivElement>) {
    const state = drag.current
    if (!state || state.pointerId !== e.pointerId) return
    drag.current = null
    setDragging(false)
    if (state.moved) swallowClick.current = true
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId)
  }

  const scroller = (
    <div
      ref={ref}
      className={`home-rail-scroll${dragging ? ' dragging' : ''}`}
      onScroll={(e) => {
        scheduleUpdate()
        const left = e.currentTarget.scrollLeft
        const saved = railScrollLeft.get(railKey) ?? 0
        // Ignore the clamped scroll events fired while content is still loading.
        if (!restored.current && saved > 0 && left < saved) return
        restored.current = true
        railScrollLeft.set(railKey, left)
      }}
      {...(finePointer
        ? {
            onPointerDown: (e: React.PointerEvent<HTMLDivElement>) => {
              if (e.pointerType !== 'mouse' || e.button !== 0) return
              swallowClick.current = false
              drag.current = { pointerId: e.pointerId, startX: e.clientX, startLeft: e.currentTarget.scrollLeft, moved: false }
            },
            onPointerMove: (e: React.PointerEvent<HTMLDivElement>) => {
              const state = drag.current
              if (!state || state.pointerId !== e.pointerId) return
              const dx = e.clientX - state.startX
              if (!state.moved) {
                if (Math.abs(dx) <= DRAG_CLICK_THRESHOLD) return
                // Only capture once it is a real drag, so a plain click still
                // lands on the card under the pointer.
                state.moved = true
                setDragging(true)
                e.currentTarget.setPointerCapture(e.pointerId)
              }
              e.currentTarget.scrollLeft = state.startLeft - dx
            },
            onPointerUp: endDrag,
            onPointerCancel: endDrag,
            onClickCapture: (e: React.MouseEvent<HTMLDivElement>) => {
              if (!swallowClick.current) return
              swallowClick.current = false
              e.preventDefault()
              e.stopPropagation()
            },
            // Artwork would otherwise start the browser's own image drag.
            onDragStart: (e: React.DragEvent<HTMLDivElement>) => e.preventDefault(),
          }
        : {})}
    >
      {children}
    </div>
  )

  if (!finePointer) return scroller
  return (
    <div className="home-rail-scroll-wrap">
      {scroller}
      {canPrev && (
        <button
          type="button"
          className="home-rail-arrow home-rail-arrow-prev"
          aria-label="Scroll back"
          tabIndex={-1}
          onClick={() => scrollByPage(-1)}
        >
          <ChevronLeftIcon aria-hidden="true" />
        </button>
      )}
      {canNext && (
        <button
          type="button"
          className="home-rail-arrow home-rail-arrow-next"
          aria-label="Scroll forward"
          tabIndex={-1}
          onClick={() => scrollByPage(1)}
        >
          <ChevronRightIcon aria-hidden="true" />
        </button>
      )}
    </div>
  )
}

/** The Discovery playlist that feeds "Recommended from your library": picked
 * by its auto kind, since Favourites, Song Mix and every genre mix are
 * auto-generated playlists too and can come first in the list. */
function findDiscoveryPlaylist(playlists: Playlist[]): Playlist | undefined {
  return playlists.find((playlist) => playlist.auto_kind === 'discovery')
}

function cappedMixes(mixes: MixPlaylist[], max: number): MixPlaylist[] {
  let featured = 0
  return mixes.filter((mix) => {
    if (mix.is_specified) return true
    if (featured >= max) return false
    featured += 1
    return true
  })
}

function playSourceIcon(kind: PlaySourceEntry['kind']): ReactNode {
  switch (kind) {
    case 'album':
      return <DiscIcon />
    case 'artist':
      return <MicIcon />
    case 'radio':
      return <RadioIcon />
    case 'library':
      return <ShuffleIcon />
    case 'playlist':
    default:
      return <MusicNoteIcon />
  }
}

const PLAY_SOURCE_KINDS: Record<string, string> = {
  album: 'Album',
  artist: 'Artist',
  radio: 'Radio',
  library: 'Songs',
  playlist: 'Playlist',
}

function trackSubtitle(item: MediaItem): string {
  if (item.artist && item.album) return `${item.artist} · ${item.album}`
  return item.artist || item.album || (item.kind === 'video' ? 'Video' : '')
}

function TrackRail({
  items,
  onPlay,
  playlists,
  onPlaylistsChanged,
  onOpenArtist,
  onOpenAlbum,
}: {
  items: MediaItem[]
  onPlay: (item: MediaItem, index: number) => void
  playlists: Playlist[]
  onPlaylistsChanged: () => void
  onOpenArtist: (artist: string) => void
  onOpenAlbum: (artist: string | null, album: string) => void
}) {
  const menu = useLongPressMenu<number>()
  return (
    <RailScroll railKey="tracks">
      {items.map((item, index) => (
        <div key={`${item.id}:${index}`} className="home-menu-item">
          {/* The play button is an overlay filling the card, with the artist
              and album links above it as its siblings (see .rail-card-play). */}
          <div
            className="rail-card"
            onContextMenu={(e) => menu.onContextMenu(e, item.id)}
            onPointerDown={(e) => menu.onPointerDown(e, item.id)}
            onPointerUp={menu.onPointerUp}
            onPointerLeave={menu.onPointerLeave}
            onPointerCancel={menu.onPointerCancel}
          >
            <button
              type="button"
              className="rail-card-play"
              aria-label={`Play ${item.title}`}
              onClick={() => { if (!menu.consumeClick()) onPlay(item, index) }}
            />
            <Art mediaItemId={item.id} className="art-tile rail-card-art" alt={item.title} />
            <span className="rail-card-title">{item.title}</span>
            <span className="rail-card-sub">
              {item.artist || item.album ? (
                <LibraryLinks artist={item.artist} artists={item.artists} albumArtist={item.album_artist} album={item.album} onArtist={onOpenArtist} onAlbum={onOpenAlbum} />
              ) : (
                trackSubtitle(item)
              )}
            </span>
          </div>
          {menu.openFor === item.id && menu.anchor && (
            <TrackMenuPanel
              track={item}
              playlists={playlists}
              isAdmin={false}
              anchor={menu.anchor}
              onClose={menu.close}
              onPlaylistsChanged={onPlaylistsChanged}
            />
          )}
        </div>
      ))}
    </RailScroll>
  )
}

function ExploreCard({
  track,
  isPlaying,
  onToggle,
}: {
  track: ExploreTrack
  isPlaying: boolean
  onToggle: () => void
}) {
  return (
    <div className="rail-card explore-card">
      <button
        className="explore-card-art art-tile art-wrap"
        onClick={onToggle}
        title={isPlaying ? 'Pause preview' : 'Play 30s preview'}
      >
        {track.artwork_url ? (
          <img src={track.artwork_url} alt="" className="art-img" width={250} height={250} loading="lazy" decoding="async" />
        ) : (
          <div className="art-fallback"><MusicNoteIcon /></div>
        )}
        <span className="explore-play-overlay">{isPlaying ? <PauseIcon /> : <PlayIcon />}</span>
      </button>
      <span className="rail-card-title">{track.title || track.artist}</span>
      <span className="rail-card-sub">{track.artist}</span>
      {track.view_url && (
        <a
          href={track.view_url}
          target="_blank"
          rel="noopener noreferrer"
          className="explore-card-badge"
        >
          Open on Deezer <ExternalLinkIcon aria-hidden="true" />
        </a>
      )}
    </div>
  )
}

function HomeRail({
  title,
  helpText,
  children,
  onRegenerate,
  regenerating,
  onSpecify,
  links,
}: {
  title: string
  helpText: string
  children: ReactNode
  /** Rails backed by a recommendation/mix algorithm (as opposed to plain
   * factual lists like "Recently played") can be regenerated on demand -
   * this renders a "Regenerate" link beside the title for that, rather
   * than only offering it buried in that rail's empty state. */
  onRegenerate?: () => void
  regenerating?: boolean
  onSpecify?: () => void
  /** More links on the right of the heading ("History", "All radio"). */
  links?: ReactNode
}) {
  return (
    <section className="home-rail" aria-label={title}>
      <div className="home-rail-header sec-head">
        <div className="home-rail-title">
          <h2 className="home-section-title">{title}</h2>
          <HelpTip
            label={`About ${title}`}
            text={helpText}
            className="home-rail-help"
            buttonClassName="home-rail-help-button"
            bubbleClassName="home-rail-help-bubble"
            align="start"
          />
        </div>
        {(onRegenerate || onSpecify || links) && (
          <div className="sec-head-links">
            {onRegenerate && (
              <button
                type="button"
                className={`text-link home-rail-regenerate ${regenerating ? 'spinning' : ''}`}
                onClick={onRegenerate}
                disabled={regenerating}
                aria-label={`Regenerate ${title}`}
              >
                <RefreshIcon aria-hidden="true" />
                Regenerate
              </button>
            )}
            {onSpecify && (
              <button type="button" className="text-link home-rail-specify" onClick={onSpecify}>
                Specify mix
              </button>
            )}
            {links}
          </div>
        )}
      </div>
      {children}
    </section>
  )
}

type SectionStatus = 'loading' | 'ready' | 'error'

/** A section still loading: its heading and a box of about its height, so
 * the sections below do not jump when it fills in. */
/** A section whose request failed: its heading, why, and a Retry for just
 * that section - never its "nothing yet" text, which would say the user has
 * played or saved nothing. */
function HomeSectionError({ id, error, onRetry }: { id: HomeSectionId; error: unknown; onRetry: () => void }) {
  const title = HOME_SECTION_TITLES[id]
  return (
    <section className="home-rail" aria-label={title}>
      <div className="home-rail-header sec-head">
        <div className="home-rail-title">
          <h2 className="home-section-title">{title}</h2>
        </div>
      </div>
      <ErrorState message={errorMessage(error, `Could not load ${title}.`)} onRetry={onRetry} />
    </section>
  )
}

function HomeSectionPlaceholder({ id }: { id: HomeSectionId }) {
  const title = HOME_SECTION_TITLES[id]
  return (
    <section className="home-rail" aria-label={title} aria-busy="true">
      <div className="home-rail-header sec-head">
        <div className="home-rail-title">
          <h2 className="home-section-title">{title}</h2>
        </div>
      </div>
      <div className={`home-rail-placeholder home-rail-placeholder-${id}`}>
        <span className="visually-hidden">Loading…</span>
      </div>
    </section>
  )
}

const HOME_SECTION_TITLES: Record<HomeSectionId, string> = {
  recent: 'Recently played',
  recommended: 'Recommended from your library',
  discover: 'Discover new artists',
  mixes: 'Mixes for you',
  radio: 'Your radio stations',
}

type SearchTab = 'artists' | 'albums' | 'songs' | 'playlists'

type SearchResults = { q: string; data: LibrarySearchResult | null; error?: string }

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`
}

/** What replaces the rails while the search box has text: one section per
 * kind of match, each hidden when empty. */
function HomeSearchResults({
  q,
  results,
  playlists,
  onRerun,
  onPlaylistsChanged,
  onOpenArtist,
  onOpenAlbum,
  onOpenMix,
  onViewFullSearch,
}: {
  q: string
  results: SearchResults | null
  playlists: Playlist[]
  onRerun: () => void
  onPlaylistsChanged: () => Promise<void>
  onOpenArtist: (artist: string) => void
  onOpenAlbum: (artist: string | null, album: string) => void
  onOpenMix: (playlistId: number) => void
  onViewFullSearch: (tab: SearchTab, q: string) => void
}) {
  if (!results) return <p className="loading">Searching…</p>
  if (results.error && results.q === q) {
    return (
      <div className="home-rail-empty">
        <p>{results.error}</p>
      </div>
    )
  }
  const data = results.data
  if (!data) return null
  const { artists, albums, songs } = data
  const matchedPlaylists = data.playlists
  if (
    artists.length + albums.length + songs.length + matchedPlaylists.length === 0 &&
    results.q === q
  ) {
    return (
      <div className="home-rail-empty">
        <p>No results for "{q}"</p>
      </div>
    )
  }

  async function openPlaylist(id: number) {
    // Same reason as playMix: App looks the id up in its own playlists
    // list, so make sure it's there before navigating.
    if (!playlists.some((p) => p.id === id)) await onPlaylistsChanged()
    onOpenMix(id)
  }

  function section(title: string, tab: SearchTab, children: ReactNode) {
    return (
      <section className="home-rail" aria-label={title}>
        <div className="home-rail-header sec-head">
          <h2 className="home-section-title">{title}</h2>
          <div className="sec-head-links">
            <button
              type="button"
              className="text-link home-rail-specify"
              onClick={() => onViewFullSearch(tab, q)}
              aria-label={`View full ${title.toLowerCase()} search`}
            >
              View full search
            </button>
          </div>
        </div>
        {children}
      </section>
    )
  }

  return (
    <>
      {artists.length > 0 &&
        section(
          'Artists',
          'artists',
          <RailScroll railKey="search-artists">
            {artists.map((artist) => (
              <button
                key={artist.artist}
                className="rail-card rail-card-artist"
                onClick={() => onOpenArtist(artist.artist)}
              >
                <Art
                  mediaItemId={artist.representative_media_item_id}
                  className="art-tile rail-card-art"
                  alt={artist.artist}
                  fallback={<MicIcon />}
                />
                <span className="rail-card-title">{artist.artist}</span>
                <span className="rail-card-sub">{plural(artist.album_count, 'album', 'albums')}</span>
              </button>
            ))}
          </RailScroll>,
        )}
      {albums.length > 0 &&
        section(
          'Albums',
          'albums',
          <RailScroll railKey="search-albums">
            {albums.map((album) => (
              <button
                key={`${album.artist ?? ''}::${album.album}`}
                className="rail-card"
                onClick={() => onOpenAlbum(album.artist, album.album)}
              >
                <Art
                  mediaItemId={album.representative_media_item_id}
                  className="art-tile rail-card-art"
                  alt={album.album}
                  fallback={<DiscIcon />}
                />
                <span className="rail-card-title">{album.album}</span>
                <span className="rail-card-sub">{album.artist ?? ''}</span>
              </button>
            ))}
          </RailScroll>,
        )}
      {songs.length > 0 &&
        section(
          'Songs',
          'songs',
          <TrackList
            tracks={songs}
            playlists={playlists}
            onChanged={onRerun}
            onPlaylistsChanged={onPlaylistsChanged}
            onOpenArtist={onOpenArtist}
            onOpenAlbum={onOpenAlbum}
          />,
        )}
      {matchedPlaylists.length > 0 &&
        section(
          'Playlists',
          'playlists',
          <RailScroll railKey="search-playlists">
            {matchedPlaylists.map((playlist) => (
              <button
                key={playlist.id}
                className="rail-card"
                onClick={() => openPlaylist(playlist.id)}
              >
                {playlist.preview_media_item_ids.length > 0 ? (
                  <div className="art-tile rail-card-art playlist-collage-wrap">
                    <MixCollage mediaItemIds={playlist.preview_media_item_ids} />
                  </div>
                ) : (
                  <Art
                    mediaItemId={null}
                    className="art-tile rail-card-art"
                    fallback={
                      playlist.auto_kind === 'favorites' ? (
                        <HeartIcon filled />
                      ) : playlist.auto_kind ? (
                        <SparkleIcon />
                      ) : (
                        <MusicNoteIcon />
                      )
                    }
                  />
                )}
                <span className="rail-card-title">{playlist.name} <CollectionDownloadMark kind="playlist" itemKey={playlist.id} subject={playlist.name} /></span>
                <span className="rail-card-sub">{plural(playlist.item_count, 'song', 'songs')}</span>
              </button>
            ))}
          </RailScroll>,
        )}
    </>
  )
}

export type { SearchTab }

export function HomeView({
  onOpenMix,
  onOpenPlaySource,
  playlists,
  onPlaylistsChanged,
  loadPlaylists,
  onOpenArtist,
  onOpenAlbum,
  onLoaded,
  query,
  onQueryChange,
  onViewFullSearch,
  onOpenTab,
}: {
  onViewFullSearch: (tab: SearchTab, q: string) => void
  /** "History" (Recently played) and "All radio" open these tabs. */
  onOpenTab: (tab: Tab) => void
  onOpenMix: (playlistId: number) => void
  /** A "Recently played" card takes you to the thing that was played
   * (Songs tab with filters restored, an artist, an album, a playlist, or
   * Radio) instead of starting playback immediately - see App's
   * openPlaySource. */
  onOpenPlaySource: (entry: PlaySourceEntry) => void
  playlists: Playlist[]
  onOpenArtist: (artist: string) => void
  onOpenAlbum: (artist: string | null, album: string) => void
  /** Mix playlists are real Playlist rows, generated/replaced server-side -
   * App's own top-level playlists list (used to look up a playlist by id
   * when opening one) needs to know a new one just appeared, or opening a
   * just-generated mix falls back to the plain playlist list instead of
   * its detail view since the id isn't in that stale list yet. Returns a
   * promise (App's refreshPlaylists is async) so playMix can await it
   * before navigating - see playMix for why that matters. */
  onPlaylistsChanged: () => Promise<void>
  /** App's playlists, from its own (shared) request: the Recommended section
   * looks for the Discovery playlist in them without asking again. */
  loadPlaylists: () => Promise<Playlist[]>
  /** Called once the rails have loaded, so App can restore the scroll
   * position it couldn't reach while the page was still just "Loading…". */
  onLoaded: () => void
  /** The search text from the URL (#/home/<q>), '' when not searching. */
  query: string
  /** Called (debounced) with the trimmed search text, to keep the URL in
   * step without adding history entries. */
  onQueryChange: (q: string) => void
}) {
  const { playQueue, playRadioStation } = usePlayerActions()
  const { showToast } = useToast()
  const [recentlyPlayedSources, setRecentlyPlayedSources] = useState<PlaySourceEntry[]>([])
  const { settings } = useSettings()
  const sections = visibleHomeSections(settings.homeSections)
  const [customiseOpen, setCustomiseOpen] = useState(false)
  const [recommended, setRecommended] = useState<MediaItem[]>([])
  const [discoveryExists, setDiscoveryExists] = useState(false)
  const [favoriteStations, setFavoriteStations] = useState<RadioStation[]>([])
  const [mixes, setMixes] = useState<MixPlaylist[]>([])
  const [mixesRegenerating, setMixesRegenerating] = useState(false)
  const [specifyMixOpen, setSpecifyMixOpen] = useState(false)
  const [refreshingDiscovery, setRefreshingDiscovery] = useState(false)
  const [discoveryResult, setDiscoveryResult] = useState<{
    seed_artists: string[]
    similar_artists: string[]
    tracks_added: number
  } | null>(null)
  const [discoveryError, setDiscoveryError] = useState<string | null>(null)
  // Each section loads and appears on its own (a placeholder of about its
  // height holds the place meanwhile), so a slow one never holds up the rest.
  const [sectionStatus, setSectionStatus] = useState<Partial<Record<HomeSectionId, SectionStatus>>>({})
  const sectionStatusRef = useRef(sectionStatus)
  useEffect(() => {
    sectionStatusRef.current = sectionStatus
  }, [sectionStatus])
  // Why the first section failed, for the page shown when every one did (a
  // down server must not look like a brand-new, empty library).
  const firstFailureRef = useRef<unknown>(null)
  // Why each failed section failed, shown in its place with a Retry.
  const [sectionErrors, setSectionErrors] = useState<Partial<Record<HomeSectionId, unknown>>>({})
  // Set by a section's own Retry: the next load leaves the other failed
  // sections as they are.
  const retryOnlyRef = useRef<HomeSectionId | null>(null)
  const [loadToken, setLoadToken] = useState(0)
  const playlistsRef = useRef(playlists)
  useEffect(() => {
    playlistsRef.current = playlists
  }, [playlists])

  const [exploreResults, setExploreResults] = useState<ExploreTrack[]>([])
  const [exploreSeedArtists, setExploreSeedArtists] = useState<string[]>([])
  const [exploreTried, setExploreTried] = useState(false)
  const [exploreLoading, setExploreLoading] = useState(false)
  const [exploreError, setExploreError] = useState<string | null>(null)
  const [playingPreviewUrl, setPlayingPreviewUrl] = useState<string | null>(null)
  // Not a DOM element: a preview is a detached Audio object, so no extra
  // <audio> sits in the page next to the player's own.
  const previewAudioRef = useRef<HTMLAudioElement | null>(null)

  const [input, setInput] = useState(query)
  const [results, setResults] = useState<SearchResults | null>(null)
  const trimmed = input.trim()
  // The last query this component sent up (or took from the URL), so an
  // echo of our own onQueryChange isn't mistaken for a URL change.
  const sentQueryRef = useRef(query)
  const searchSeqRef = useRef(0)
  const searchAbortRef = useRef<AbortController | null>(null)
  const firstSearchRef = useRef(true)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const isDesktop = useIsDesktop()

  // The URL changed under a still-mounted Home (the Home tab, or Back):
  // show its query instead of what was typed.
  useEffect(() => {
    if (query === sentQueryRef.current) return
    sentQueryRef.current = query
    setInput(query)
  }, [query])

  // Called from the search effect's timers: reads the latest URL query and
  // callback without being a dependency of that effect.
  const sendQuery = useEffectEvent((q: string) => {
    if (q === query) return
    sentQueryRef.current = q
    onQueryChange(q)
  })

  function runSearch(q: string) {
    searchAbortRef.current?.abort()
    const controller = new AbortController()
    searchAbortRef.current = controller
    const seq = ++searchSeqRef.current
    api
      .librarySearch(q, controller.signal)
      .then((data) => {
        if (seq === searchSeqRef.current) setResults({ q, data })
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === 'AbortError') return
        if (seq !== searchSeqRef.current) return
        setResults((prev) => ({
          q,
          data: prev?.data ?? null,
          error: err instanceof Error ? err.message : 'Search failed. Try again.',
        }))
      })
  }

  useEffect(() => {
    // A query already in the URL on mount (refresh, or Back to Home) runs
    // straight away; typing waits for a pause.
    const immediate = firstSearchRef.current
    firstSearchRef.current = false
    if (!trimmed) {
      searchAbortRef.current?.abort()
      searchSeqRef.current += 1
      setResults(null)
      if (immediate) return
      const timer = window.setTimeout(() => sendQuery(''), SEARCH_DEBOUNCE_MS)
      return () => window.clearTimeout(timer)
    }
    const run = () => {
      sendQuery(trimmed)
      runSearch(trimmed)
    }
    if (immediate) {
      run()
      return
    }
    const timer = window.setTimeout(run, SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trimmed])

  useEffect(() => () => searchAbortRef.current?.abort(), [])

  // Mix playlists are generated server-side (see refresh_all_genre_mixes),
  // which also materializes each one's collage ids on the playlist row -
  // /api/library/mixes responses carry preview_media_item_ids directly, so
  // Home needs no per-mix playlist fetches at all. Opening/playing a mix is
  // still as fast as any other playlist because the playlist itself is a
  // real, pre-generated row.

  useEffect(() => {
    return () => {
      previewAudioRef.current?.pause()
    }
  }, [])

  // The sections whose failure counts towards "Home could not load": Discover
  // needs only the optional discovery state, which never counts.
  const countedSections = sections.filter((id) => id !== 'discover')
  const settled = sections.every((id) => sectionStatus[id] === 'ready' || sectionStatus[id] === 'error')
  const allFailed =
    settled && countedSections.length > 0 && countedSections.every((id) => sectionStatus[id] === 'error')
  const someFailed = settled && countedSections.some((id) => sectionStatus[id] === 'error')
  const loadError = allFailed ? errorMessage(firstFailureRef.current, 'Could not load Home.') : null
  // While the load is still under way and nothing has arrived, the outcome
  // may be the one page-wide error above: a failed section keeps its
  // placeholder until that is known, so a down server does not flash one
  // error per section first.
  const mayAllFail = !settled && countedSections.every((id) => sectionStatus[id] !== 'ready')

  useEffect(() => {
    // Every shown section has its content (or its error), so the page has its
    // height: App can restore the scroll position it could not reach before.
    if (settled) onLoaded()
    // Only the moment loading finishes matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settled])

  const partlyFailed = someFailed && !allFailed
  useEffect(() => {
    if (partlyFailed) showToast('Some of Home could not be loaded.', 'error')
    // Once per load (a Retry starts a new one).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [partlyFailed, loadToken])

  useEffect(() => {
    let cancelled = false
    const explorePollTimers: number[] = []
    // Only what the shown sections need, and only what has not loaded yet: a
    // hidden section fetches nothing, and one shown again fetches its own.
    // A section's own Retry asks for that section (and for any that was still
    // loading, whose answer this run's start has just dropped), not for the
    // other failed ones.
    const only = retryOnlyRef.current
    retryOnlyRef.current = null
    const wanted = sections.filter((id) => {
      const status = sectionStatusRef.current[id]
      return status !== 'ready' && (only == null || id === only || status !== 'error')
    })
    if (wanted.length === 0) return
    setSectionStatus((previous) => {
      const next = { ...previous }
      for (const id of wanted) next[id] = 'loading'
      return next
    })
    const done = (id: HomeSectionId, error?: unknown) => {
      if (cancelled) return
      if (error !== undefined) {
        firstFailureRef.current ??= error
        setSectionErrors((previous) => ({ ...previous, [id]: error }))
      }
      setSectionStatus((previous) => ({ ...previous, [id]: error === undefined ? 'ready' : 'error' }))
    }
    const shown = new Set(wanted)

    // Discovery state is optional (nothing to show before the first refresh):
    // its failure is not a failed section. It hydrates both discovery panels
    // from what was last found - manually or by the daily auto-refresh - so a
    // tab switch or page refresh doesn't lose them and force a re-fetch.
    const discoveryState =
      shown.has('recommended') || shown.has('discover')
        ? api.homeGetDiscoveryState().then(
            (state) => {
              if (cancelled) return state
              if (state.discovery_updated_at) {
                setDiscoveryResult({
                  seed_artists: state.discovery_seed_artists,
                  similar_artists: state.discovery_similar_artists,
                  tracks_added: state.discovery_tracks_added,
                })
              }
              if (state.explore_updated_at) {
                setExploreSeedArtists(state.explore_seed_artists)
                setExploreResults(state.explore_results)
                setExploreTried(true)
              }
              if (state.explore_pending) pollExplorePreviews(0)
              return state
            },
            () => null,
          )
        : null

    // The server looks Explore's Deezer previews up in the background (they
    // are never stored); ask again until it has them, a bounded number of times.
    function pollExplorePreviews(attempt: number) {
      if (attempt >= EXPLORE_POLL_MAX) return
      const timer = window.setTimeout(() => {
        if (cancelled) return
        api.homeGetDiscoveryState().then(
          (state) => {
            if (cancelled) return
            if (state.explore_updated_at) setExploreResults(state.explore_results)
            if (state.explore_pending) pollExplorePreviews(attempt + 1)
          },
          () => {},
        )
      }, EXPLORE_POLL_MS)
      explorePollTimers.push(timer)
    }

    if (shown.has('recent')) {
      api.homeRecentlyPlayedSources().then(
        (sources) => {
          if (!cancelled) setRecentlyPlayedSources(sources)
          done('recent')
        },
        (error: unknown) => done('recent', error),
      )
    }
    if (shown.has('radio')) {
      api.homeListStations().then(
        (stations) => {
          if (!cancelled) setFavoriteStations(stations.filter((s) => s.is_favorite))
          done('radio')
        },
        (error: unknown) => done('radio', error),
      )
    }
    if (shown.has('mixes')) {
      api.homeListMixes().then(
        (list) => {
          if (cancelled) return
          setMixes(list)
          done('mixes')
          // A mix playlist App does not know yet (just generated) is fetched
          // into its list, so opening the mix finds it.
          const known = new Set(playlistsRef.current.map((p) => p.id))
          if (list.some((mix) => !known.has(mix.playlist_id))) void onPlaylistsChanged()
        },
        (error: unknown) => done('mixes', error),
      )
    }
    if (shown.has('discover')) {
      void discoveryState?.then(() => done('discover'))
    }
    if (shown.has('recommended')) {
      // The Discovery playlist that feeds the rail, found in App's playlists.
      loadPlaylists().then(
        async (list) => {
          await discoveryState
          const discovery = findDiscoveryPlaylist(list)
          if (discovery && !cancelled) {
            setDiscoveryExists(true)
            const detail = await api.homeGetPlaylist(discovery.id).catch(() => null)
            if (!cancelled && detail) setRecommended(detail.items.slice(0, MAX_HOME_ITEMS))
          }
          done('recommended')
        },
        (error: unknown) => done('recommended', error),
      )
    }
    return () => {
      cancelled = true
      for (const timer of explorePollTimers) window.clearTimeout(timer)
      // Left half-way (unmount, a section hidden or shown): what has not
      // arrived is asked for again by the next run.
      setSectionStatus((previous) => {
        const next = { ...previous }
        for (const id of wanted) if (next[id] === 'loading') delete next[id]
        return next
      })
    }
    // Runs on mount, on Retry and when a section is shown again (its data
    // was not fetched while hidden). The callbacks are App's and are read
    // when the answers land.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadToken, sections.join(',')])

  /** A failed section's Retry: that section goes back to its placeholder and
   * is asked for again; the rest of Home stays as it is. */
  function retrySection(id: HomeSectionId) {
    retryOnlyRef.current = id
    setSectionStatus((previous) => {
      const next = { ...previous }
      delete next[id]
      return next
    })
    setLoadToken((token) => token + 1)
  }

  async function regenerateMixes() {
    // The button is disabled meanwhile; this covers a press before that shows.
    if (mixesRegenerating) return
    setMixesRegenerating(true)
    try {
      const list = await api.refreshMixes()
      const refreshedSources = sections.includes('recent') ? await api.homeRecentlyPlayedSources().catch(() => null) : null
      setMixes(list)
      // Mix playlists are disposable and may have been deleted by the
      // refresh. Replace the Home rail immediately instead of showing a
      // dead recently-played card until Home is remounted.
      if (refreshedSources) setRecentlyPlayedSources(refreshedSources)
      void onPlaylistsChanged()
    } catch (error) {
      // The mixes on screen are still the old ones: say the refresh failed.
      showToast(errorMessage(error, 'Could not regenerate the mixes.'), 'error')
    } finally {
      setMixesRegenerating(false)
    }
  }

  async function playMix(mix: MixPlaylist) {
    // App's own `playlists` list (used to look up a playlist by id when
    // opening one) can still be missing a just-generated mix at click time -
    // the background refresh from load/regenerate is a separate, unawaited
    // call, so a fast click can beat it. Awaiting a fresh one here first
    // guarantees the mix is in that list *before* navigating, so
    // PlaylistsView's very first render already finds it - otherwise it
    // briefly renders the plain playlist list (not found) before the
    // list state catches up and it re-renders as the actual mix, visibly
    // flashing the wrong screen.
    await onPlaylistsChanged()
    // Opening a mix should browse it without unexpectedly starting playback.
    onOpenMix(mix.playlist_id)
  }

  async function handleRefreshDiscovery() {
    setRefreshingDiscovery(true)
    setDiscoveryError(null)
    try {
      const result = await api.refreshDiscovery()
      setDiscoveryResult(result)
      const playlists = await api.listPlaylists().catch(() => [])
      const discovery = findDiscoveryPlaylist(playlists)
      if (discovery) {
        setDiscoveryExists(true)
        const detail = await api.getPlaylist(discovery.id).catch(() => null)
        if (detail) setRecommended(detail.items.slice(0, MAX_HOME_ITEMS))
      }
    } catch (err) {
      setDiscoveryError(err instanceof Error ? err.message : 'Something went wrong. Try again.')
    } finally {
      setRefreshingDiscovery(false)
    }
  }

  function discoveryEmptyMessage(): string {
    if (discoveryError) return discoveryError
    if (!discoveryResult) {
      return discoveryExists
        ? "No matches found last time. Try again, or listen to a bit more of your library first."
        : 'Personalised picks from your own library, based on what you listen to.'
    }
    const { seed_artists, similar_artists } = discoveryResult
    if (seed_artists.length === 0) {
      return "You haven't listened to enough yet for this to have anything to go on - play a handful of tracks, then try again."
    }
    if (similar_artists.length === 0) {
      return `Couldn't find artists in your library similar to ${seed_artists.slice(0, 3).join(', ')}. Try again after listening to some other artists too.`
    }
    return `Found artists similar to ${seed_artists.slice(0, 3).join(', ')} (like ${similar_artists.slice(0, 3).join(', ')}), but none of them are in your library yet.`
  }

  async function handleExplore() {
    setExploreLoading(true)
    setExploreError(null)
    try {
      const result = await api.exploreDiscovery()
      setExploreSeedArtists(result.seed_artists)
      setExploreResults(result.results)
    } catch (err) {
      setExploreError(err instanceof Error ? err.message : 'Something went wrong. Try again.')
    } finally {
      setExploreTried(true)
      setExploreLoading(false)
    }
  }

  function togglePreview(url: string) {
    const audio = (previewAudioRef.current ??= new Audio())
    audio.onended = () => setPlayingPreviewUrl(null)
    if (playingPreviewUrl === url) {
      audio.pause()
      setPlayingPreviewUrl(null)
      return
    }
    audio.src = url
    audio.play().catch(() => {})
    setPlayingPreviewUrl(url)
  }

  // Whole rows of the grid: 3 across on desktop, 2 on a phone.
  const visibleRecentlyPlayedSources = recentlyPlayedSources.slice(0, isDesktop ? 9 : 6)
  const visibleExploreResults = exploreResults.slice(0, MAX_HOME_ITEMS)
  const visibleMixes = cappedMixes(mixes, MAX_HOME_ITEMS)
  const visibleFavoriteStations = favoriteStations.slice(0, MAX_HOME_ITEMS)

  function exploreEmptyMessage(): string {
    if (exploreError) return exploreError
    if (exploreSeedArtists.length === 0) {
      return "You haven't listened to enough yet for this to have anything to go on - play a handful of tracks, then try again."
    }
    return `Found artists similar to ${exploreSeedArtists.slice(0, 3).join(', ')}, but couldn't find previews for any of them right now. Try again later.`
  }

  /** One of Home's sections (settings.homeSections picks which, in order). */
  function renderSection(id: HomeSectionId): ReactNode {
    switch (id) {
      case 'recent':
        return (
              <HomeRail
                title="Recently played"
                helpText="Jump back to albums, artists, playlists, radio stations, or filtered library views you listened to recently."
                links={
                  <a
                    href="#/recent-played"
                    className="text-link"
                    onClick={(e) => {
                      e.preventDefault()
                      onOpenTab('recent-played')
                    }}
                  >
                    History
                  </a>
                }
              >
                {visibleRecentlyPlayedSources.length > 0 ? (
                  <div className="play-source-grid">
                    {visibleRecentlyPlayedSources.map((entry) => (
                      <button
                        key={`${entry.kind}:${JSON.stringify(entry.payload)}`}
                        className="play-source-card"
                        onClick={() => onOpenPlaySource(entry)}
                        title={`Go to ${entry.label}`}
                      >
                        {entry.preview_media_item_ids.length > 0 ? (
                          <div className="play-source-art playlist-collage-wrap">
                            <MixCollage mediaItemIds={entry.preview_media_item_ids} />
                          </div>
                        ) : (
                          <Art
                            mediaItemId={entry.art_media_item_id}
                            className="play-source-art"
                            fallback={playSourceIcon(entry.kind)}
                            alt={entry.label}
                          />
                        )}
                        <span className="play-source-text">
                          <span className="play-source-label">{entry.label}</span>
                          <span className="play-source-kind">{PLAY_SOURCE_KINDS[entry.kind] ?? ''}</span>
                        </span>
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="home-rail-empty">
                    <p>
                      Albums, artists, playlists, and searches you play will show up here to
                      jump back into.
                    </p>
                  </div>
                )}
              </HomeRail>
        )
      case 'recommended':
        return (
              <HomeRail
                title="Recommended from your library"
                helpText="A selection of tracks from your library. Use the refresh button to get a different set of recommendations."
                onRegenerate={recommended.length > 0 ? handleRefreshDiscovery : undefined}
                regenerating={refreshingDiscovery}
              >
                {recommended.length > 0 ? (
                  <TrackRail items={recommended} onPlay={(_, i) => playQueue(recommended, i)} playlists={playlists} onPlaylistsChanged={onPlaylistsChanged} onOpenArtist={onOpenArtist} onOpenAlbum={onOpenAlbum} />
                ) : (
                  <div className="home-rail-empty">
                    <p>{discoveryEmptyMessage()}</p>
                    <button onClick={handleRefreshDiscovery} disabled={refreshingDiscovery}>
                      {refreshingDiscovery
                        ? 'Finding picks…'
                        : discoveryExists
                          ? 'Try again'
                          : 'Get recommendations'}
                    </button>
                  </div>
                )}
              </HomeRail>
        )
      case 'discover':
        return (
              <HomeRail
                title="Discover new artists"
                helpText="Find artists similar to your listening and hear short previews from outside your library."
                onRegenerate={exploreResults.length > 0 ? handleExplore : undefined}
                regenerating={exploreLoading}
              >
                {visibleExploreResults.length > 0 ? (
                  <>
                    {exploreError && (
                      <p className="home-rail-empty-error" role="status">
                        {exploreError}
                      </p>
                    )}
                    <RailScroll railKey="explore">
                      {visibleExploreResults.map((track) => (
                        <ExploreCard
                          key={track.preview_url}
                          track={track}
                          isPlaying={playingPreviewUrl === track.preview_url}
                          onToggle={() => togglePreview(track.preview_url)}
                        />
                      ))}
                    </RailScroll>
                    <p className="home-rail-attribution">
                      {visibleExploreResults.length} previews · scroll horizontally for more · Previews courtesy of Deezer
                    </p>
                  </>
                ) : (
                  <div className="home-rail-empty">
                    <p>
                      {exploreTried
                        ? exploreEmptyMessage()
                        : "Artists similar to what you listen to that you don't own yet - with a 30-second preview from Deezer's catalogue so you can hear them before deciding whether they're worth tracking down."}
                    </p>
                    <button onClick={handleExplore} disabled={exploreLoading}>
                      {exploreLoading ? 'Looking…' : exploreTried ? 'Try again' : 'Find new music'}
                    </button>
                  </div>
                )}
              </HomeRail>
        )
      case 'mixes':
        return (
              <HomeRail
                title="Mixes for you"
                helpText="Daily mixes from the artists you play together (mostly songs you know, some new to you) and from the genres and tags you listen to, plus a few to explore. Refresh for new mixes, or choose a genre or tag with Specify mix."
                onRegenerate={mixes.length > 0 ? regenerateMixes : undefined}
                regenerating={mixesRegenerating}
                onSpecify={() => setSpecifyMixOpen(true)}
              >
                {visibleMixes.length > 0 ? (
                  <RailScroll railKey="mixes">
                    {visibleMixes.map((mix) => (
                      <button
                        key={mix.playlist_id}
                        className="mix-card"
                        onClick={() => playMix(mix)}
                        title={`Open ${mix.name}`}
                      >
                        <span className="mix-card-art">
                          <MixCollage mediaItemIds={mix.preview_media_item_ids ?? []} />
                        </span>
                        <span className="mix-card-title">{mix.genre}</span>
                        <span className="mix-card-sub">Mix</span>
                      </button>
                    ))}
                  </RailScroll>
                ) : (
                  <div className="home-rail-empty">
                    <p>
                      Shuffled mixes built from what you listen to and the genres in your library -
                      refreshed daily, or generate them now.
                    </p>
                    <button onClick={regenerateMixes} disabled={mixesRegenerating}>
                      {mixesRegenerating ? 'Finding mixes…' : 'Get mixes'}
                    </button>
                  </div>
                )}
              </HomeRail>
        )
      case 'radio':
        return (
              <HomeRail
                title="Your radio stations"
                helpText="Stations you favourite in the Radio section appear here. Select one to start listening."
                links={
                  <a
                    href="#/radio"
                    className="text-link"
                    onClick={(e) => {
                      e.preventDefault()
                      onOpenTab('radio')
                    }}
                  >
                    All radio
                  </a>
                }
              >
                {visibleFavoriteStations.length > 0 ? (
                  <div className="station-tile-grid">
                    {visibleFavoriteStations.map((station) => (
                      <button
                        key={station.id}
                        className="rail-card station-tile"
                        onClick={() => playRadioStation(station)}
                      >
                        <span className="station-tile-icon" aria-hidden="true"><RadioIcon /></span>
                        <span className="station-tile-text">
                          <span className="rail-card-title">{station.name}</span>
                          <span className="rail-card-sub">
                            {station.description || 'Radio station'}
                          </span>
                        </span>
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="home-rail-empty">
                    <p>Favourite a station from the Radio tab to see it here.</p>
                  </div>
                )}
              </HomeRail>
        )
    }
  }

  return (
    <div className="home-view">
      {/* The whole-library search sits in the top bar, where other tabs show
          their title; its state stays here. */}
      <TopBarTitlePortal>
        <form
          role="search"
          className="home-search"
          onSubmit={(e) => {
            e.preventDefault()
            searchInputRef.current?.blur()
          }}
        >
          <SearchIcon className="home-search-icon" />
          <input
            ref={searchInputRef}
            type="search"
            aria-label="Search your library"
            autoComplete="off"
            placeholder={isDesktop ? 'Search artists, albums, songs, playlists' : 'Search your library'}
            value={input}
            onChange={(e) => setInput(e.target.value)}
          />
          {input && (
            <button
              type="button"
              className="home-search-clear"
              aria-label="Clear search"
              onClick={() => {
                setInput('')
                searchInputRef.current?.focus()
              }}
            >
              <CloseIcon />
            </button>
          )}
        </form>
      </TopBarTitlePortal>
      {/* Customise sits in the header beside the device picker; search
          results are not Home's sections, so it steps aside for them. */}
      {!trimmed && (
        <HeaderActionsPortal>
          <button
            type="button"
            className="home-customise-toggle"
            onClick={() => setCustomiseOpen(true)}
            aria-label="Customise Home"
            title="Customise Home"
          >
            <CustomiseIcon aria-hidden="true" />
            <span className="home-customise-label">Customise</span>
          </button>
        </HeaderActionsPortal>
      )}
      {trimmed ? (
        <HomeSearchResults
          q={trimmed}
          results={results}
          playlists={playlists}
          onRerun={() => runSearch(trimmed)}
          onPlaylistsChanged={onPlaylistsChanged}
          onOpenArtist={onOpenArtist}
          onOpenAlbum={onOpenAlbum}
          onOpenMix={onOpenMix}
          onViewFullSearch={onViewFullSearch}
        />
      ) : loadError ? (
        <ErrorState
          message={loadError}
          onRetry={() => {
            firstFailureRef.current = null
            setSectionStatus({})
            setLoadToken((token) => token + 1)
          }}
        />
      ) : (
        <>
          {sections.map((id) => (
            <Fragment key={id}>
              {sectionStatus[id] === 'ready' ? (
                renderSection(id)
              ) : sectionStatus[id] === 'error' && !mayAllFail ? (
                <HomeSectionError id={id} error={sectionErrors[id]} onRetry={() => retrySection(id)} />
              ) : (
                <HomeSectionPlaceholder id={id} />
              )}
            </Fragment>
          ))}
          {sections.length === 0 && (
            <div className="home-rail-empty">
              <p>Every Home section is hidden. Use Customise to show them again.</p>
            </div>
          )}
          {specifyMixOpen && (
            <Suspense fallback={null}>
            <SpecifyMixDialog
              hint="Choose a genre or library tag to add a temporary mix to the end of this row."
              // The rail shows the POST response directly, and awaiting
              // onPlaylistsChanged here is what makes a just-created mix
              // known to App before playMix can navigate to it.
              onCreated={async (list) => {
                setMixes(list)
                await onPlaylistsChanged()
              }}
              onClose={() => setSpecifyMixOpen(false)}
            />
            </Suspense>
          )}
        </>
      )}
      {customiseOpen && (
        <Suspense fallback={null}>
          <HomeSectionsDialog onClose={() => setCustomiseOpen(false)} />
        </Suspense>
      )}
    </div>
  )
}
