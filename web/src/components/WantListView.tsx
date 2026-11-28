import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { useAuth } from '../AuthContext'
import { formatDuration } from '../formatDuration'
import { usePlayerActions } from '../PlayerContext'
import { useToast } from '../ToastContext'
import { useConfirm } from '../dialogs'
import {
  onWantListChange,
  wantApi,
  type CatalogAlbum,
  type CatalogArtist,
  type CatalogReleaseGroup,
  type WantedArtistDetail,
  type WantedArtistSummary,
  type WantEntry,
  type WantInput,
  type WantSearchResults,
} from '../wantList'
import type { WantNav } from '../nav'
import { TopBarBack } from '../headerControlsPortal'
import { Art } from './Art'
import { BrowseDetailHeader } from './BrowseDetailHeader'
import { Badge, CatalogCover, CatalogTile, CatalogTrackList, WantButton } from './Catalog'
import { ImageCredit } from './InfoParts'
import { useCatalogArtCredit } from '../artCredit'
import { FetchingMore } from './ErrorState'
import { PENDING_REASK_MS, usePendingAsync } from '../usePendingAsync'
import { KEPT_TAB_STALE_MS, useAsync } from '../useAsync'
import { errorMessage, groupCatalogSections, year } from '../catalogHelpers'
import './wantList.css'
import { MicIcon } from './Icons'
import { Switch } from './Switch'

export type WantView = WantNav

interface Props {
  view: WantView
  /** `up` is a page's back link: up to `view`, its parent page, without
   * adding a history entry (see App's goUp). */
  onNavigate: (view: WantView, options?: { replace?: boolean; up?: boolean }) => void
  /** Open a library artist's own page; `showMissing` turns its missing
   * releases on (an owned artist picked from the want-list search). */
  onOpenArtist: (name: string, options?: { showMissing?: boolean }) => void
  onOpenAlbum: (artist: string | null, album: string) => void
}

/** While the server re-checks an artist in the background, poll until it is
 * done and then reload quietly (the page keeps showing what it has). */
