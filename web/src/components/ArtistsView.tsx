import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { errorMessage } from '../errorMessage'
import { albumCountLabel, trackCountLabel } from '../plural'
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
import { useSettings } from '../SettingsContext'
import { GRID_LIST_ART_REMS } from '../settings'
import type { Artist, BrowseSort, Playlist } from '../types'
import { useVirtualGridRange } from '../useVirtualGridRange'
import { useVirtualRange } from '../useVirtualRange'
import { useIsDesktop } from '../useIsDesktop'
import { AlbumsView, type SelectedAlbum } from './AlbumsView'
import { AlphabetJumpBar, buildAlphabetIndex } from './AlphabetJumpBar'
import { Art } from './Art'
import { ErrorState, LoadingState } from './ErrorState'
import { CollectionDetailHeader } from './CollectionDetailHeader'
import { LabelsButton } from './LabelsButton'
import { CollectionHeart } from './CollectionHeart'
import { BrowseToolbar } from './BrowseToolbar'
import { CollectionMenuPanel } from './CollectionMenuPanel'
import { RefDownloadMark } from '../native/DownloadMark'
import { MoreMenu, MoreButton } from './MoreMenu'
import { artistRef } from '../artEntity'
import { CollectionPlayActions } from './CollectionPlayActions'
import { TopTracks } from './TopTracks'
import { useLongPressMenu } from '../useLongPressMenu'
import { MicIcon } from './Icons'

const PAGE_SIZE = 100
const NO_ARTISTS: Artist[] = []
const NO_GENRES: string[] = []

interface Props {
  /** Search text to start with (Home's "View full search"). */
  initialSearch?: string
  playlists: Playlist[]
  selectedArtist: string | null
  onSelectArtist: (artist: string | null) => void
  selectedAlbum: SelectedAlbum | null
  onSelectAlbum: (album: SelectedAlbum | null) => void
  onPlaylistsChanged?: () => void
  onOpenLabel?: (label: { genre?: string; tag?: string }) => void
  scrollContainerRef?: React.RefObject<HTMLElement | null>
  /** Open a release the library doesn't have (the Want list's catalog). */
  onOpenCatalogAlbum?: (releaseGroupMbid: string, artist: string) => void
}

