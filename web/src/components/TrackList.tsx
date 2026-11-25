import { Suspense, useEffect, useId, useLayoutEffect, useOptimistic, useState } from 'react'
import { api } from '../api'
import { useAuth } from '../AuthContext'
import { publishPreferenceChange, subscribePreferenceChange } from '../favoritesBus'
import { usePlayerActions } from '../PlayerContext'
import { useCurrentTrackId } from '../playerSelectors'
import { useSettings } from '../SettingsContext'
import { menuTriggerProps, type MenuAnchor } from '../menuTrigger'
import { GRID_LIST_ART_REMS } from '../settings'
import { useToast } from '../ToastContext'
import type { MediaItem, Playlist, PlaySource, PreferenceState, TrackSort } from '../types'
import { useLongPress } from '../useLongPress'
import { useVirtualRange } from '../useVirtualRange'
import { useIsDesktop } from '../useIsDesktop'
import { Art } from './Art'
import { useConfirm } from '../dialogs'
import { LibraryLinks } from './LibraryLinks'
import { MarqueeText } from './MarqueeText'
import { trackPreference } from '../preference'
import { PreferenceHeart } from './PreferenceHeart'
import { lazyView } from '../lazyView'
import { TrackMenuPanel } from './TrackMenuPanel'
import { mediaRef } from '../artEntity'
import { formatDuration } from '../formatDuration'
import { DownloadMark } from '../native/DownloadMark'
import { useDownloadedIds } from '../native/downloadStore'
import { NOT_DOWNLOADED_MESSAGE } from '../native/localPlayback'
import { useOffline } from '../native/offline'

// Opened on demand, so their code loads when first needed.
const LyricsPanel = lazyView(() => import('./LyricsPanel').then((m) => m.LyricsPanel))
const TagEditModal = lazyView(() => import('./TagEditModal').then((m) => m.TagEditModal))

function isTrackControl(target: EventTarget | null): boolean {
  return target instanceof Element && Boolean(target.closest('a, button, [role="button"]'))
}

type Overrides = Record<number, Partial<MediaItem>>

// The table gets its Year and Plays columns once the list is at least this
// wide (CSS px): below it the title, artist and album columns would be too
// narrow to read. Measured on the list itself, so an open side panel or the
// sidebar counts, not just the window width.
const STATS_MIN_WIDTH_PX = 800

/** A number cell's text: "–" for no value (no year, never played). */
function numberCell(value: number | null | undefined): string {
  return value ? String(value) : '–'
}

function mergeOverride(overrides: Overrides, { id, fields }: { id: number; fields: Partial<MediaItem> }): Overrides {
  return { ...overrides, [id]: { ...overrides[id], ...fields } }
}

function preferenceFields(state: PreferenceState): Pick<MediaItem, 'is_favorite' | 'is_disliked'> {
  return { is_favorite: state === 'liked', is_disliked: state === 'disliked' }
}

/** Why a row is dimmed, or null: the song itself is disliked, or - while
 * its own heart is neutral - its album or artist is. */
function dislikeReason(track: MediaItem): string | null {
  if (track.is_disliked) return 'Disliked'
  if (track.is_favorite || !track.excluded_by) return null
  return track.excluded_by === 'album' ? 'Album disliked' : 'Artist disliked'
}

