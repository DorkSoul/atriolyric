import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import {
  DEFAULT_SONGS_DIRECTION,
  loadSongsSort,
  loadSongsViewMode,
  saveSongsSort,
  saveSongsViewMode,
  type SongsSort,
  type SongsViewMode,
} from '../browsePrefs'
import { errorMessage } from '../errorMessage'
import { useSettings } from '../SettingsContext'
import type { MediaItem, Playlist, TrackSort } from '../types'
import { useDebouncedValue } from '../useDebouncedValue'
import { useIsDesktop } from '../useIsDesktop'
import { usePagedList } from '../usePagedList'
import { AlphabetJumpBar, buildAlphabetIndex, letterFor } from './AlphabetJumpBar'
import { SortMenu } from './BrowseToolbar'
import { GenreMenu } from './GenreMenu'
import { ErrorState, LoadingState } from './ErrorState'
import { CloseIcon, GridViewIcon, ListViewIcon } from './Icons'
import { SearchChip } from './SearchChip'
import { TrackList } from './TrackList'

interface Props {
  playlists: Playlist[]
  /** Applied by Home's "View full search" (shown as a removable chip). */
  search: string
  onClearSearch: () => void
  genre: string
  onGenreChange: (genre: string) => void
  genres: string[]
  tag: string
  onClearTag: () => void
  scrollContainerRef: React.RefObject<HTMLElement | null>
  onPlaylistsChanged: () => void
  onOpenArtist: (artist: string) => void
  onOpenAlbum: (artist: string | null, album: string) => void
}

const LETTERS = ['#', ...Array.from({ length: 26 }, (_, i) => String.fromCharCode(65 + i))]

// The sort button's names, in menu order; the menu spells out the default.
const SONGS_SORT_LABELS: Record<TrackSort, string> = {
  artist: 'A–Z',
  title: 'Title',
  album: 'Album',
  year: 'Year',
  date_added: 'Date added',
  most_played: 'Most played',
  last_played: 'Last played',
}
const SONGS_SORT_MENU_LABELS: Partial<Record<TrackSort, string>> = { artist: 'A–Z by artist' }

// What the A-Z jump goes by for each alphabetical sort (the field the server
// orders by first); the other sorts have no jump.
const JUMP_FIELD: Partial<Record<TrackSort, (track: MediaItem) => string | null>> = {
  artist: (track) => track.artist,
  title: (track) => track.title,
  album: (track) => track.album,
}

/** The Songs tab: the library's tracks, a page at a time, filtered by genre,
 * tag and (from Home's "View full search") a search, all kept in App, in the
 * sort picked here (kept per device). Above the list a toolbar holds the
 * filters, the sort and the Table / Cards and Compact choices; the A-Z jump
 * goes by the field an alphabetical sort orders by (artist, title, album)
 * and is hidden for the others. */