export function ArtistsView({
  onOpenCatalogAlbum,
  playlists,
  selectedArtist,
  onSelectArtist,
  selectedAlbum,
  onSelectAlbum,
  onPlaylistsChanged,
  onOpenLabel,
  scrollContainerRef,
  initialSearch = '',
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
  // Only ever set by Home's "View full search" (a chip in the toolbar shows
  // it, and removing the chip clears it): the tab has no search box.
  const [typedSearch, setSearch] = useState(initialSearch)
  const search = useDebouncedValue(typedSearch)
  const [genre, setGenre] = useState('')
  const initialPrefs = useMemo(() => loadBrowsePrefs('artists'), [])
  const [sort, setSort] = useState<BrowseSort>(initialPrefs.sort)
  const [direction, setDirection] = useState<BrowseDirection>(initialPrefs.direction)
  const [viewMode, setViewMode] = useState<BrowseViewMode>(initialPrefs.viewMode)
  const [listNode, setListNode] = useState<HTMLUListElement | null>(null)
  const menu = useLongPressMenu<string>()

  // A device preference like theme/tile size - kept in localStorage so it
  // survives navigating away and back.
  useEffect(() => {
    saveBrowsePrefs('artists', { sort, direction, viewMode })
  }, [sort, direction, viewMode])

  // An artist page needs none of the grid's data: the artist list, its genres
  // and the warm-up below wait until Back shows the grid (what was already
  // loaded stays on hand).
  const gridHidden = selectedArtist != null
  const genresState = useAsync(() => api.listGenres(), [], { enabled: !gridHidden })
  const genres = genresState.data ?? NO_GENRES

  // Everything below belongs to this search/sort; see the matching block in
  // AlbumsView. The previous rows stay on screen while a refetch is in flight,
  // so the grid is not swapped out and back in (which would leave
  // useVirtualGridRange measuring a detached container).
  const filterParams = { q: search || undefined, genre: genre || undefined, sort, direction }
  const listKey = JSON.stringify([search, genre, sort, direction])
  const first = useAsync(() => api.listArtists({ ...filterParams, limit: PAGE_SIZE }), [listKey], {
    keepPrevious: true,
    enabled: !gridHidden,
  })
  // Then the whole matching list is swapped in (after a short delay, off the
  // first page's critical path) so every letter of the jump bar is reachable.
  const full = useAsync(
    async (signal) => {
      await delay(100, signal)
      return api.listArtistsFull(filterParams)
    },
    [listKey],
    { enabled: !gridHidden && first.status === 'ready' },
  )
  const [more, setMore] = useState<{ key: string; items: Artist[]; hasMore: boolean } | null>(null)
  const moreForList = more?.key === listKey ? more : null
  const firstItems = first.data ?? NO_ARTISTS
  const artists = full.data ?? (moreForList ? [...firstItems, ...moreForList.items] : firstItems)
  const hasMore = full.data === undefined && (moreForList ? moreForList.hasMore : firstItems.length === PAGE_SIZE)
  const loading = first.data === undefined && first.status !== 'error'
  const loadError = first.data === undefined && first.status === 'error'

  function loadMore() {
    if (loadingMore || !hasMore || first.status !== 'ready') return
    const key = listKey
    const offset = firstItems.length + (moreForList?.items.length ?? 0)
    setLoadingMore(true)
    api.listArtists({ ...filterParams, limit: PAGE_SIZE, offset })
      .then((page) =>
        setMore((previous) => ({
          key,
          items: [...(previous?.key === key ? previous.items : []), ...page],
          hasMore: page.length === PAGE_SIZE,
        })),
      )
      .catch(() => showToast('Could not load more artists.', 'error'))
      .finally(() => setLoadingMore(false))
  }

  // See the reciprocal warm-up in AlbumsView. Wait until this grid is on
  // screen before using any spare bandwidth for its counterpart.
  useEffect(() => {
    if (gridHidden || loading || search || genre || sort !== 'alphabetical') return
    const timer = window.setTimeout(() => api.prefetchAlbumsFull(), 250)
    return () => window.clearTimeout(timer)
  }, [gridHidden, loading, search, genre, sort])

  // An artist opened from a link (a featured artist, Home, the Want list) may
  // not be in the loaded page: look it up rather than show "0 tracks · 0 albums".
  const listedArtist = selectedArtist ? artists.find((artist) => artist.artist === selectedArtist) : undefined
  const summaryState = useAsync(
    () => api.artistSummaries([selectedArtist ?? '']),
    [selectedArtist],
    { enabled: Boolean(selectedArtist) && listedArtist === undefined },
  )

  // The server orders the list by `sort` and `direction` (desc = the biggest /
  // newest / most played first, which the down arrow shows), so it is shown as is.
  const displayArtists = artists

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

  // A library with thousands of artists must never render a tile for
  // every one of them at once - only the rows actually near the viewport
  // do. See useVirtualGridRange. Both hooks run every render regardless of
  // viewMode (React hooks can't be called conditionally) - whichever mode
  // isn't active just never gets its container node attached, so its
  // effects no-op.
  const gridRange = useVirtualGridRange(displayArtists.length, { scrollContainerRef, onEndReached: loadMore })
  const listRange = useVirtualRange(displayArtists.length, {
    itemHeight: listRowHeight,
    containerNode: listNode,
    scrollContainerRef,
    onEndReached: loadMore,
  })
  const { start, end, topPad, bottomPad, scrollToIndex } = viewMode === 'grid' ? gridRange : listRange

  // Only meaningful (and only ever shown) when sorted alphabetically -
  // jumping to "M" doesn't mean anything once the order is by play count.
  const alphabetIndex = useMemo(
    () =>
      sort === 'alphabetical'
        ? buildAlphabetIndex(displayArtists.map((a) => a.artist))
        : new Map<string, number>(),
    [displayArtists, sort],
  )

  if (selectedArtist) {
    const artistInfo = listedArtist ?? summaryState.data?.find((artist) => artist.artist === selectedArtist)
    // Until the lookup answers there are no counts to show, and a made-up
    // "0 tracks" would be wrong.
    const artistSubtitle = artistInfo
      ? `${trackCountLabel(artistInfo.track_count)} · ${albumCountLabel(artistInfo.album_count)}`
      : summaryState.status === 'loading'
        ? 'Loading…'
        : null
    return (
      <div>
        {!selectedAlbum && (
          <CollectionDetailHeader
              backLabel="Artists"
              onBack={() => onSelectArtist(null)}
              title={selectedArtist}
              eyebrow="Artist"
              round
              subtitle={artistSubtitle}
              representativeMediaItemId={artistInfo?.representative_media_item_id}
              artRef={artistRef(artistInfo)}
              fallback={<MicIcon />}
              actions={
                <>
                  <CollectionPlayActions
                    filter={{ artist: selectedArtist }}
                    playSource={{ kind: 'artist', label: selectedArtist, payload: { artist: selectedArtist } }}
                  />
                  <CollectionHeart kind="artist" artist={selectedArtist} />
                  <LabelsButton
                    title={`${selectedArtist} · genres/tags`}
                    target={{ artist: selectedArtist }}
                    onPick={onOpenLabel}
                  />
                  {artistInfo && (
                    <MoreMenu label={selectedArtist}>
                      {(anchor, close, menuId) => (
                        <CollectionMenuPanel
                          label={selectedArtist}
                          filter={{ artist: selectedArtist }}
                          playlists={playlists}
                          artRef={artistRef(artistInfo)}
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
        )}
        <AlbumsView
          playlists={playlists}
          artistFilter={selectedArtist}
          selectedAlbum={selectedAlbum}
          onSelectAlbum={onSelectAlbum}
          onPlaylistsChanged={onPlaylistsChanged}
          // One step (opening an artist leaves the album): closing the album
          // first would put a second entry in the history.
          onOpenArtist={onSelectArtist}
          onOpenAlbum={(artist, album) => onSelectAlbum({ artist, album })}
          onOpenLabel={onOpenLabel}
          scrollContainerRef={scrollContainerRef}
          artistExtras={<TopTracks key={selectedArtist} artist={selectedArtist} />}
          onOpenCatalogAlbum={onOpenCatalogAlbum}
        />
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
        <ErrorState message={errorMessage(first.error, 'Could not load your artists.')} onRetry={first.retry} />
      ) : loading ? (
        <LoadingState />
      ) : displayArtists.length === 0 ? (
        <p className="empty">No artists yet.</p>
      ) : (
        <div className="browse-with-jump-bar browse-page-list">
          {viewMode === 'grid' ? (
            <div className="browse-grid" ref={gridRange.setContainerNode}>
              {topPad > 0 && <div className="browse-grid-spacer" style={{ height: topPad }} aria-hidden />}
              {displayArtists.slice(start, end).map((artist) => (
                <div key={artist.artist} className="more-card-wrap">
                  <button
                  className="browse-card"
                  onClick={() => { if (!menu.consumeClick()) onSelectArtist(artist.artist) }}
                  onContextMenu={(e) => menu.onContextMenu(e, artist.artist)}
                  onPointerDown={(e) => menu.onPointerDown(e, artist.artist)}
                  onPointerUp={menu.onPointerUp}
                  onPointerLeave={menu.onPointerLeave}
                  onPointerCancel={menu.onPointerCancel}
                >
                  <Art
                    artRef={artistRef(artist)}
                    mediaItemId={artist.representative_media_item_id}
                    className="art-tile"
                    fallback={<MicIcon />}
                  />
                  <div className="browse-title">{artist.artist} <RefDownloadMark artRef={artistRef(artist)} subject={artist.artist} /></div>
                  <div className="browse-subtitle">
                    {artist.album_count} album{artist.album_count === 1 ? '' : 's'}
                  </div>
                  </button>
                <MoreButton label={artist.artist} open={menu.openFor === artist.artist} menuId={menu.menuId} onOpen={(a) => menu.open(artist.artist, a.x, a.y, a.trigger)} />
                {menu.openFor === artist.artist && menu.anchor && (
                  <CollectionMenuPanel
                    label={artist.artist}
                    filter={{ artist: artist.artist }}
                    playlists={playlists}
                    artRef={artistRef(artist)}
                    anchor={menu.anchor}
                    menuId={menu.menuId}
                    onClose={menu.close}
                    onPlaylistsChanged={onPlaylistsChanged}
                  />
                  )}
                </div>
              ))}
              {bottomPad > 0 && (
                <div className="browse-grid-spacer" style={{ height: bottomPad }} aria-hidden />
              )}
            </div>
          ) : (
            <ul className="browse-list" ref={setListNode}>
              {topPad > 0 && <li className="browse-list-spacer" style={{ height: topPad }} aria-hidden />}
              {displayArtists.slice(start, end).map((artist) => (
                <li key={artist.artist} className="more-row-wrap">
                  <button
                    className="browse-list-row"
                    onClick={() => { if (!menu.consumeClick()) onSelectArtist(artist.artist) }}
                    onContextMenu={(e) => menu.onContextMenu(e, artist.artist)}
                    onPointerDown={(e) => menu.onPointerDown(e, artist.artist)}
                    onPointerUp={menu.onPointerUp}
                    onPointerLeave={menu.onPointerLeave}
                    onPointerCancel={menu.onPointerCancel}
                  >
                    <Art artRef={artistRef(artist)} mediaItemId={artist.representative_media_item_id} className="art-sm appearance-list-art" fallback={<MicIcon />} />
                    <div className="browse-list-text">
                      <div className="browse-title">{artist.artist} <RefDownloadMark artRef={artistRef(artist)} subject={artist.artist} /></div>
                      <div className="browse-subtitle">
                        {artist.album_count} album{artist.album_count === 1 ? '' : 's'}
                      </div>
                    </div>
                  </button>
                  <MoreButton label={artist.artist} open={menu.openFor === artist.artist} menuId={menu.menuId} onOpen={(a) => menu.open(artist.artist, a.x, a.y, a.trigger)} />
                {menu.openFor === artist.artist && menu.anchor && (
                    <CollectionMenuPanel
                      label={artist.artist}
                      filter={{ artist: artist.artist }}
                      playlists={playlists}
                      artRef={artistRef(artist)}
                      anchor={menu.anchor}
                      menuId={menu.menuId}
                      onClose={menu.close}
                      onPlaylistsChanged={onPlaylistsChanged}
                    />
                  )}
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
