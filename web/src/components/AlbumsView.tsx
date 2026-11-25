import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { errorMessage } from '../errorMessage'
import { trackCountLabel } from '../plural'
import { useDebouncedValue } from '../useDebouncedValue'
import { delay, useAsync } from '../useAsync'
import { useToast } from '../ToastContext'
import {
  type BrowseDirection,
  type BrowseViewMode,
  DEFAULT_SORT_DIRECTION,
  loadBrowsePrefs,
  saveBrowsePrefs,
} from '../browsePrefs'
import type { Album, BrowseSort, MediaItem, Playlist } from '../types'
import { useSettings } from '../SettingsContext'
import { GRID_LIST_ART_REMS } from '../settings'
import { useVirtualGridRange } from '../useVirtualGridRange'
import { useVirtualRange } from '../useVirtualRange'
import { useIsDesktop } from '../useIsDesktop'
import { AlphabetJumpBar, buildAlphabetIndex } from './AlphabetJumpBar'
import { Art } from './Art'
import { ErrorState, LoadingState } from './ErrorState'
import { CollectionDetailHeader } from './CollectionDetailHeader'
import { CollectionHeart } from './CollectionHeart'
import { BrowseToolbar, ViewModeChips } from './BrowseToolbar'
import { CollectionMenuPanel } from './CollectionMenuPanel'
import { RefDownloadMark } from '../native/DownloadMark'
import { MoreMenu, MoreButton } from './MoreMenu'
import { albumRef } from '../artEntity'
import { CollectionPlayActions } from './CollectionPlayActions'
import { LabelsButton } from './LabelsButton'
import { LibraryLinks } from './LibraryLinks'
import { groupAlbumSections } from '../albumSections'
import type { CatalogReleaseGroup } from '../wantList'
import { AlbumMissingTracks, MissingTiles, ShowMissingToggle, useMissingReleases } from './MissingReleases'
import { DiscIcon, SortIcon } from './Icons'
import { TrackList } from './TrackList'
import { useLongPressMenu } from '../useLongPressMenu'

const PAGE_SIZE = 100
const NO_ALBUMS: Album[] = []
const NO_TRACKS: MediaItem[] = []
const NO_GENRES: string[] = []

type ArtistAlbumSort = 'release' | 'alphabetical'

const ARTIST_ALBUM_SORT_KEY = 'atriolyric.browse.artist-albums'

function loadArtistAlbumSort(): ArtistAlbumSort {
  try {
    return localStorage.getItem(ARTIST_ALBUM_SORT_KEY) === 'alphabetical' ? 'alphabetical' : 'release'
  } catch {
    return 'release'
  }
}

/** The album hero's fact pills: release year, track count, length, the
 * audio format when every track shares one, and the genre. */
function albumFacts(album: Album | undefined, tracks: MediaItem[]): string[] {
  const facts: string[] = []
  if (album?.release_date) facts.push(`Released ${album.release_date.slice(0, 4)}`)
  facts.push(trackCountLabel(tracks.length || album?.track_count || 0))
  const seconds = tracks.reduce((sum, track) => sum + (track.duration_seconds ?? 0), 0)
  if (seconds > 0) facts.push(seconds >= 3600 ? `${Math.floor(seconds / 3600)} h ${Math.round((seconds % 3600) / 60)} min` : `${Math.max(1, Math.round(seconds / 60))} min`)
  const formats = new Set(tracks.map((track) => track.format_label).filter(Boolean))
  if (formats.size === 1) facts.push([...formats][0] as string)
  const genre = tracks.find((track) => track.genre)?.genre
  if (genre) facts.push(genre)
  return facts
}

export interface SelectedAlbum {
  artist: string | null
  album: string
}

interface Props {
  /** Search text to start with (Home's "View full search"). */
  initialSearch?: string
  playlists: Playlist[]
  artistFilter?: string
  selectedAlbum: SelectedAlbum | null
  onSelectAlbum: (album: SelectedAlbum | null) => void
  onPlaylistsChanged?: () => void
  onOpenArtist?: (artist: string) => void
  onOpenAlbum?: (artist: string | null, album: string) => void
  onOpenLabel?: (label: { genre?: string; tag?: string }) => void
  scrollContainerRef?: React.RefObject<HTMLElement | null>
  /** Shown above an artist's albums (the artist page's Top tracks). */
  artistExtras?: React.ReactNode
  /** Open a release the library doesn't have (the Want list's catalog). */
  onOpenCatalogAlbum?: (releaseGroupMbid: string, artist: string) => void
}