function useBackgroundRecheck(refreshing: boolean | undefined, mbid: string | null | undefined, reload: () => void) {
  useEffect(() => {
    if (!refreshing || !mbid) return
    let cancelled = false
    const started = Date.now()
    const timer = window.setInterval(() => {
      wantApi.artistRefreshStatus(mbid).then(
        (status) => {
          if (cancelled) return
          if (!status.refreshing || Date.now() - started > 10 * 60_000) {
            window.clearInterval(timer)
            reload()
          }
        },
        () => {},
      )
    }, 3000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [refreshing, mbid, reload])
  // The reload brings the page's own `refreshing` flag back to false.
  return Boolean(refreshing && mbid)
}

/** While the server re-matches the want list against the library in the
 * background (it said `matching`), poll until that is done and then reload
 * quietly. Returns whether it is still going, for the "Checking…" bar. */
function useMatchingPoll(matching: boolean, allUsers: boolean, reload: () => void) {
  const [done, setDone] = useState(false)
  // A fresh "matching" answer starts the wait again.
  const [seen, setSeen] = useState(matching)
  if (seen !== matching) {
    setSeen(matching)
    setDone(false)
  }
  useEffect(() => {
    if (!matching || done) return
    let cancelled = false
    const started = Date.now()
    const timer = window.setInterval(() => {
      wantApi.matchStatus(allUsers).then(
        (status) => {
          if (cancelled) return
          if (!status.refreshing || Date.now() - started > 10 * 60_000) {
            window.clearInterval(timer)
            setDone(true)
            reload()
          }
        },
        () => {},
      )
    }, PENDING_REASK_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [matching, done, allUsers, reload])
  return matching && !done
}

/** Thin bar at the top of a page whose saved data is being re-checked. */
function RecheckBar({ active }: { active: boolean }) {
  if (!active) return null
  return (
    <div className="recheck-bar" role="status" aria-live="polite">
      <div className="recheck-bar-track" aria-hidden />
      <span>Checking for new information…</span>
    </div>
  )
}

const SHOW_UNWANTED_KEY = 'atriolyric.wantShowUnwanted'

/** "Show releases not wanted" on an artist's page: a per-device choice, on
 * by default (the whole discography). */
function useShowUnwanted(): [boolean, (next: boolean) => void] {
  const [value, setValue] = useState(() => {
    try {
      return localStorage.getItem(SHOW_UNWANTED_KEY) !== '0'
    } catch {
      return true
    }
  })
  const set = (next: boolean) => {
    setValue(next)
    try {
      localStorage.setItem(SHOW_UNWANTED_KEY, next ? '1' : '0')
    } catch {
      // Not remembered across reloads, but still applies now.
    }
  }
  return [value, set]
}

function useWantRefresh(load: () => void) {
  useEffect(() => onWantListChange(load), [load])
}

export function WantListView({ view, onNavigate, onOpenArtist, onOpenAlbum }: Props) {
  if (view.page === 'artist' || view.page === 'catalog') {
    // An artist with a MusicBrainz id gets one page: the whole discography
    // with the want toggles, and the songs wanted one by one under it.
    const mbid = view.mbid
    if (!mbid && view.page === 'artist') {
      return <WantedArtistPage view={view} onNavigate={onNavigate} onOpenArtist={onOpenArtist} onOpenAlbum={onOpenAlbum} />
    }
    return (
      <CatalogArtistPage
        view={{ page: 'catalog', mbid: mbid!, name: view.name }}
        onNavigate={onNavigate}
        onOpenArtist={onOpenArtist}
        onOpenAlbum={onOpenAlbum}
      />
    )
  }
  if (view.page === 'album') {
    return <CatalogAlbumPage view={view} onNavigate={onNavigate} onOpenAlbum={onOpenAlbum} />
  }
  return <WantListHome view={view} onNavigate={onNavigate} onOpenArtist={onOpenArtist} onOpenAlbum={onOpenAlbum} />
}

// --- home: search + wanted artists -------------------------------------------

const SEARCH_PREVIEW = 5

function WantListHome({ view, onNavigate, onOpenArtist, onOpenAlbum }: Props & { view: { page: 'home'; query?: string } }) {
  const { user } = useAuth()
  const { showToast } = useToast()
  const confirm = useConfirm()
  const [input, setInput] = useState(view.query ?? '')
  const [results, setResults] = useState<WantSearchResults | null>(null)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  const [allUsers, setAllUsers] = useState(false)
  // A kept-mounted tab: shown again soon after, it keeps the list it has (a
  // change to the want list anywhere reloads it, below).
  const listState = useAsync(() => wantApi.artists(allUsers), [allUsers], { staleMs: KEPT_TAB_STALE_MS })
  const artists: WantedArtistSummary[] | null = listState.data?.rows ?? null
  const listError = listState.status === 'error'
  // The server is still re-matching the list against the library.
  const matching = listState.data?.matching ?? false
  const loadList = listState.reload
  useWantRefresh(loadList)
  const checking = useMatchingPoll(matching, allUsers, loadList)

  // Searches run only on Enter (they reach MusicBrainz, about a second per
  // request); the query lives in the URL so Back returns to the results.
  useEffect(() => {
    const q = view.query?.trim()
    setInput(view.query ?? '')
    if (!q) {
      setResults(null)
      return
    }
    let cancelled = false
    setSearching(true)
    setSearchError(null)
    wantApi
      .search(q)
      .then(
        (value) => {
          if (!cancelled) setResults(value)
        },
        (err) => {
          if (!cancelled) setSearchError(errorMessage(err, 'Search failed - MusicBrainz may be busy, try again.'))
        },
      )
      .finally(() => {
        if (!cancelled) setSearching(false)
      })
    return () => {
      cancelled = true
    }
  }, [view.query])

  function submit(event: FormEvent) {
    event.preventDefault()
    const q = input.trim()
    onNavigate({ page: 'home', query: q || undefined }, { replace: !!view.query })
  }

  async function clearOwned() {
    // Not undoable, and one press away at the end of a row of links.
    if (!(await confirm({
      title: 'Clear owned items',
      body: 'Remove every item on your want list that is already in your library? The music stays in your library; only the want-list entries are cleared.',
      confirmLabel: 'Clear owned',
      danger: true,
    }))) return
    try {
      const { removed } = await wantApi.clearOwned()
      showToast(removed ? `Cleared ${removed} owned item${removed === 1 ? '' : 's'}` : 'Nothing owned to clear', 'success')
    } catch (err) {
      showToast(errorMessage(err, 'Could not clear owned items.'), 'error')
    }
  }

  return (
    <div className="want-list">
      <RecheckBar active={checking} />
      <form className="want-search" role="search" onSubmit={submit}>
        <input
          type="search"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Artist, album or song you don't own yet…"
          aria-label="Search MusicBrainz for artists, albums and songs"
          autoComplete="off"
          enterKeyHint="search"
        />
        <button type="submit" className="btn btn-primary" disabled={searching || !input.trim()}>
          Look up
        </button>
      </form>
      <p className="want-search-hint">
        Looks things up on MusicBrainz (press Enter). To search what you already own, use the search on Home.
      </p>

      {searching && (
        <div className="want-loading" role="status">
          <span className="want-spinner" aria-hidden /> Searching MusicBrainz…
        </div>
      )}
      {searchError && <p className="login-error" role="alert">{searchError}</p>}
      {results && !searching && (
        <SearchResults results={results} onNavigate={onNavigate} onOpenArtist={onOpenArtist} onOpenAlbum={onOpenAlbum} />
      )}

      <section className="want-section" aria-labelledby="want-artists-heading">
        <div className="sec-head want-section-heading">
          <h3 id="want-artists-heading">Wanted</h3>
          <div className="sec-head-links want-section-actions">
            {user?.is_admin && (
              <Switch className="want-toggle" checked={allUsers} onChange={setAllUsers}>
                All users
                <span className="badge badge-accent">admin</span>
              </Switch>
            )}
            <a className="text-link want-link" href={wantApi.exportUrl('csv', allUsers)} download>
              Export CSV
            </a>
            <a className="text-link want-link" href={wantApi.exportUrl('json', allUsers)} download>
              JSON
            </a>
            <button type="button" className="text-link want-link" onClick={clearOwned}>
              Clear owned
            </button>
          </div>
        </div>
        {listError ? (
          <p className="empty">Couldn't load your want list.</p>
        ) : artists === null ? (
          <p className="loading">Loading…</p>
        ) : artists.length === 0 ? (
          <p className="empty">Nothing wanted yet. Search above, or use “+ Want” on an artist, album or song.</p>
        ) : (
          <div className="want-artist-tiles">
            {artists.map((artist) => (
              <button
                key={artist.artist_mbid ?? artist.artist}
                type="button"
                className="browse-card want-artist-card"
                onClick={() => onNavigate({ page: 'artist', name: artist.artist, mbid: artist.artist_mbid ?? undefined })}
              >
                <ArtistPicture
                  mediaItemId={artist.representative_media_item_id}
                  mbid={artist.artist_mbid}
                  cover={artist.cover_release_group_mbid}
                  className="want-artist-cover"
                />
                <span className="want-artist-text">
                  <span className="browse-title">{artist.artist}</span>
                  <span className="browse-subtitle">{summaryLine(artist)}</span>
                  {artist.users && <span className="browse-subtitle">{artist.users.join(', ')}</span>}
                </span>
              </button>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}

function summaryLine(artist: WantedArtistSummary) {
  const parts: string[] = []
  if (artist.wants_artist) parts.push('Everything')
  if (artist.album_count) parts.push(`${artist.album_count} album${artist.album_count === 1 ? '' : 's'}`)
  if (artist.track_count) parts.push(`${artist.track_count} song${artist.track_count === 1 ? '' : 's'}`)
  if (artist.owned_count) parts.push(`${artist.owned_count} in library`)
  return parts.join(' · ')
}

function SearchSection<T>({
  title,
  rows,
  render,
}: {
  title: string
  rows: T[]
  render: (row: T) => ReactNode
}) {
  const [expanded, setExpanded] = useState(false)
  if (rows.length === 0) return null
  const visible = expanded ? rows : rows.slice(0, SEARCH_PREVIEW)
  return (
    <section className="want-results-section" aria-label={title}>
      <h3>{title}</h3>
      <ul className="want-results">{visible.map(render)}</ul>
      {rows.length > SEARCH_PREVIEW && (
        <button type="button" className="text-link want-more" onClick={() => setExpanded(!expanded)}>
          {expanded ? 'Show less' : `Show ${rows.length - SEARCH_PREVIEW} more`}
        </button>
      )}
    </section>
  )
}

function SearchResults({
  results,
  onNavigate,
  onOpenArtist,
  onOpenAlbum,
}: {
  results: WantSearchResults
  onNavigate: Props['onNavigate']
  onOpenArtist: Props['onOpenArtist']
  onOpenAlbum: Props['onOpenAlbum']
}) {
  // Optimistic marks so a just-added row shows as wanted without a refetch.
  const [added, setAdded] = useState<Set<string>>(new Set())
  const mark = (key: string) => setAdded((current) => new Set(current).add(key))
  const nothing = results.artists.length + results.albums.length + results.tracks.length === 0
  if (nothing) return <p className="empty">No matches on MusicBrainz.</p>
  return (
    <div className="want-search-results">
      <SearchSection
        title="Artists"
        rows={results.artists}
        render={(artist) => (
          <li key={artist.mbid} className="want-result">
            <CatalogCover kind="artist" mbid={artist.mbid} className="art-xs" />
            <button
              type="button"
              className="want-result-main"
              onClick={() =>
                artist.in_library && artist.library_name
                  ? onOpenArtist(artist.library_name, { showMissing: true })
                  : onNavigate({ page: 'catalog', mbid: artist.mbid, name: artist.name ?? undefined })
              }
            >
              <span className="want-result-title">{artist.name}</span>
              <span className="want-result-sub">
                {[artist.disambiguation, artist.type, artist.country].filter(Boolean).join(' · ')}
              </span>
            </button>
            {artist.in_library && <Badge>In library</Badge>}
            {artist.name != null && (
              <WantButton
                compact
                wanted={artist.wanted || added.has(`artist:${artist.mbid}`)}
                onAdd={() =>
                  wantApi
                    .add({ kind: 'artist', artist: artist.name ?? '', artist_mbid: artist.mbid })
                    .then(() => mark(`artist:${artist.mbid}`))
                }
              />
            )}
          </li>
        )}
      />
      <SearchSection
        title="Albums"
        rows={results.albums}
        render={(album) => (
          <li key={album.release_group_mbid} className="want-result">
            <CatalogCover mbid={album.release_group_mbid} className="art-xs" />
            <button
              type="button"
              className="want-result-main"
              onClick={() =>
                album.owned_album?.album
                  ? onOpenAlbum(album.owned_album.artist, album.owned_album.album)
                  : onNavigate({ page: 'album', mbid: album.release_group_mbid, artist: album.artist })
              }
            >
              <span className="want-result-title">{album.title}</span>
              <span className="want-result-sub">
                {[album.artist, album.section !== 'Album' ? album.section : null, album.year].filter(Boolean).join(' · ')}
              </span>
            </button>
            {album.in_library && <Badge>In library</Badge>}
            {!album.in_library && (
              <WantButton
                compact
                wanted={album.wanted || added.has(`album:${album.release_group_mbid}`)}
                onAdd={() =>
                  wantApi
                    .add({
                      kind: 'album',
                      artist: album.artist,
                      title: album.title,
                      artist_mbid: album.artist_mbid,
                      release_group_mbid: album.release_group_mbid,
                      release_type: album.primary_type,
                    })
                    .then(() => mark(`album:${album.release_group_mbid}`))
                }
              />
            )}
          </li>
        )}
      />
      <SearchSection
        title="Songs"
        rows={results.tracks}
        render={(track) => (
          <li key={track.recording_mbid} className="want-result">
            <button
              type="button"
              className="want-result-main"
              onClick={() =>
                track.release_group_mbid
                  ? onNavigate({ page: 'album', mbid: track.release_group_mbid, artist: track.artist })
                  : undefined
              }
              disabled={!track.release_group_mbid}
            >
              <span className="want-result-title">{track.title}</span>
              <span className="want-result-sub">
                {[track.artist, track.album, track.year, formatDuration(track.length_seconds)].filter(Boolean).join(' · ')}
              </span>
            </button>
            {track.in_library && <Badge>In library</Badge>}
            {!track.in_library && (
              <WantButton
                compact
                wanted={track.wanted || added.has(`track:${track.recording_mbid}`)}
                onAdd={() =>
                  wantApi
                    .add({
                      kind: 'track',
                      artist: track.artist,
                      title: track.title,
                      album: track.album,
                      artist_mbid: track.artist_mbid,
                      recording_mbid: track.recording_mbid,
                      release_group_mbid: track.release_group_mbid,
                    })
                    .then(() => mark(`track:${track.recording_mbid}`))
                }
              />
            )}
          </li>
        )}
      />
    </div>
  )
}

// --- one wanted artist --------------------------------------------------------

function WantedArtistPage({
  view,
  onNavigate,
  onOpenArtist,
  onOpenAlbum,
}: Omit<Props, 'view'> & { view: { page: 'artist'; name: string; mbid?: string } }) {
  // Stored data shows at once; a part still being fetched (`pending`) is
  // asked for again until it is in.
  const state = usePendingAsync(() => wantApi.artist(view.name, view.mbid), [view.name, view.mbid], {
    staleMs: KEPT_TAB_STALE_MS,
  })
  const detail: WantedArtistDetail | null = state.data ?? null
  const failed = state.status === 'error'
  const load = state.reload
  useWantRefresh(load)
  const rechecking = useBackgroundRecheck(detail?.refreshing, detail?.artist_mbid, load)
  const matching = useMatchingPoll(detail?.matching === true, false, load)
  const checking = rechecking || matching
  // Opened by name, but the entries know the artist's MusicBrainz id: show
  // the discography page instead.
  const foundMbid = detail?.artist_mbid
  useEffect(() => {
    if (foundMbid) onNavigate({ page: 'artist', name: view.name, mbid: foundMbid }, { replace: true })
  }, [foundMbid, view.name, onNavigate])

  const back = () => onNavigate({ page: 'home' }, { up: true })
  if (failed) return <BackOnly onBack={back} message="Couldn't load this artist's wants." />
  if (!detail) return <BackOnly onBack={back} message="Loading…" loading />
  const mbid = detail.artist_mbid
  const remove = (entry: WantEntry) => wantApi.remove(entry.id)
  const artistEntry = detail.artist_entry
  const missing = detail.missing_release_groups ?? []
  const albumEntries = detail.albums.filter((e) => !e.excluded)
  const entryByGroup = new Map(albumEntries.flatMap((e) => (e.release_group_mbid ? [[e.release_group_mbid, e] as const] : [])))
  const missingIds = new Set(missing.map((g) => g.mbid))
  // The missing part of the catalog and the albums wanted one by one, as
  // tiles in release-type sections like an artist page in the library.
  const tiles: CatalogReleaseGroup[] = [
    ...missing.map((group) => ({
      ...group,
      secondary_types: [],
      disambiguation: null,
      owned_album: null,
      wanted: true,
      excluded: false,
      want_entry_id: entryByGroup.get(group.mbid)?.id ?? null,
    })),
    ...albumEntries.filter((e) => e.release_group_mbid && !missingIds.has(e.release_group_mbid)).map(entryTile),
  ]
  const tracks = detail.tracks.filter((e) => !e.excluded)
  const openGroup = (group: CatalogReleaseGroup) => {
    const entry = entryByGroup.get(group.mbid)
    if (entry?.owned && entry.title) onOpenAlbum(entry.artist, entry.title)
    else onNavigate({ page: 'album', mbid: group.mbid, artist: detail.artist })
  }
  const subtitle = artistEntry
    ? detail.missing_release_groups == null
      ? 'Everything · includes future releases'
      : `${missing.length} release${missing.length === 1 ? '' : 's'} still missing · includes future releases`
    : wantedCounts(albumEntries.length, tracks.length)

  return (
    <div className="want-list want-artist-page">
      <RecheckBar active={checking} />
      <WantArtistHeader
        name={detail.artist}
        mbid={mbid}
        cover={detail.cover_release_group_mbid}
        subtitle={subtitle}
        onBack={back}
        actions={
          <>
            {artistEntry ? (
              <WantButton label="Want everything" wanted onAdd={async () => {}} onRemove={() => remove(artistEntry)} />
            ) : (
              mbid && (
                <WantButton
                  label="Want everything"
                  wanted={false}
                  onAdd={() => wantApi.add({ kind: 'artist', artist: detail.artist, artist_mbid: mbid })}
                />
              )
            )}
            {detail.in_library && (
              <button type="button" className="btn btn-ghost" onClick={() => onOpenArtist(detail.artist, { showMissing: true })}>
                Open in library
              </button>
            )}
          </>
        }
      />
      {state.fetchingMore && <FetchingMore />}

      {groupCatalogSections(tiles).map((section) => (
        <section key={section.heading} className="artist-album-section" aria-label={section.heading}>
          <h3 className="artist-section-heading">{section.heading}</h3>
          <div className="browse-grid">
            {section.groups.map((group) => (
              <CatalogTile
                key={group.mbid}
                group={group}
                artist={detail.artist}
                artistMbid={mbid}
                artistWanted={artistEntry != null}
                onOpen={() => openGroup(group)}
              />
            ))}
          </div>
        </section>
      ))}

      <WantedExtras detail={detail} hasTiles={tiles.length > 0} onOpenAlbum={onOpenAlbum} />
    </div>
  )
}

/** What an artist's tiles cannot show: albums wanted without a MusicBrainz
 * id, and the songs wanted one by one. */
function WantedExtras({
  detail,
  hasTiles,
  onOpenAlbum,
}: {
  detail: WantedArtistDetail
  hasTiles: boolean
  onOpenAlbum: Props['onOpenAlbum']
}) {
  const { playQueueIds } = usePlayerActions()
  const remove = (entry: WantEntry) => wantApi.remove(entry.id)
  const albumsWithoutId = detail.albums.filter((e) => !e.excluded && !e.release_group_mbid)
  const tracks = detail.tracks.filter((e) => !e.excluded)
  return (
    <>
      {albumsWithoutId.length > 0 && (
        <section className="artist-album-section" aria-label="Other albums">
          <h3 className="artist-section-heading">{hasTiles ? 'Other albums' : 'Albums'}</h3>
          <ul className="want-results">
            {albumsWithoutId.map((entry) => (
              <li key={entry.id} className={`want-result${entry.owned ? '' : ' is-missing'}`}>
                <button
                  type="button"
                  className="want-result-main"
                  onClick={() => entry.owned && entry.title && onOpenAlbum(entry.artist, entry.title)}
                  disabled={!(entry.owned && entry.title)}
                >
                  <span className="want-result-title">{entry.title}</span>
                  <span className="want-result-sub">{progressLine(entry)}</span>
                </button>
                {entry.owned && <Badge>In library</Badge>}
                <WantButton compact wanted onAdd={async () => {}} onRemove={() => remove(entry)} />
              </li>
            ))}
          </ul>
        </section>
      )}

      {tracks.length > 0 && (
        <section className="artist-album-section" aria-label="Songs">
          <h3 className="artist-section-heading">Songs</h3>
          <ul className="want-results">
            {tracks.map((entry) => (
              <li key={entry.id} className={`want-result${entry.owned ? '' : ' is-missing'}`}>
                {entry.release_group_mbid && <CatalogCover mbid={entry.release_group_mbid} className="art-xs" />}
                <div className="want-result-main">
                  <span className="want-result-title">{entry.title}</span>
                  <span className="want-result-sub">
                    {[entry.album, entry.station_name ? `From radio: ${entry.station_name}` : null].filter(Boolean).join(' · ')}
                  </span>
                </div>
                {entry.owned && <Badge>In library</Badge>}
                {entry.media_item_id != null && (
                  <button
                    type="button"
                    className="text-link want-link"
                    onClick={() => playQueueIds([entry.media_item_id!], 0)}
                    aria-label={`Play ${entry.title}`}
                  >
                    Play
                  </button>
                )}
                <WantButton compact wanted onAdd={async () => {}} onRemove={() => remove(entry)} />
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  )
}

/** A wanted album entry as a catalog tile. */
function entryTile(entry: WantEntry): CatalogReleaseGroup {
  const type = entry.release_type || 'Album'
  return {
    mbid: entry.release_group_mbid!,
    title: entry.title ?? '',
    primary_type: entry.release_type,
    secondary_types: [],
    section: ['Album', 'EP', 'Single'].includes(type) ? type : 'Other',
    first_release_date: null,
    disambiguation: null,
    status: entry.owned ? 'owned' : entry.owned_count ? 'partial' : 'missing',
    owned_count: entry.owned_count,
    total_count: entry.total_count,
    owned_album: null,
    wanted: true,
    excluded: false,
    want_entry_id: entry.id,
  }
}

function wantedCounts(albums: number, songs: number) {
  const parts: string[] = []
  if (albums) parts.push(`${albums} album${albums === 1 ? '' : 's'} wanted`)
  if (songs) parts.push(`${songs} song${songs === 1 ? '' : 's'} wanted`)
  return parts.join(' · ')
}

/** An artist's picture: a library track's art, else the artist's photo, else
 * (no photo known) one of their covers, else the mic. */
function ArtistPicture({
  mediaItemId,
  mbid,
  cover,
  className,
}: {
  mediaItemId?: number | null
  mbid: string | null | undefined
  cover: string | null | undefined
  className: string
}) {
  if (mediaItemId != null) return <Art mediaItemId={mediaItemId} className={className} fallback={<MicIcon />} />
  if (cover) return <CatalogCover mbid={cover} className={className} />
  if (mbid) return <CatalogCover kind="artist" mbid={mbid} className={className} />
  return <Art mediaItemId={null} className={className} fallback={<MicIcon />} />
}

/** The top of a wanted or catalog artist's page, laid out like a library
 * artist's: round picture, name, a line of facts and the actions. */
function WantArtistHeader({
  name,
  mbid,
  cover,
  subtitle,
  actions,
  onBack,
}: {
  name: string
  mbid: string | null | undefined
  cover: string | null | undefined
  subtitle: string
  actions: ReactNode
  onBack: () => void
}) {
  // A cover standing in for the photo has no photo credit to show.
  const credit = useCatalogArtCredit(cover ? null : mbid)
  return (
    <div className="collection-detail collection-detail-round want-artist-header">
      <BrowseDetailHeader backLabel="Want list" onBack={onBack} title={name} />
      <div className="collection-detail-info">
        <ArtistPicture mbid={mbid} cover={cover} className="collection-detail-art" />
        <div className="collection-detail-copy">
          <span className="collection-detail-eyebrow">Artist</span>
          <h2>{name}</h2>
          {subtitle && <p className="collection-detail-subtitle">{subtitle}</p>}
          {credit && <ImageCredit credit={credit} />}
          <div className="collection-detail-actions">{actions}</div>
        </div>
      </div>
    </div>
  )
}

function progressLine(entry: WantEntry) {
  const parts = [entry.release_type && entry.release_type !== 'Album' ? entry.release_type : null]
  if (entry.total_count && entry.total_count > 1) parts.push(`${entry.owned_count ?? 0}/${entry.total_count} songs`)
  return parts.filter(Boolean).join(' · ')
}

function BackOnly({ onBack, message, loading = false }: { onBack: () => void; message: string; loading?: boolean }) {
  return (
    <div className="want-list">
      <TopBarBack label="Want list" onBack={onBack} />
      {loading ? (
        <div className="want-loading" role="status">
          <span className="want-spinner" aria-hidden /> {message}
        </div>
      ) : (
        <p className="empty">{message}</p>
      )}
    </div>
  )
}

// --- catalog: an artist's whole discography --------------------------------------

function CatalogArtistPage({
  view,
  onNavigate,
  onOpenArtist,
  onOpenAlbum,
}: Omit<Props, 'view'> & { view: { page: 'catalog'; mbid: string; name?: string } }) {
  // A first look answers at once with a placeholder (`pending`) while the
  // server reads MusicBrainz; asked again until it is complete.
  const state = usePendingAsync(
    () => wantApi.catalogArtist({ mbid: view.mbid, name: view.name }),
    [view.mbid, view.name],
    { staleMs: KEPT_TAB_STALE_MS },
  )
  const catalog: CatalogArtist | null = state.data ?? null
  const [refreshError, setRefreshError] = useState<string | null>(null)
  const error =
    refreshError ?? (state.status === 'error' ? errorMessage(state.error, "Couldn't load this discography.") : null)
  const [refreshing, setRefreshing] = useState(false)
  const { reload, setData } = state
  function load(refresh: boolean) {
    if (!refresh) return reload()
    setRefreshing(true)
    wantApi
      .catalogArtist({ mbid: view.mbid, name: view.name, refresh })
      .then(setData, (err) => setRefreshError(errorMessage(err, "Couldn't load this discography.")))
      .finally(() => setRefreshing(false))
  }
  useWantRefresh(reload)
  // The pending re-asks above already follow a first look; the status poll
  // is for a re-check of data that was complete.
  const rechecking = useBackgroundRecheck(catalog?.refreshing && !catalog.pending, catalog?.artist_mbid, reload)
  const checking = rechecking || state.fetchingMore
  // The artist's want-list entries, for the songs wanted one by one (and
  // albums wanted that the discography does not list).
  const wantName = view.name ?? catalog?.artist_name ?? undefined
  const wantedState = usePendingAsync(() => wantApi.artist(wantName ?? '', view.mbid), [wantName, view.mbid], {
    enabled: Boolean(wantName),
    staleMs: KEPT_TAB_STALE_MS,
  })
  useWantRefresh(wantedState.reload)
  const wanted = wantedState.data ?? null
  const [showUnwanted, setShowUnwanted] = useShowUnwanted()

  const back = () => onNavigate({ page: 'home' }, { up: true })
  if (error) return <BackOnly onBack={back} message={error} />
  if (!catalog) return <BackOnly onBack={back} message="Loading the discography from MusicBrainz…" loading />
  const name = catalog.artist_name ?? view.name ?? 'Artist'
  const artistEntryInput: WantInput = { kind: 'artist', artist: name, artist_mbid: catalog.artist_mbid }
  const listed = new Set(catalog.release_groups.map((g) => g.mbid))
  const wantedAlbums = (wanted?.albums ?? []).filter((e) => !e.excluded)
  const entryByGroup = new Map(wantedAlbums.flatMap((e) => (e.release_group_mbid ? [[e.release_group_mbid, e] as const] : [])))
  const allTiles = [
    ...catalog.release_groups,
    ...wantedAlbums.filter((e) => e.release_group_mbid && !listed.has(e.release_group_mbid)).map(entryTile),
  ]
  const notWanted = allTiles.filter((g) => !g.wanted).length
  const tiles = showUnwanted ? allTiles : allTiles.filter((g) => g.wanted)
  const wantedCount = allTiles.length - notWanted
  const subtitle = [
    allTiles.length ? `${allTiles.length} release${allTiles.length === 1 ? '' : 's'}` : null,
    wantedCount ? `${wantedCount} wanted` : null,
    catalog.artist_wanted ? 'new releases wanted too' : null,
  ]
    .filter(Boolean)
    .join(' · ')
  const openGroup = (group: CatalogReleaseGroup) => {
    const entry = entryByGroup.get(group.mbid)
    if (group.owned_album?.album) onOpenAlbum(group.owned_album.artist, group.owned_album.album)
    else if (entry?.owned && entry.title) onOpenAlbum(entry.artist, entry.title)
    else onNavigate({ page: 'album', mbid: group.mbid, artist: name })
  }

  return (
    <div className="want-list catalog-page">
      <RecheckBar active={checking} />
      <WantArtistHeader
        name={name}
        mbid={catalog.artist_mbid}
        cover={catalog.cover_release_group_mbid}
        subtitle={subtitle}
        onBack={back}
        actions={
          <>
            <WantButton
              label="Want everything"
              wanted={catalog.artist_wanted}
              onAdd={() => wantApi.add(artistEntryInput)}
              onRemove={async () => {
                const detail = await wantApi.artist(name, catalog.artist_mbid)
                if (detail.artist_entry) await wantApi.remove(detail.artist_entry.id)
              }}
            />
            {catalog.in_library && (
              <button type="button" className="btn btn-ghost" onClick={() => onOpenArtist(name, { showMissing: true })}>
                Open in library
              </button>
            )}
            <button type="button" className="btn btn-ghost" onClick={() => load(true)} disabled={refreshing || checking}>
              {refreshing || checking ? 'Refreshing…' : 'Refresh'}
            </button>
          </>
        }
      />
      {catalog.tracklists_total > 0 && catalog.tracklists_known < catalog.tracklists_total && catalog.artist_wanted && (
        <p className="settings-hint">
          Fetching tracklists in the background ({catalog.tracklists_known}/{catalog.tracklists_total})…
        </p>
      )}
      {notWanted > 0 && wantedCount > 0 && (
        <Switch className="want-toggle want-unwanted-toggle" checked={showUnwanted} onChange={setShowUnwanted}>
          Show releases not wanted
        </Switch>
      )}
      {allTiles.length === 0 && state.fetchingMore ? (
        <FetchingMore label="Loading the discography from MusicBrainz…" />
      ) : allTiles.length === 0 ? (
        <p className="empty">MusicBrainz lists no releases for this artist.</p>
      ) : tiles.length === 0 ? (
        <p className="empty">Nothing by this artist is wanted.</p>
      ) : (
        groupCatalogSections(tiles).map((section) => (
          <section key={section.heading} className="artist-album-section" aria-label={section.heading}>
            <h3 className="artist-section-heading">{section.heading}</h3>
            <div className="browse-grid">
              {section.groups.map((group) => (
                <CatalogTile
                  key={group.mbid}
                  group={group}
                  artist={name}
                  artistMbid={catalog.artist_mbid}
                  artistWanted={catalog.artist_wanted}
                  onOpen={() => openGroup(group)}
                />
              ))}
            </div>
          </section>
        ))
      )}
      {wanted && <WantedExtras detail={wanted} hasTiles={allTiles.length > 0} onOpenAlbum={onOpenAlbum} />}
    </div>
  )
}

// --- catalog: one album -----------------------------------------------------------

function CatalogAlbumPage({
  view,
  onNavigate,
  onOpenAlbum,
}: Pick<Props, 'onNavigate' | 'onOpenAlbum'> & { view: { page: 'album'; mbid: string; artist?: string } }) {
  // A first look may answer with a placeholder (`pending`, no title or
  // tracks yet): asked again until MusicBrainz has answered.
  const state = usePendingAsync(() => wantApi.catalogAlbum(view.mbid, view.artist), [view.mbid, view.artist], {
    staleMs: KEPT_TAB_STALE_MS,
  })
  const loaded: CatalogAlbum | null = state.data ?? null
  // A placeholder with nothing to show yet reads as still loading.
  const album = loaded?.pending && !loaded.title ? null : loaded
  const error =
    state.status === 'error'
      ? errorMessage(state.error, "Couldn't load this album.")
      : loaded && !album && !state.fetchingMore
        ? "Couldn't load this album."
        : null
  useWantRefresh(state.reload)

  const home = () => onNavigate({ page: 'home' }, { up: true })
  const back = () => (album?.artist_mbid ? onNavigate({ page: 'catalog', mbid: album.artist_mbid, name: album.artist }, { up: true }) : home())
  if (error) return <BackOnly onBack={home} message={error} />
  if (!album) return <BackOnly onBack={home} message="Loading the tracklist from MusicBrainz…" loading />
  const albumInput: WantInput = {
    kind: 'album',
    artist: album.artist,
    title: album.title,
    artist_mbid: album.artist_mbid,
    release_group_mbid: album.mbid,
    release_type: album.primary_type,
  }

  return (
    <div className="want-list catalog-page">
      <BrowseDetailHeader backLabel={album.artist_mbid ? album.artist : 'Want list'} onBack={back} title={`${album.title} · ${album.artist}`} />
      <div className="collection-detail-info catalog-album-info">
        <CatalogCover mbid={album.mbid} className="collection-detail-art" />
        <div className="collection-detail-copy">
          <span className="collection-detail-eyebrow">{album.section || 'Album'}</span>
          <h2>{album.title}</h2>
          {state.fetchingMore && <FetchingMore />}
          <p>
            {[album.artist, album.section !== 'Album' ? album.section : null, year(album.first_release_date), `${album.owned_count}/${album.tracks.length} in library`]
              .filter(Boolean)
              .join(' · ')}
          </p>
          <div className="collection-detail-actions">
            {album.artist_wanted && !album.want_entry_id ? (
              <WantButton
                label="Want album"
                wanted={!album.excluded}
                onAdd={() => wantApi.add(albumInput)}
                onRemove={() => wantApi.add({ ...albumInput, excluded: true })}
              />
            ) : (
              <WantButton
                label="Want album"
                wanted={album.wanted}
                onAdd={() => wantApi.add(albumInput)}
                onRemove={album.want_entry_id != null ? () => wantApi.remove(album.want_entry_id!) : undefined}
              />
            )}
            {album.owned_album?.album && (
              <button type="button" className="btn btn-ghost" onClick={() => onOpenAlbum(album.owned_album!.artist, album.owned_album!.album!)}>
                Open in library
              </button>
            )}
          </div>
        </div>
      </div>
      <CatalogTrackList album={album} />
    </div>
  )
}