interface Props {
  tracks: MediaItem[]
  playlists: Playlist[]
  onChanged: () => void
  scrollContainerRef?: React.RefObject<HTMLElement | null>
  onLoadMore?: () => void
  loadingMore?: boolean
  hasMore?: boolean
  /** When provided, this list represents a server-filterable context (the
   * Songs tab) rather than a fixed small set - tapping a track queues the
   * whole matching context (windowed server-side), not just the
   * loaded/rendered page, so shuffle/next draw from everything matching
   * the filter instead of only what's scrolled into view. */
  libraryFilters?: { q?: string; genre?: string; tag?: string }
  /** The order `libraryFilters`' list is shown in (the Songs tab's sort),
   * so the queue it starts follows the rows on screen. */
  libraryOrder?: { sort: TrackSort; direction: 'asc' | 'desc' }
  /** The table layout adds Year and Plays columns where they fit (the
   * Songs tab, whose tracks carry `year` and `play_count`). */
  showStats?: boolean
  /** What this list itself represents (an album, artist, playlist, or the
   * Songs tab) - recorded via api.recordPlaySource whenever a track here
   * starts playback, so "Recently played" can resurface the actual thing
   * played (the album, the playlist) instead of a lone track stranded out
   * of its context. Omitted for lists with no meaningful named source
   * (a generated-from-song mix, search results, etc). */
  playSource?: PlaySource
  /** Called after "Generate playlist from this song" creates/updates the
   * managed "Generated" playlist - lets whatever's showing the playlist
   * list pick it up immediately instead of only on next reload. */
  onPlaylistsChanged?: () => void
  /** True when this list's own membership depends on favorite status (the
   * Favourites playlist) - unfavoriting a track here, or anywhere else
   * while this is on screen, should drop it from view immediately rather
   * than just leaving a stale, unfilled heart until the next reload. */
  membershipTracksFavorites?: boolean
  /** Set when this list is a playlist's own detail view - enables a
   * "Remove from playlist" menu action (each track's `playlist_item_id`
   * says which occurrence to remove, in case the same track is in the
   * playlist more than once). */
  playlistId?: number
  onOpenArtist?: (artist: string) => void
  onOpenAlbum?: (artist: string | null, album: string) => void
  disableArtistLink?: boolean
  disableAlbumLink?: boolean
  /** 'list' (default): art, title and an artist · album line. 'table': the
   * Songs tab's columns (title, artist, album, time) under a header row on a
   * desktop-width screen - a phone shows the list rows. 'cards': song cards,
   * two columns on a desktop-width screen, one on a phone. */
  layout?: TrackListLayout
  /** Filled with a function that scrolls the list to a track index (A-Z). */
  jumpRef?: React.RefObject<((index: number, offsetPx?: number) => void) | null>
}

export type TrackListLayout = 'list' | 'table' | 'cards'