export function SongsView({
  playlists,
  search: typedSearch,
  onClearSearch,
  genre,
  onGenreChange,
  genres,
  tag,
  onClearTag,
  scrollContainerRef,
  onPlaylistsChanged,
  onOpenArtist,
  onOpenAlbum,
}: Props) {
  const { settings, updateSettings } = useSettings()
  const isDesktop = useIsDesktop()
  const [viewMode, setViewMode] = useState<SongsViewMode>(loadSongsViewMode)
  // A pause before following the search.
  const search = useDebouncedValue(typedSearch)
  const [{ sort, direction }, setSongsSort] = useState<SongsSort>(loadSongsSort)
  // The default order is asked for with no sort at all.
  const order = sort === 'artist' && direction === 'asc' ? undefined : { sort, direction }
  const fetchTracksPage = useCallback(
    (offset: number, limit: number) =>
      api.listTracks({
        q: search || undefined,
        genre: genre || undefined,
        tag: tag || undefined,
        ...(sort === 'artist' && direction === 'asc' ? {} : { sort, direction }),
        limit,
        offset,
      }),
    [search, genre, tag, sort, direction],
  )
  // A new sort starts over at page one (usePagedList drops a slower reply
  // for the old one).
  const tracksPage = usePagedList(fetchTracksPage, [search, genre, tag, sort, direction])
  const jumpRef = useRef<((index: number, offsetPx?: number) => void) | null>(null)
  const [pendingLetter, setPendingLetter] = useState<string | null>(null)

  function pickSort(picked: TrackSort) {
    // Picking the current sort again flips it, as on Albums and Artists.
    const next: SongsSort =
      picked === sort
        ? { sort, direction: direction === 'asc' ? 'desc' : 'asc' }
        : { sort: picked, direction: DEFAULT_SONGS_DIRECTION[picked] }
    setSongsSort(next)
    saveSongsSort(next)
    setPendingLetter(null)
    scrollContainerRef.current?.scrollTo({ top: 0 })
  }

  const jumpField = JUMP_FIELD[sort]
  const alphabetIndex = useMemo(
    () => (jumpField ? buildAlphabetIndex(tracksPage.items.map(jumpField)) : new Map<string, number>()),
    [tracksPage.items, jumpField],
  )
  // Letters past the last one loaded may still come with later pages: after
  // it A to Z, before it Z to A (where '#' comes last).
  const pendingLetters = useMemo(() => {
    if (!jumpField || !tracksPage.hasMore || tracksPage.items.length === 0) return undefined
    const last = LETTERS.indexOf(letterFor(jumpField(tracksPage.items[tracksPage.items.length - 1])))
    const ahead = direction === 'asc' ? LETTERS.slice(last + 1) : LETTERS.slice(0, last)
    return new Set(ahead.filter((letter) => !alphabetIndex.has(letter)))
  }, [jumpField, direction, tracksPage.hasMore, tracksPage.items, alphabetIndex])

  // A letter not loaded yet: read pages until it shows up (or the list ends).
  const { hasMore, loadingMore, loadMore } = tracksPage
  useEffect(() => {
    if (!pendingLetter) return
    const index = alphabetIndex.get(pendingLetter)
    if (index != null) {
      setPendingLetter(null)
      const row = document.querySelector('.songs-view .songs-toolbar')
      jumpRef.current?.(index, row?.getBoundingClientRect().height ?? 0)
      return
    }
    if (!hasMore) {
      setPendingLetter(null)
      return
    }
    if (!loadingMore) loadMore()
  }, [pendingLetter, alphabetIndex, hasMore, loadingMore, loadMore])

  function chooseView(next: SongsViewMode) {
    setViewMode(next)
    saveSongsViewMode(next)
  }

  const compact = settings.listDensity === 'compact'

  // Letters in the toolbar on a desktop-width screen, a rail on a phone.
  const jumpBar =
    jumpField && tracksPage.items.length > 0 && !tracksPage.loading && tracksPage.failure !== 'first' ? (
      <AlphabetJumpBar
        firstIndexByLetter={alphabetIndex}
        onJump={(index, offset) => jumpRef.current?.(index, offset)}
        pendingLetters={pendingLetters}
        onPendingJump={setPendingLetter}
      />
    ) : null

  let body
  if (tracksPage.loading) body = <LoadingState />
  else if (tracksPage.failure === 'first') {
    body = (
      <ErrorState message={errorMessage(tracksPage.error, 'Could not load your songs.')} onRetry={tracksPage.reload} />
    )
  } else if (tracksPage.items.length === 0) body = <p className="empty">Nothing here yet.</p>
  else {
    body = (
      <>
        {/* The rail on a phone, right after the toolbar (it measures it). */}
        {!isDesktop && jumpBar}
        <TrackList
          tracks={tracksPage.items}
          playlists={playlists}
          onChanged={tracksPage.reload}
          scrollContainerRef={scrollContainerRef}
          onLoadMore={tracksPage.loadMore}
          hasMore={tracksPage.hasMore}
          loadingMore={tracksPage.loadingMore}
          onPlaylistsChanged={onPlaylistsChanged}
          onOpenArtist={onOpenArtist}
          onOpenAlbum={onOpenAlbum}
          layout={!isDesktop || viewMode === 'cards' ? 'cards' : 'table'}
          jumpRef={jumpRef}
          libraryFilters={{ q: search || undefined, genre: genre || undefined, tag: tag || undefined }}
          libraryOrder={order}
          showStats
          playSource={{
            kind: 'library',
            label: search || genre || tag ? 'Songs' : 'All Songs',
            payload: { filters: { q: search || undefined, genre: genre || undefined, tag: tag || undefined } },
          }}
        />
        {/* A failed "load more" or refresh keeps the rows already read and offers another try. */}
        {tracksPage.failure === 'more' && (
          <ErrorState message="Could not load more songs." onRetry={tracksPage.loadMore} />
        )}
        {tracksPage.failure === 'refresh' && (
          <ErrorState message="Could not refresh the list." onRetry={tracksPage.reload} />
        )}
      </>
    )
  }

  return (
    <div className="songs-view">
      <div className="page-toolbar songs-toolbar" role="group" aria-label="Filter and view songs">
        <GenreMenu genre={genre} genres={genres} onGenreChange={onGenreChange} />
        {isDesktop && jumpBar}
        {tag && (
          <button type="button" className="chip chip-token" onClick={onClearTag} aria-label={`Clear tag ${tag}`}>
            <span>Tag: {tag}</span>
            <CloseIcon aria-hidden="true" />
          </button>
        )}
        <SearchChip query={typedSearch} onClear={onClearSearch} />
        <span className="page-toolbar-gap" />
        <SortMenu
          labels={SONGS_SORT_LABELS}
          menuLabels={SONGS_SORT_MENU_LABELS}
          sort={sort}
          direction={direction}
          onSortPick={pickSort}
        />
        <div className="page-toolbar-group" role="group" aria-label="View">
          {/* Table / Cards are a desktop choice (a phone always shows cards). */}
          {isDesktop && (
            <>
              <span className="page-toolbar-label" aria-hidden="true">
                View
              </span>
              <button
                type="button"
                className="chip"
                aria-pressed={viewMode === 'table'}
                onClick={() => chooseView('table')}
              >
                <ListViewIcon aria-hidden="true" />
                Table
              </button>
              <button
                type="button"
                className="chip"
                aria-pressed={viewMode === 'cards'}
                onClick={() => chooseView('cards')}
              >
                <GridViewIcon aria-hidden="true" />
                Cards
              </button>
            </>
          )}
          <button
            type="button"
            className="chip"
            aria-pressed={compact}
            onClick={() => updateSettings({ listDensity: compact ? 'comfortable' : 'compact' })}
          >
            Compact
          </button>
        </div>
      </div>
      {body}
    </div>
  )
}