export function AlbumsView({
  playlists,
  artistFilter,
  selectedAlbum,
  onSelectAlbum,
  onPlaylistsChanged,
  onOpenArtist,
  onOpenAlbum,
  onOpenLabel,
  scrollContainerRef,
  initialSearch = '',
  artistExtras,
  onOpenCatalogAlbum,
}: Props) {
  const { settings } = useSettings()
  const isDesktop = useIsDesktop()
  const gridSize = isDesktop ? settings.gridSizeDesktop : settings.gridSizeMobile
  // First-paint estimate only - useVirtualRange measures the real row
  // height once one renders.
  // Compact density shrinks the art to 0.75x and the padding (albums-artists.css).
  const compact = settings.listDensity === 'compact'
  const listRowHeight = Math.ceil(GRID_LIST_ART_REMS[gridSize] * 16 * (compact ? 0.75 : 1) + (compact ? 6 : 16))
  const { showToast } = useToast()
  const [loadingMore, setLoadingMore] = useState(false)
  // An owned artist's releases the library is missing (Show missing).
  const missing = useMissingReleases(artistFilter)
  // Only ever set by Home's "View full search" (a chip in the toolbar shows
  // it, and removing the chip clears it): the tab has no search box.
  const [typedSearch, setSearch] = useState(initialSearch)
  const search = useDebouncedValue(typedSearch)
  const [genre, setGenre] = useState('')
  const initialPrefs = useMemo(() => loadBrowsePrefs('albums'), [])
  const [sort, setSort] = useState<BrowseSort>(initialPrefs.sort)
  const [direction, setDirection] = useState<BrowseDirection>(initialPrefs.direction)
  const [viewMode, setViewMode] = useState<BrowseViewMode>(initialPrefs.viewMode)
  const [listNode, setListNode] = useState<HTMLUListElement | null>(null)
  const menu = useLongPressMenu<string>()
  // The albums-by-one-artist drill-down has its own sort (newest release
  // first by default) that's stored separately, so it never touches the
  // top-level Albums tab's sort preference.
  const [artistSort, setArtistSort] = useState<ArtistAlbumSort>(loadArtistAlbumSort)

  function albumKey(album: Album) {
    return `${album.artist ?? ''}::${album.album}`
  }

  function albumFilter(album: { artist: string | null; album: string }) {
    return {
      artist: album.artist ?? undefined,
      artist_is_null: album.artist == null,
      album: album.album,
    }
  }

  /** The owner line under an album: links (or the multi-artist picker)
   * when there's somewhere to go. Tiles render it beside their button, not
   * inside it - a link can't sit inside a button. List rows keep it in the
   * row as plain text, the row being one big target. */
  function albumSubtitle(album: Album, inRow = false) {
    if (inRow || !onOpenArtist) {
      return <div className="browse-subtitle">{album.artist ?? 'Unknown artist'}</div>
    }
    return (
      <div className="browse-subtitle browse-subtitle-links">
        <LibraryLinks artist={album.artist} artists={album.artists} onArtist={onOpenArtist} />
      </div>
    )
  }

  function albumMenu(album: Album) {
    const key = albumKey(album)
    return menu.openFor === key && menu.anchor ? (
      <CollectionMenuPanel
        label={`${album.album} · ${album.artist ?? 'Unknown artist'}`}
        filter={albumFilter(album)}
        playlists={playlists}
        artRef={albumRef(album)}
        anchor={menu.anchor}
        menuId={menu.menuId}
        onClose={menu.close}
        onPlaylistsChanged={onPlaylistsChanged}
      />
    ) : null
  }

  // A device preference like theme/tile size - kept in localStorage so it
  // survives navigating away and back.
  useEffect(() => {
    saveBrowsePrefs('albums', { sort, direction, viewMode })
  }, [sort, direction, viewMode])

  // An album page opened from the Albums tab needs none of the grid's data:
  // the list, its genres and the warm-up below wait until Back shows the grid
  // (what was already loaded stays). An artist's albums are the artist page.
  const gridHidden = selectedAlbum != null && !artistFilter
  const genresState = useAsync(() => api.listGenres(), [artistFilter], { enabled: !artistFilter && !gridHidden })
  const genres = genresState.data ?? NO_GENRES

  // What the loaded list belongs to. Everything below is keyed on it, so a
  // different search, sort or artist never shows the previous one's rows.
  const filterParams = { q: search || undefined, genre: genre || undefined, sort, direction }
  const listKey = JSON.stringify([artistFilter ?? null, artistSort, search, genre, sort, direction])
  // The top-level list keeps the previous rows on screen (grid mounted) while a
  // search or sort refetches; an artist's albums never do, so artist A's
  // discography is not shown under artist B's name.
  const first = useAsync(
    () =>
      artistFilter
        ? api.listAlbumsFull(
            artistSort === 'release'
              ? { artist: artistFilter, sort: 'date_published', direction: 'desc' }
              : { artist: artistFilter, sort: 'alphabetical', direction: 'asc' },
          )
        : api.listAlbums({ ...filterParams, limit: PAGE_SIZE }),
    [listKey],
    { keepPrevious: !artistFilter, enabled: !gridHidden },
  )
  // Once the first page has rendered, quietly fetch the whole matching list
  // and swap it in so every letter of the alphabet jump bar is reachable. A
  // short delay keeps it off the first page's critical path; until it lands,
  // scrolling still pages as a fallback.
  const full = useAsync(
    async (signal) => {
      await delay(100, signal)
      return api.listAlbumsFull(filterParams)
    },
    [listKey],
    { enabled: !artistFilter && !gridHidden && first.status === 'ready' },
  )
  const [more, setMore] = useState<{ key: string; items: Album[]; hasMore: boolean } | null>(null)
  const moreForList = more?.key === listKey ? more : null
  const firstItems = first.data ?? NO_ALBUMS
  const albums = full.data ?? (moreForList ? [...firstItems, ...moreForList.items] : firstItems)
  const hasMore =
    !artistFilter && full.data === undefined && (moreForList ? moreForList.hasMore : firstItems.length === PAGE_SIZE)
  const loading = first.data === undefined && first.status !== 'error'
  const loadError = first.data === undefined && first.status === 'error'

  const appearsOnState = useAsync(
    () =>
      api
        .listAlbumsFull(
          artistSort === 'release'
            ? { artist: artistFilter, appears_on: true, sort: 'date_published', direction: 'desc' }
            : { artist: artistFilter, appears_on: true, sort: 'alphabetical', direction: 'asc' },
        )
        // Best-effort: the artist's own albums are what the page is for.
        .catch(() => [] as Album[]),
    [artistFilter, artistSort],
    { enabled: Boolean(artistFilter) },
  )
  const appearsOn = appearsOnState.data ?? NO_ALBUMS

  function loadMore() {
    if (artistFilter || loadingMore || !hasMore || first.status !== 'ready') return
    const key = listKey
    const offset = firstItems.length + (moreForList?.items.length ?? 0)
    setLoadingMore(true)
    api.listAlbums({ ...filterParams, limit: PAGE_SIZE, offset })
      .then((page) =>
        setMore((previous) => ({
          key,
          items: [...(previous?.key === key ? previous.items : []), ...page],
          hasMore: page.length === PAGE_SIZE,
        })),
      )
      .catch(() => showToast('Could not load more albums.', 'error'))
      .finally(() => setLoadingMore(false))
  }

  // Once the top-level Albums list has rendered, warm the matching default
  // Artists list in the background. This turns the common first
  // Albums-to-Artists switch into a cache hit without delaying Albums.
  useEffect(() => {
    if (artistFilter || gridHidden || loading || search || genre || sort !== 'alphabetical') return
    const timer = window.setTimeout(() => api.prefetchArtistsFull(), 250)
    return () => window.clearTimeout(timer)
  }, [artistFilter, gridHidden, loading, search, genre, sort])

  // The open album's header (art, artists, release date, its MusicBrainz
  // match): from the list when it is already loaded (the grid, an artist's
  // albums), otherwise one album's summary.
  const listedAlbum = selectedAlbum
    ? [...albums, ...appearsOn].find((album) => album.artist === selectedAlbum.artist && album.album === selectedAlbum.album)
    : undefined
  // An artist's own lists are coming anyway: wait for them first.
  const listsSettled = !artistFilter || (first.status !== 'loading' && appearsOnState.status !== 'loading')
  const summaryState = useAsync(
    () => api.albumSummaries(selectedAlbum?.artist ?? null, selectedAlbum?.album ?? ''),
    [selectedAlbum?.artist ?? null, selectedAlbum?.album],
    { enabled: selectedAlbum != null && listedAlbum === undefined && listsSettled },
  )
  const headerLoading =
    listedAlbum === undefined && (!listsSettled || (summaryState.status === 'loading' && summaryState.data === undefined))

  // Tracks of the open album, keyed on the album itself: switching albums
  // clears them at once, so album A's rows never sit under album B's header
  // (and a tap on one cannot play the wrong album).
  const tracksState = useAsync(
    () =>
      api.listTracks({
        artist: selectedAlbum?.artist ?? undefined,
        artist_is_null: selectedAlbum?.artist == null,
        album: selectedAlbum?.album,
      }),
    [selectedAlbum?.artist ?? null, selectedAlbum?.album],
    { enabled: Boolean(selectedAlbum) },
  )
  const tracks = tracksState.data ?? NO_TRACKS
  const refreshOpenAlbum = tracksState.reload

  // The server orders the list by `sort` and `direction` (desc = the biggest /
  // newest / most played first, which the down arrow shows), so it is shown as is.
  const displayAlbums = albums

  // Clicking the sort that's already active flips direction; picking a
  // different one starts from its default direction.
  function handleSortPick(picked: BrowseSort) {
    if (picked === sort) {
      setDirection((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSort(picked)
      setDirection(DEFAULT_SORT_DIRECTION[picked])
    }
  }

  // A library with thousands of albums must never render a tile for every
  // one of them at once - only the rows actually near the viewport do.
  // See useVirtualGridRange. Both hooks run every render regardless of
  // viewMode (React hooks can't be called conditionally) - whichever mode
  // isn't active just never gets its container node attached, so its
  // effects no-op. The nested "albums by this one artist" drill-down
  // (artistFilter set) has its own view-mode toggle further down, sharing
  // the same `viewMode` preference as the top-level Albums tab.
  const gridRange = useVirtualGridRange(displayAlbums.length, { scrollContainerRef, onEndReached: loadMore })
  const listRange = useVirtualRange(displayAlbums.length, {
    itemHeight: listRowHeight,
    containerNode: listNode,
    scrollContainerRef,
    onEndReached: loadMore,
  })
  const { start, end, topPad, bottomPad, scrollToIndex } = viewMode === 'grid' ? gridRange : listRange

  // Only meaningful (and only ever shown) when sorted alphabetically -
  // jumping to "M" doesn't mean anything once the order is by play count.
  // Keyed on the album title, which is what the server's alphabetical
  // album order sorts by first - the index and the order have to agree
  // or the letters land in the wrong place.
  const alphabetIndex = useMemo(
    () =>
      sort === 'alphabetical'
        ? buildAlphabetIndex(displayAlbums.map((a) => a.album))
        : new Map<string, number>(),
    [displayAlbums, sort],
  )

  if (selectedAlbum) {
    const albumInfo = listedAlbum ?? summaryState.data?.[0]
    return (
      <div>
        <CollectionDetailHeader
          backLabel={artistFilter ?? 'Albums'}
          onBack={() => onSelectAlbum(null)}
          title={`${selectedAlbum.album} · ${selectedAlbum.artist ?? 'Unknown artist'}`}
          heading={selectedAlbum.album}
          eyebrow="Album"
          subtitle={
            <LibraryLinks
              artist={selectedAlbum.artist}
              artists={albumInfo?.artists}
              onArtist={onOpenArtist}
            />
          }
          facts={albumFacts(albumInfo, tracks)}
          representativeMediaItemId={albumInfo?.representative_media_item_id}
          artRef={albumRef(albumInfo)}
          fallback={<DiscIcon />}
          actions={
            <>
              <CollectionPlayActions
                filter={albumFilter(selectedAlbum)}
                playSource={{
                  kind: 'album',
                  label: `${selectedAlbum.album} · ${selectedAlbum.artist ?? 'Unknown artist'}`,
                  payload: albumFilter(selectedAlbum),
                }}
              />
              <CollectionHeart
                kind="album"
                artist={selectedAlbum.artist}
                album={selectedAlbum.album}
                onChanged={refreshOpenAlbum}
              />
              <LabelsButton
                title={`${selectedAlbum.album} · genres/tags`}
                target={{ artist: selectedAlbum.artist, album: selectedAlbum.album }}
                onPick={onOpenLabel}
              />
              {(albumInfo?.representative_media_item_id ?? tracks[0]?.id) != null && (
                <MoreMenu label={selectedAlbum.album}>
                  {(anchor, close, menuId) => (
                    <CollectionMenuPanel
                      label={`${selectedAlbum.album} · ${selectedAlbum.artist ?? 'Unknown artist'}`}
                      filter={albumFilter(selectedAlbum)}
                      playlists={playlists}
                      artRef={albumRef(albumInfo)}
                      anchor={anchor}
                      menuId={menuId}
                      onClose={close}
                      onPlaylistsChanged={onPlaylistsChanged}
                    />
                  )}
                </MoreMenu>
              )}
            </>
          }
        />
        <div className="page-toolbar browse-nested-toolbar">
          <span className="page-toolbar-gap" />
          <ShowMissingToggle />
        </div>
        {tracksState.status === 'error' && tracksState.data === undefined ? (
          <ErrorState message={errorMessage(tracksState.error, 'Could not load this album.')} onRetry={tracksState.retry} />
        ) : tracksState.data === undefined ? (
          <LoadingState />
        ) : tracks.length === 0 ? (
          <p className="empty">Nothing here yet.</p>
        ) : (
          <TrackList
            tracks={tracks}
            playlists={playlists}
            onChanged={refreshOpenAlbum}
            onPlaylistsChanged={onPlaylistsChanged}
            onOpenArtist={onOpenArtist}
            onOpenAlbum={onOpenAlbum}
            disableAlbumLink
            playSource={{
              kind: 'album',
              label: `${selectedAlbum.album} · ${selectedAlbum.artist ?? 'Unknown artist'}`,
              payload: { artist: selectedAlbum.artist, album: selectedAlbum.album },
            }}
          />
        )}
        <AlbumMissingTracks
          releaseGroupMbid={albumInfo?.release_group_mbid}
          artist={selectedAlbum.artist}
          headerLoading={headerLoading}
        />
      </div>
    )
  }

  if (artistFilter) {
    const sections = groupAlbumSections(albums, settings.artistAlbumSections, missing.bySection)
    const renderAlbums = (list: Album[], extra: CatalogReleaseGroup[] = []) =>
      viewMode === 'grid' || extra.length > 0 ? (
        <div className="browse-grid">
          {list.map((album) => (
            <div key={albumKey(album)} className="more-card-wrap">
              <button
                className="browse-card browse-card-album"
                onClick={() => { if (!menu.consumeClick()) onSelectAlbum({ artist: album.artist, album: album.album }) }}
                onContextMenu={(e) => menu.onContextMenu(e, albumKey(album))}
                onPointerDown={(e) => menu.onPointerDown(e, albumKey(album))}
                onPointerUp={menu.onPointerUp}
                onPointerLeave={menu.onPointerLeave}
                onPointerCancel={menu.onPointerCancel}
              >
                <Art artRef={albumRef(album)} mediaItemId={album.representative_media_item_id} className="art-tile" fallback={<DiscIcon />} />
                <div className="browse-title">{album.album} <RefDownloadMark artRef={albumRef(album)} subject={album.album} /></div>
              </button>
              {albumSubtitle(album)}
              <MoreButton label={album.album} open={menu.openFor === albumKey(album)} menuId={menu.menuId} onOpen={(a) => menu.open(albumKey(album), a.x, a.y, a.trigger)} />
              {albumMenu(album)}
            </div>
          ))}
          {missing.catalog && extra.length > 0 && (
            <MissingTiles
              groups={extra}
              catalog={missing.catalog}
              artist={artistFilter}
              onOpenCatalogAlbum={(mbid, artist) => onOpenCatalogAlbum?.(mbid, artist)}
            />
          )}
        </div>
      ) : (
        <ul className="browse-list">
          {list.map((album) => (
            <li key={albumKey(album)} className="more-row-wrap">
              <button
                className="browse-list-row"
                onClick={() => { if (!menu.consumeClick()) onSelectAlbum({ artist: album.artist, album: album.album }) }}
                onContextMenu={(e) => menu.onContextMenu(e, albumKey(album))}
                onPointerDown={(e) => menu.onPointerDown(e, albumKey(album))}
                onPointerUp={menu.onPointerUp}
                onPointerLeave={menu.onPointerLeave}
                onPointerCancel={menu.onPointerCancel}
              >
                <Art artRef={albumRef(album)} mediaItemId={album.representative_media_item_id} className="art-sm appearance-list-art" fallback={<DiscIcon />} />
                <div className="browse-list-text">
                  <div className="browse-title">{album.album} <RefDownloadMark artRef={albumRef(album)} subject={album.album} /></div>
                  <div className="browse-subtitle">
                    {album.release_date ? `${album.release_date.slice(0, 4)} · ` : ''}
                    {album.track_count} track{album.track_count === 1 ? '' : 's'}
                  </div>
                </div>
              </button>
              <MoreButton label={album.album} open={menu.openFor === albumKey(album)} menuId={menu.menuId} onOpen={(a) => menu.open(albumKey(album), a.x, a.y, a.trigger)} />
              {albumMenu(album)}
            </li>
          ))}
        </ul>
      )
    // An artist's discography is small enough to render whole, so these
    // sections aren't virtualized like the top-level Albums tab.
    return (
      <div>
        {(albums.length > 0 || appearsOn.length > 0) && (
          <div className="page-toolbar browse-nested-toolbar">
            <button
              type="button"
              className="chip browse-sort-pill"
              onClick={() => {
                const next: ArtistAlbumSort = artistSort === 'release' ? 'alphabetical' : 'release'
                setArtistSort(next)
                try {
                  localStorage.setItem(ARTIST_ALBUM_SORT_KEY, next)
                } catch {
                  // Not persisting is fine; the choice still applies for this visit.
                }
              }}
              aria-label={
                artistSort === 'release'
                  ? 'Sorted by release date, newest first. Switch to alphabetical'
                  : 'Sorted alphabetically. Switch to release date'
              }
            >
              <SortIcon aria-hidden="true" />
              {artistSort === 'release' ? 'Newest first' : 'A–Z'}
            </button>
            <ViewModeChips viewMode={viewMode} onViewModeChange={setViewMode} />
            <span className="page-toolbar-gap" />
            <ShowMissingToggle />
          </div>
        )}
        {artistExtras}
        {loadError ? (
          <ErrorState message={errorMessage(first.error, 'Could not load these albums.')} onRetry={first.retry} />
        ) : loading ? (
          <LoadingState />
        ) : albums.length === 0 && appearsOn.length === 0 && missing.bySection.size === 0 ? (
          <p className="empty">No albums yet.</p>
        ) : (
          <>
            {sections.map(({ key, heading, albums: list, extra }) => (
              <section key={key || 'all'} className="artist-album-section" aria-label={heading || undefined}>
                {heading && <h3 className="artist-section-heading">{heading}</h3>}
                {renderAlbums(list, extra)}
              </section>
            ))}
            {missing.loading && <p className="missing-section-note">Looking up what's missing on MusicBrainz…</p>}
            {missing.error && <p className="missing-section-note">{missing.error}</p>}
            {appearsOn.length > 0 && (
              <section className="artist-album-section" aria-label="Appears on">
                <h3 className="artist-section-heading">Appears on</h3>
                {renderAlbums(appearsOn)}
              </section>
            )}
          </>
        )}
      </div>
    )
  }

  const jumpBar =
    sort === 'alphabetical' && !loading && !loadError ? (
      <AlphabetJumpBar firstIndexByLetter={alphabetIndex} onJump={scrollToIndex} />
    ) : null
  return (
    <div className="browse-page">
      <BrowseToolbar
        genre={genre}
        onGenreChange={setGenre}
        genres={genres}
        search={typedSearch}
        onClearSearch={() => setSearch('')}
        sort={sort}
        direction={direction}
        onSortPick={handleSortPick}
        viewMode={viewMode}
        onViewModeChange={setViewMode}
        jump={isDesktop ? jumpBar : undefined}
      />
      {/* Letters in the toolbar on a desktop-width screen (above); a rail on
          the right on a phone (a zero-height sticky box right after the
          toolbar, so it never narrows the grid). */}
      {!isDesktop && jumpBar}
      {loadError ? (
        <ErrorState message={errorMessage(first.error, 'Could not load your albums.')} onRetry={first.retry} />
      ) : loading ? (
        <LoadingState />
      ) : displayAlbums.length === 0 ? (
        <p className="empty">No albums yet.</p>
      ) : (
        <div className="browse-with-jump-bar browse-page-list">
          {viewMode === 'grid' ? (
            <div className="browse-grid" ref={gridRange.setContainerNode}>
              {topPad > 0 && <div className="browse-grid-spacer" style={{ height: topPad }} aria-hidden />}
            {displayAlbums.slice(start, end).map((album) => (
                <div key={albumKey(album)} className="more-card-wrap">
                <button
                  key={albumKey(album)}
                  className="browse-card browse-card-album"
                  onClick={() => { if (!menu.consumeClick()) onSelectAlbum({ artist: album.artist, album: album.album }) }}
                  onContextMenu={(e) => menu.onContextMenu(e, albumKey(album))}
                  onPointerDown={(e) => menu.onPointerDown(e, albumKey(album))}
                  onPointerUp={menu.onPointerUp}
                  onPointerLeave={menu.onPointerLeave}
                  onPointerCancel={menu.onPointerCancel}
                >
                  <Art artRef={albumRef(album)} mediaItemId={album.representative_media_item_id} className="art-tile" fallback={<DiscIcon />} />
                  <div className="browse-title">{album.album} <RefDownloadMark artRef={albumRef(album)} subject={album.album} /></div>
                </button>
                {albumSubtitle(album)}
                <MoreButton label={album.album} open={menu.openFor === albumKey(album)} menuId={menu.menuId} onOpen={(a) => menu.open(albumKey(album), a.x, a.y, a.trigger)} />
{albumMenu(album)}
                </div>
              ))}
              {bottomPad > 0 && (
                <div className="browse-grid-spacer" style={{ height: bottomPad }} aria-hidden />
              )}
            </div>
          ) : (
            <ul className="browse-list" ref={setListNode}>
              {topPad > 0 && <li className="browse-list-spacer" style={{ height: topPad }} aria-hidden />}
              {displayAlbums.slice(start, end).map((album) => (
                <li key={albumKey(album)} className="more-row-wrap">
                <button
                  className="browse-list-row"
                  onClick={() => { if (!menu.consumeClick()) onSelectAlbum({ artist: album.artist, album: album.album }) }}
                  onContextMenu={(e) => menu.onContextMenu(e, albumKey(album))}
                  onPointerDown={(e) => menu.onPointerDown(e, albumKey(album))}
                  onPointerUp={menu.onPointerUp}
                  onPointerLeave={menu.onPointerLeave}
                  onPointerCancel={menu.onPointerCancel}
                  >
                    <Art artRef={albumRef(album)} mediaItemId={album.representative_media_item_id} className="art-sm appearance-list-art" fallback={<DiscIcon />} />
                    <div className="browse-list-text">
                      <div className="browse-title">{album.album} <RefDownloadMark artRef={albumRef(album)} subject={album.album} /></div>
                      {albumSubtitle(album, true)}
                    </div>
                </button>
                <MoreButton label={album.album} open={menu.openFor === albumKey(album)} menuId={menu.menuId} onOpen={(a) => menu.open(albumKey(album), a.x, a.y, a.trigger)} />
{albumMenu(album)}
                </li>
              ))}
              {bottomPad > 0 && (
                <li className="browse-list-spacer" style={{ height: bottomPad }} aria-hidden />
              )}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