export function TrackList({
  tracks,
  playlists,
  onChanged,
  scrollContainerRef,
  onLoadMore,
  loadingMore = false,
  hasMore,
  libraryFilters,
  libraryOrder,
  showStats = false,
  playSource,
  onPlaylistsChanged,
  membershipTracksFavorites,
  playlistId,
  onOpenArtist,
  onOpenAlbum,
  disableArtistLink,
  disableAlbumLink,
  layout = 'list',
  jumpRef,
}: Props) {
  const { user } = useAuth()
  const { settings } = useSettings()
  const isDesktop = useIsDesktop()
  const gridSize = isDesktop ? settings.gridSizeDesktop : settings.gridSizeMobile
  const compact = settings.listDensity === 'compact'
  const shownLayout: TrackListLayout = layout === 'table' && !isDesktop ? 'list' : layout
  // Cards sit two to a row on a desktop-width screen: the window is counted
  // in rows of cards, each row as tall as one card (the grid has no row gap).
  const columns = shownLayout === 'cards' && isDesktop ? 2 : 1
  // First-paint estimate only - useVirtualRange measures the real row
  // height once one renders. Compact rows have 0.75x art and less padding
  // (track-list.css, [data-density='compact']); cards add a little more.
  const artPx = GRID_LIST_ART_REMS[gridSize] * 16 * (compact ? 0.75 : 1)
  const rowHeight = Math.ceil(artPx + (compact ? 12 : 24) + (shownLayout === 'cards' ? 8 : 0))
  // Not the whole player value: a list re-rendered on every step of a volume
  // drag or a device heartbeat.
  const { playQueue, playLibraryContext } = usePlayerActions()
  const currentTrackId = useCurrentTrackId()
  const { showToast } = useToast()
  const offline = useOffline()
  const downloaded = useDownloadedIds()
  const confirm = useConfirm()
  const menuId = useId()
  const [openMenuFor, setOpenMenuFor] = useState<string | null>(null)
  const [menuAnchor, setMenuAnchor] = useState<MenuAnchor | null>(null)
  const [lyricsFor, setLyricsFor] = useState<MediaItem | null>(null)
  const [editingTagsFor, setEditingTagsFor] = useState<MediaItem[] | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [removing, setRemoving] = useState(false)
  const [overrides, setOverrides] = useState<Overrides>({})
  const [shownOverrides, showOverride] = useOptimistic(overrides, mergeOverride)
  const [locallyRemoved, setLocallyRemoved] = useState<Set<number>>(new Set())
  const longPress = useLongPress<string>((trackKey, point) => {
    setMenuAnchor({ x: point.x, y: point.y })
    setOpenMenuFor(trackKey)
  })
  // The node itself (not a ref object) - see the comment in
  // useVirtualRange.ts on why a plain useRef here would silently keep
  // measuring a stale, detached list after this component's list element
  // unmounts and remounts (e.g. navigating away and back).
  const [listNode, setListNode] = useState<HTMLUListElement | null>(null)
  const [wideEnoughForStats, setWideEnoughForStats] = useState(false)
  const statsShown = showStats && shownLayout === 'table' && wideEnoughForStats

  // Before paint (no flash of a column appearing), then on every resize.
  useLayoutEffect(() => {
    if (!showStats || shownLayout !== 'table' || !listNode) return
    const measure = () => setWideEnoughForStats(listNode.getBoundingClientRect().width >= STATS_MIN_WIDTH_PX)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(listNode)
    return () => observer.disconnect()
  }, [showStats, shownLayout, listNode])

  // Any track liked/disliked anywhere (another list, the Now Playing
  // heart) should be reflected here instantly, not just wherever the click
  // happened - see favoritesBus.
  useEffect(() => {
    return subscribePreferenceChange((mediaItemId, state) => {
      setOverride(mediaItemId, preferenceFields(state))
      if (membershipTracksFavorites && state !== 'liked') {
        setLocallyRemoved((prev) => new Set(prev).add(mediaItemId))
      }
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [membershipTracksFavorites])

  const visibleTracks =
    locallyRemoved.size === 0 ? tracks : tracks.filter((t) => !locallyRemoved.has(t.id))

  const rowCount = Math.ceil(visibleTracks.length / columns)
  const { start: startRow, end: endRow, topPad, bottomPad, scrollToIndex } = useVirtualRange(rowCount, {
    itemHeight: rowHeight,
    containerNode: listNode,
    scrollContainerRef,
    onEndReached: hasMore ? onLoadMore : undefined,
  })
  const start = startRow * columns
  const end = Math.min(visibleTracks.length, endRow * columns)

  // The Songs tab's A-Z jump scrolls to a track index (see SongsView).
  useEffect(() => {
    if (!jumpRef) return
    jumpRef.current = (index: number, offsetPx?: number) => scrollToIndex(Math.floor(index / columns), offsetPx)
    return () => {
      jumpRef.current = null
    }
  })

  function closeMenu() {
    setOpenMenuFor(null)
  }

  function withOverride(track: MediaItem): MediaItem {
    const o = shownOverrides[track.id]
    return o ? { ...track, ...o } : track
  }

  function setOverride(id: number, fields: Partial<MediaItem>) {
    setOverrides((prev) => mergeOverride(prev, { id, fields }))
  }

  // Runs inside the heart's transition: the row shows `next` (its dimming
  // too) while the change is out, and goes back by itself if it fails.
  async function cyclePreference(track: MediaItem, next: PreferenceState) {
    showOverride({ id: track.id, fields: preferenceFields(next) })
    try {
      await api.setTrackPreference(track.id, next)
    } catch (err) {
      showToast(`Couldn't update "${track.title}".`, 'error')
      throw err
    }
    // The real override, through this list's own subscription, before the
    // transition ends.
    publishPreferenceChange(track.id, next)
    // Liking adds the track to the Favourites playlist and anything else
    // removes it - the Playlists tab's item_count needs to know, not just
    // show it stale until the next manual refresh.
    onPlaylistsChanged?.()
  }

  async function removeFromPlaylist(track: MediaItem) {
    if (playlistId == null || track.playlist_item_id == null) return
    if (!(await confirm({
      title: 'Remove from playlist',
      body: `Remove "${track.title}" from this playlist?`,
      confirmLabel: 'Remove',
      danger: true,
    }))) return
    try {
      // Removes by this specific playlist_item_id (not media item id), so
      // if the same track appears in the playlist more than once, only
      // this occurrence goes - refetch via onChanged rather than a local
      // filter-by-id, which couldn't tell those occurrences apart.
      await api.removeFromPlaylist(playlistId, track.playlist_item_id)
      onChanged()
      onPlaylistsChanged?.()
      showToast(`Removed "${track.title}" from the playlist.`, 'success')
    } catch {
      showToast(`Couldn't remove "${track.title}" from the playlist.`, 'error')
    }
  }

  function rowKey(track: MediaItem, index: number): string {
    return track.playlist_item_id != null
      ? `playlist-item:${track.playlist_item_id}`
      : `track:${track.id}:${index}`
  }

  /** What a selection is keyed by: the track (or playlist entry) itself, not
   * its position, so a reload that moves rows - after a partial bulk remove -
   * cannot leave the selection pointing at nothing. */
  function selectionKey(track: MediaItem): string {
    return track.playlist_item_id != null ? `playlist-item:${track.playlist_item_id}` : `track:${track.id}`
  }

  function toggleSelected(key: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  function handleContextMenu(e: React.MouseEvent, trackKey: string) {
    e.preventDefault()
    setMenuAnchor({ x: e.clientX, y: e.clientY })
    setOpenMenuFor(trackKey)
  }

  function playFull(track: MediaItem, index: number) {
    // A library-filterable context (the Songs tab) queues the whole
    // matching set server-side, windowed - not just the page that happens
    // to be loaded - so shuffle/next draw from everything matching the
    // filter instead of only a few dozen scrolled-into-view tracks.
    if (libraryFilters) {
      playLibraryContext(libraryFilters, track.id, libraryOrder, playSource)
    } else {
      playQueue(visibleTracks, index, playSource)
    }
    if (playSource) api.recordPlaySource(playSource).catch(() => {})
  }

  function handleTrackTap(track: MediaItem, index: number) {
    if (longPress.consumeClick()) return
    if (offline && !downloaded.has(track.id)) {
      showToast(NOT_DOWNLOADED_MESSAGE, 'error', 'not-downloaded')
      return
    }
    if (selectedTracks.length > 0) {
      toggleSelected(selectionKey(track))
    } else {
      playFull(track, index)
    }
  }

  async function bulkRemove() {
    if (removing) return
    if (!(await confirm({
      title: 'Remove from library',
      body: `Remove ${selectedTracks.length} selected track(s) from Atriolyric? This removes their indexed records and generated Atriolyric cache only. The original music files will not be changed or deleted.`,
      confirmLabel: 'Remove',
      danger: true,
    }))) {
      return
    }
    setRemoving(true)
    try {
      const selectedIds = [...new Set(selectedTracks.map((track) => track.id))]
      const result = await api.adminBulkRemove(selectedIds)
      const failedIds = new Set(result.failed.map((failure) => failure.id))
      setSelected(new Set(visibleTracks.filter((track) => failedIds.has(track.id)).map(selectionKey)))
      if (result.failed.length > 0) {
        showToast(`${result.failed.length} track(s) could not be removed from Atriolyric.`, 'error')
      }
      onChanged()
    } catch {
      showToast('Could not remove the selected tracks from Atriolyric.', 'error')
    } finally {
      setRemoving(false)
    }
  }

  // Only rows still in the list count: a stale key never opens an empty editor.
  const selectedTracks = visibleTracks.filter((track) => selected.has(selectionKey(track)))

  return (
    <>
      {user?.is_admin && selectedTracks.length > 0 && (
        <div className="bulk-action-bar">
          <span>{selectedTracks.length} selected</span>
          <button onClick={() => selectedTracks.length > 0 && setEditingTagsFor(selectedTracks)}>Edit tags</button>
          <button onClick={bulkRemove} disabled={removing}>
            {removing ? 'Removing…' : 'Remove from library'}
          </button>
          <button onClick={() => setSelected(new Set())}>Cancel</button>
        </div>
      )}

      {shownLayout === 'table' && (
        // Column labels for the rows below. The rows are list items, not
        // table cells, so the labels are for the eye only.
        <div className={`track-table-head ${statsShown ? 'track-table-stats' : ''}`} aria-hidden="true">
          <span className="track-col-title">Title</span>
          <span className="track-col-artist">Artist</span>
          <span className="track-col-album">Album</span>
          {statsShown && (
            <>
              <span className="track-col-num">Year</span>
              <span className="track-col-num">Plays</span>
            </>
          )}
          <span className="track-col-time">Time</span>
        </div>
      )}
      <ul
        className={`track-list track-list-${shownLayout} ${statsShown ? 'track-table-stats' : ''}`}
        ref={setListNode}
      >
        {topPad > 0 && <li className="track-row-spacer" style={{ height: topPad }} aria-hidden />}
        {visibleTracks.slice(start, end).map((rawTrack, i) => {
          const index = start + i
          const track = withOverride(rawTrack)
          const key = rowKey(track, index)
          const isSelected = selected.has(selectionKey(track))
          const dislikedReason = dislikeReason(track)
          // The Android app with no connection: only what is on the device plays.
          const unavailable = offline && !downloaded.has(track.id)
          return (
          // The keyboard path is the row's play/select button. These handlers
          // are the pointer extras: a click on the row's empty part plays it,
          // and right-click or a long press opens its menu.
          // oxlint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions
          <li
            key={key}
            className={`track-row ${shownLayout === 'cards' ? 'track-card' : ''} ${track.id === currentTrackId ? 'track-row-current' : ''} ${isSelected ? 'track-row-selected' : ''} ${dislikedReason ? 'track-row-disliked' : ''} ${unavailable ? 'track-row-unavailable' : ''}`}
            title={unavailable ? 'Not downloaded' : dislikedReason ? `${dislikedReason} – won’t appear in mixes` : undefined}
            onClick={(e) => {
              if (!isTrackControl(e.target)) handleTrackTap(track, index)
            }}
            onContextMenu={(e) => handleContextMenu(e, key)}
            onPointerDown={(e) => longPress.start(e, key)}
            onPointerUp={longPress.cancel}
            onPointerLeave={longPress.cancel}
            onPointerCancel={longPress.cancel}
          >
            {/* Nothing interactive nests in another interactive element: the
                play/select button is an overlay that fills this area (the
                art and text above it pass presses through), and the artist
                and album links sit above the overlay as its siblings. */}
            <div className="track-title-btn">
              <button
                type="button"
                className="track-play-btn"
                onClick={() => handleTrackTap(track, index)}
                aria-label={
                  selectedTracks.length === 0
                    ? `Play ${track.title}`
                    : `${isSelected ? 'Deselect' : 'Select'} ${track.title}`
                }
                aria-pressed={selectedTracks.length > 0 ? isSelected : undefined}
              />
              <Art mediaItemId={track.id} artRef={mediaRef(track)} className="art-xs appearance-list-art" defer={Boolean(onLoadMore)} />
              <div className="track-title-text">
                <MarqueeText className="track-title marquee-left" text={track.title} always={track.id === currentTrackId} />
                {shownLayout !== 'table' && (
                <MarqueeText className="track-subtitle marquee-left" always={track.id === currentTrackId}>
                  <LibraryLinks
                    artist={track.artist}
                    artists={track.artists}
                    albumArtist={track.album_artist}
                    album={track.album}
                    onArtist={onOpenArtist}
                    onAlbum={onOpenAlbum}
                    disableArtist={disableArtistLink}
                    disableAlbum={disableAlbumLink}
                  />
                </MarqueeText>
                )}
              </div>
              {/* Inside the title cell: the table layout has a fixed set of columns. */}
              <DownloadMark mediaId={track.id} subject={track.title} />
            </div>

            {shownLayout === 'table' && (
              <>
                {/* Cut with "…" at rest, and scroll with the title (hover,
                    focus, the current song) - the same MarqueeText. */}
                <span className="track-col track-col-artist">
                  <MarqueeText className="marquee-left" always={track.id === currentTrackId}>
                    <LibraryLinks
                      part="artist"
                      artist={track.artist}
                      artists={track.artists}
                      onArtist={onOpenArtist}
                      disableArtist={disableArtistLink}
                    />
                  </MarqueeText>
                </span>
                <span className="track-col track-col-album">
                  {track.album ? (
                    <MarqueeText className="marquee-left" always={track.id === currentTrackId}>
                      <LibraryLinks
                        part="album"
                        artist={track.artist}
                        albumArtist={track.album_artist}
                        album={track.album}
                        onAlbum={onOpenAlbum}
                        disableAlbum={disableAlbumLink}
                      />
                    </MarqueeText>
                  ) : null}
                </span>
                {statsShown && (
                  <>
                    <span className="track-col track-col-num track-col-year">
                      <span className="visually-hidden">Year </span>
                      {numberCell(track.year)}
                    </span>
                    <span className="track-col track-col-num track-col-plays">
                      <span className="visually-hidden">Plays </span>
                      {numberCell(track.play_count)}
                    </span>
                  </>
                )}
                <span className="track-col track-col-time">{formatDuration(track.duration_seconds)}</span>
              </>
            )}

            <PreferenceHeart
              state={trackPreference(track)}
              onChange={(next) => cyclePreference(track, next)}
              subject={track.title}
              className="favorite-btn"
              activeClassName="favorite-active"
            />

            <div className="track-menu">
              <button
                onClick={(e) => {
                  if (openMenuFor === key) {
                    closeMenu()
                  } else {
                    // A keyboard "click" has no pointer position (clientX is 0):
                    // open at the button instead.
                    const rect = e.currentTarget.getBoundingClientRect()
                    setMenuAnchor(
                      e.detail === 0
                        ? { x: rect.left, y: rect.bottom + 4, trigger: e.currentTarget }
                        : { x: e.clientX, y: e.clientY, trigger: e.currentTarget },
                    )
                    setOpenMenuFor(key)
                  }
                }}
                aria-label={`More actions for ${track.title}`}
                {...menuTriggerProps(openMenuFor === key, menuId)}
              >
                ⋮
              </button>
              {openMenuFor === key && menuAnchor && (
                <TrackMenuPanel
                  track={track}
                  playlists={playlists}
                  isAdmin={!!user?.is_admin}
                  anchor={menuAnchor}
                  menuId={menuId}
                  onClose={closeMenu}
                  onViewLyrics={() => setLyricsFor(track)}
                  onEditTags={() => setEditingTagsFor([track])}
                  onSelect={() => toggleSelected(selectionKey(track))}
                  onPlaylistsChanged={onPlaylistsChanged}
                  onRemoveFromPlaylist={
                    playlistId != null && track.playlist_item_id != null
                      ? () => removeFromPlaylist(track)
                      : undefined
                  }
                />
              )}
            </div>
          </li>
          )
        })}
        {bottomPad > 0 && <li className="track-row-spacer" style={{ height: bottomPad }} aria-hidden />}
        {lyricsFor && (
          <Suspense fallback={null}>
            <LyricsPanel track={lyricsFor} onClose={() => setLyricsFor(null)} onOpenArtist={onOpenArtist} />
          </Suspense>
        )}
        {editingTagsFor && editingTagsFor.length > 0 && (
          <Suspense fallback={null}>
          <TagEditModal
            items={editingTagsFor}
            onClose={() => setEditingTagsFor(null)}
            onSaved={() => {
              setEditingTagsFor(null)
              setSelected(new Set())
              onChanged()
            }}
            onPartiallySaved={(failedIds) => {
              // Some files changed: show their new tags, and keep only the
              // ones that did not selected so the edit can be tried again.
              const failed = new Set(failedIds)
              setSelected(new Set(visibleTracks.filter((track) => failed.has(track.id)).map(selectionKey)))
              onChanged()
            }}
          />
          </Suspense>
        )}
      </ul>
      {loadingMore && (
        <div className="loading-more" role="status" aria-live="polite">Loading more songs…</div>
      )}
    </>
  )
}
