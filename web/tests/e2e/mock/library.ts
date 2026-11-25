// The library: tracks, albums, artists, search and folders.

import { type RouteContext, empty, json } from './http'
import { artFields } from './media'
import { withExclusion } from './preferences'
import type { AppMockState, ArtistCredit, MediaItem } from './types'

export function trackCredits(item: MediaItem): ArtistCredit[] {
  return item.artists && item.artists.length > 0
    ? item.artists
    : item.artist
      ? [{ name: item.artist, role: 'main' as const }]
      : []
}

export function ownsOrPerforms(item: MediaItem, artist: string) {
  return trackCredits(item).some((credit) => credit.name === artist && credit.role !== 'featured')
}

export function albumOwner(item: MediaItem): string | null {
  return item.album_artist ?? item.artist
}

export function albumOwnerCredits(item: MediaItem): ArtistCredit[] {
  const owners = (item.artists ?? []).filter((credit) => credit.role === 'album_artist')
  const owner = albumOwner(item)
  return owners.length > 0 ? owners : owner ? [{ name: owner, role: 'album_artist' as const }] : []
}

export function filteredTracks(state: AppMockState, url: URL, includeVideos = false): MediaItem[] {
  const q = (url.searchParams.get('q') ?? '').toLowerCase()
  const genre = url.searchParams.get('genre') ?? ''
  const artist = url.searchParams.get('artist') ?? ''
  const album = url.searchParams.get('album') ?? ''
  // The album nobody owns, as opposed to no artist filter at all.
  const noArtist = url.searchParams.get('artist_is_null') === 'true'
  return state.tracks.filter((item) => {
    if (!includeVideos && item.kind !== 'track') return false
    if (includeVideos && item.kind === 'track') return false
    const searchable = [item.title, item.artist, item.album].filter(Boolean).join(' ').toLowerCase()
    return (
      (!q || searchable.includes(q)) &&
      (!genre || item.genre === genre) &&
      (!artist || item.artist === artist || ownsOrPerforms(item, artist) || albumOwner(item) === artist) &&
      (!noArtist || albumOwner(item) == null) &&
      (!album || item.album === album)
    )
  })
}

/** The Songs tab's sorts (GET /api/library/tracks sort/direction), by the
 * server's rules: names case-folded A-Z, newest / most first by default, a
 * missing album or year and never-played tracks last in both directions, and
 * ties in the list's own order. Without a sort the list keeps its order. */
export function sortTracks(list: MediaItem[], sort: string | null, direction: string | null): MediaItem[] {
  if (!sort || (sort === 'artist' && direction !== 'desc')) return list
  const desc = (direction ?? (['artist', 'title', 'album'].includes(sort) ? 'asc' : 'desc')) === 'desc'
  const key = (item: MediaItem): string | number | null => {
    switch (sort) {
      case 'artist':
        return item.artist ?? ''
      case 'title':
        return item.title.toLowerCase()
      case 'album':
        return item.album?.toLowerCase() ?? null
      case 'year':
        return item.year ?? null
      case 'date_added':
        return item.id
      case 'most_played':
        return item.play_count || null
      case 'last_played':
        return item.last_played ?? null
      default:
        return null
    }
  }
  return list
    .map((item, index) => ({ item, index, value: key(item) }))
    .sort((a, b) => {
      if (a.value === null || b.value === null) {
        return a.value === b.value ? a.index - b.index : a.value === null ? 1 : -1
      }
      const order = a.value < b.value ? -1 : a.value > b.value ? 1 : 0
      return (desc ? -order : order) || a.index - b.index
    })
    .map(({ item }) => item)
}

export const SEARCH_SECTION_LIMIT = 13

export function foldText(value: string): string {
  return value.normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase()
}

/** GET /api/library/search, by the same rules as the server: every folded
 * word in the section's own field, starts-with first, then A-Z, tracks
 * only, 13 per section. */
export function librarySearch(state: AppMockState, q: string) {
  const words = foldText(q).split(/\s+/).filter(Boolean)
  if (words.length === 0) return { artists: [], albums: [], songs: [], playlists: [] }
  const phrase = words.join(' ')
  const matches = (value: string | null | undefined) =>
    value != null && words.every((word) => foldText(value).includes(word))
  function ranked<T>(items: T[], name: (item: T) => string): T[] {
    return items
      .map((item) => ({ item, folded: foldText(name(item)) }))
      .sort(
        (a, b) =>
          Number(!a.folded.startsWith(phrase)) - Number(!b.folded.startsWith(phrase)) ||
          a.folded.localeCompare(b.folded),
      )
      .slice(0, SEARCH_SECTION_LIMIT)
      .map(({ item }) => item)
  }

  const tracks = state.tracks.filter((item) => item.kind === 'track')
  const artists = new Map<string, { artist: string; ids: Set<number>; albums: Set<string | null> }>()
  for (const item of tracks) {
    for (const name of new Set([item.artist, item.album_artist])) {
      if (!name || !matches(name)) continue
      const entry = artists.get(name) ?? { artist: name, ids: new Set(), albums: new Set() }
      entry.ids.add(item.id)
      entry.albums.add(item.album)
      artists.set(name, entry)
    }
  }
  const albums = new Map<string, { artist: string | null; album: string; track_count: number; representative_media_item_id: number }>()
  for (const item of tracks) {
    if (!item.album || !matches(item.album)) continue
    const key = `${item.artist ?? ''}::${item.album}`
    const current = albums.get(key)
    if (current) current.track_count += 1
    else albums.set(key, { artist: item.artist, album: item.album, track_count: 1, representative_media_item_id: item.id })
  }
  return {
    artists: ranked([...artists.values()], (entry) => entry.artist).map((entry) => ({
      artist: entry.artist,
      track_count: entry.ids.size,
      album_count: [...entry.albums].filter(Boolean).length,
      representative_media_item_id: Math.min(...entry.ids),
    })),
    albums: ranked([...albums.values()], (album) => album.album),
    songs: ranked(tracks.filter((item) => matches(item.title)), (item) => item.title),
    playlists: ranked(state.playlists.filter((playlist) => matches(playlist.name)), (playlist) => playlist.name),
  }
}

export function mediaForIds(state: AppMockState, ids: number[]): MediaItem[] {
  return ids
    .map((id) => state.tracks.find((item) => item.id === id))
    .filter((item): item is MediaItem => item != null)
}

export function browseDirectory(state: AppMockState, name: string, path: string) {
  const ignoredBy = state.ignoredFolderPaths
    .filter((ignored) => path === ignored || path.startsWith(`${ignored}/`))
    .sort((left, right) => right.length - left.length)[0] ?? null
  return {
    name,
    path,
    is_ignored: ignoredBy !== null,
    ignored_directly: ignoredBy === path,
    ignored_by: ignoredBy,
  }
}

/** The album list entries for a /albums request's filters, as the server
 * groups them. */
function albumEntries(state: AppMockState, url: URL) {
  const q = (url.searchParams.get('q') ?? '').toLowerCase()
  const genre = url.searchParams.get('genre') ?? ''
  const artistFilter = url.searchParams.get('artist') ?? ''
  const appearsOn = url.searchParams.get('appears_on') === 'true'
  const seen = new Map<string, { artist: string | null; album: string; track_count: number; representative_media_item_id: number; artists?: { name: string; role: string }[]; section?: string; art_key?: string; custom_art?: boolean; art_version?: number | null }>()
  // Albums group by their owner (album artist, else track artist), as
  // the real endpoint does; with artist + appears_on, only albums the
  // artist performs on but doesn't own.
  const unfiltered = new URL(url)
  unfiltered.searchParams.delete('artist')
  for (const item of filteredTracks(state, unfiltered, false)) {
    if (q && !`${item.album ?? ''} ${item.artist ?? ''}`.toLowerCase().includes(q)) continue
    if (genre && item.genre !== genre) continue
    const owners = albumOwnerCredits(item)
    if (artistFilter) {
      const owns = owners.some((credit) => credit.name === artistFilter) || albumOwner(item) === artistFilter
      const performs = trackCredits(item).some((credit) => credit.name === artistFilter)
      if (appearsOn ? owns || !performs : !owns) continue
    }
    const key = `${albumOwner(item) ?? ''}::${item.album ?? ''}`
    const current = seen.get(key)
    if (current) current.track_count += 1
    else {
      seen.set(key, {
        artist: albumOwner(item),
        album: item.album ?? 'Unknown album',
        track_count: 1,
        representative_media_item_id: item.id,
        artists: owners,
        section: item.section ?? 'Album',
        art_key: `album-${item.id}`,
        ...artFields('album', `album-${item.id}`),
      })
    }
  }
  return [...seen.values()]
}

/** The Albums / Artists "Most played" and "Last played" order, by the
 * server's rules (albums_artists._played_order): `desc` (the default) is most
 * played / most recently played first, `asc` the reverse, never-played last
 * either way, ties in the list's own order. Other sorts keep the list order. */
function sortPlayed<T>(
  list: T[],
  url: URL,
  stats: (entry: T) => { plays: number; last: number | null },
): T[] {
  const sort = url.searchParams.get('sort')
  if (sort !== 'most_played' && sort !== 'last_played') return list
  const asc = url.searchParams.get('direction') === 'asc'
  const key = (entry: T): number | null => {
    const { plays, last } = stats(entry)
    return (sort === 'most_played' ? plays || null : last) ?? null
  }
  return list
    .map((entry, index) => ({ entry, index, value: key(entry) }))
    .sort((a, b) => {
      if (a.value === null || b.value === null) {
        return a.value === b.value ? a.index - b.index : a.value === null ? 1 : -1
      }
      return (asc ? a.value - b.value : b.value - a.value) || a.index - b.index
    })
    .map(({ entry }) => entry)
}

function playStats(tracks: MediaItem[]) {
  const times = tracks.map((track) => track.last_played).filter((value): value is number => value != null)
  return {
    plays: tracks.reduce((sum, track) => sum + (track.play_count ?? 0), 0),
    last: times.length ? Math.max(...times) : null,
  }
}

/** Genres, roots, tracks, search, albums and artist summaries. */
export async function handleLibraryRoutes({ state, route, url, path }: RouteContext): Promise<boolean> {
  if (path === '/api/library/genres') {
    await json(route, ['Electronic', 'Rock'])
    return true
  }
  if (path === '/api/library/mixes/options') {
    await json(route, [
      { kind: 'genre', name: 'Rock', track_count: 200 },
      { kind: 'genre', name: 'Electronic', track_count: 50 },
      { kind: 'tag', name: 'Workout', track_count: 10 },
    ])
    return true
  }
  if (path === '/api/library/roots') {
    await json(route, state.roots.map((root) => ({ id: root.id, display_name: root.display_name })))
    return true
  }
  if (path === '/api/library/tracks') {
    const list = sortTracks(filteredTracks(state, url), url.searchParams.get('sort'), url.searchParams.get('direction'))
    const offset = Number(url.searchParams.get('offset') ?? 0)
    const limit = Number(url.searchParams.get('limit') ?? list.length)
    await json(route, list.slice(offset, offset + limit).map((track) => ({ ...withExclusion(state, track), ...artFields('media_item', track.id) })))
    return true
  }
  if (path === '/api/library/search') {
    await json(route, librarySearch(state, url.searchParams.get('q') ?? ''))
    return true
  }
  if (path === '/api/library/albums') {
    const albums = sortPlayed(albumEntries(state, url), url, (entry) =>
      playStats(state.tracks.filter((track) => track.kind === 'track' && albumOwner(track) === entry.artist && (track.album ?? 'Unknown album') === entry.album)),
    )
    if (url.searchParams.get('sort') === 'date_added') albums.reverse()
    // full=1 returns the whole matching list, pagination params ignored -
    // mirroring the real endpoint. Without it, honor limit/offset so the
    // client's paging behavior is exercised for real.
    if (url.searchParams.get('full') !== '1') {
      const offset = Number(url.searchParams.get('offset') ?? 0)
      const limit = Number(url.searchParams.get('limit') ?? albums.length)
      await json(route, albums.slice(offset, offset + limit))
    } else {
      await json(route, albums)
    }
    return true
  }
  if (path === '/api/library/album-summaries') {
    // One album's list entry (0 or 1), found by title and owner; no `artist`
    // is the album with no artist.
    const album = url.searchParams.get('album') ?? ''
    const artist = url.searchParams.get('artist')
    const unfiltered = new URL(url)
    unfiltered.search = ''
    await json(
      route,
      albumEntries(state, unfiltered).filter((entry) => entry.album === album && entry.artist === artist),
    )
    return true
  }
  if (path === '/api/library/artist-summaries') {
    const names = url.searchParams.getAll('names')
    await json(
      route,
      names.flatMap((name) => {
        const items = state.tracks.filter((item) => item.kind === 'track' && trackCredits(item).some((credit) => credit.name === name))
        if (items.length === 0) return []
        return [{
          artist: name,
          track_count: items.length,
          album_count: new Set(items.map((item) => item.album)).size,
          representative_media_item_id: items[0].id,
          art_key: `artist-${items[0].id}`,
          ...artFields('artist', `artist-${items[0].id}`),
        }]
      }),
    )
    return true
  }
  return false
}

/** Artists, media, videos, files, folders, browsing and recents. */
export async function handleLibraryBrowseRoutes({ state, route, url, method, path, body }: RouteContext): Promise<boolean> {
  if (path === '/api/library/artists') {
    const q = (url.searchParams.get('q') ?? '').toLowerCase()
    const genre = url.searchParams.get('genre') ?? ''
    const seen = new Map<string, { artist: string; track_count: number; album_count: number; representative_media_item_id: number; art_key?: string; custom_art?: boolean; art_version?: number | null }>()
    // One entry per credited artist, guests included (app.artist_credits).
    for (const item of filteredTracks(state, new URL(url), false)) {
      if (genre && item.genre !== genre) continue
      for (const name of new Set(trackCredits(item).map((credit) => credit.name))) {
        if (q && !name.toLowerCase().includes(q)) continue
        const current = seen.get(name)
        if (current) {
          current.track_count += 1
        } else {
          seen.set(name, {
            artist: name,
            track_count: 1,
            album_count: 0,
            representative_media_item_id: item.id,
            art_key: `artist-${item.id}`,
            ...artFields('artist', `artist-${item.id}`),
          })
        }
      }
    }
    for (const entry of seen.values()) {
      entry.album_count = new Set(
        state.tracks
          .filter((track) => track.kind === 'track' && albumOwnerCredits(track).some((credit) => credit.name === entry.artist))
          .map((track) => track.album),
      ).size
    }
    const artists = sortPlayed([...seen.values()], url, (entry) =>
      playStats(state.tracks.filter((track) => track.kind === 'track' && trackCredits(track).some((credit) => credit.name === entry.artist))),
    )
    // Same full=1 semantics as the albums handler above.
    if (url.searchParams.get('full') !== '1') {
      const offset = Number(url.searchParams.get('offset') ?? 0)
      const limit = Number(url.searchParams.get('limit') ?? artists.length)
      await json(route, artists.slice(offset, offset + limit))
    } else {
      await json(route, artists)
    }
    return true
  }
  if (path === '/api/library/media') {
    // Like the server: `ids` is at most 20,000 characters (422 beyond) and
    // only its first 2,000 ids are read.
    const raw = url.searchParams.get('ids') ?? ''
    if (raw.length > 20_000) {
      await json(route, { detail: 'String should have at most 20000 characters' }, 422)
      return true
    }
    const ids = raw
      .split(',')
      .filter(Boolean)
      .map(Number)
      .slice(0, 2_000)
    await json(route, mediaForIds(state, ids))
    return true
  }
  if (path === '/api/library/videos/folders') {
    const parent = url.searchParams.get('parent') ?? ''
    await json(
      route,
      parent ? [] : [{ folder: 'Shows', path: 'Shows', video_count: 1, representative_media_item_id: 5 }],
    )
    return true
  }
  if (path === '/api/library/videos') {
    await json(route, filteredTracks(state, url, true).map((v) => ({ ...v, ...artFields('media_item', v.id) })))
    return true
  }
  if (path === '/api/library/files/search') {
    const rootId = Number(url.searchParams.get('root_id'))
    const query = (url.searchParams.get('q') ?? '').toLowerCase()
    if (rootId === 1 && query.includes('disc')) {
      await json(route, {
        root_id: rootId,
        folders: [browseDirectory(state, 'Disc 1', 'Disc 1')],
        items: [],
        truncated: false,
      })
    } else if (rootId === 1 && query.includes('bohemian')) {
      await json(route, {
        root_id: rootId,
        folders: [],
        items: [{ ...state.tracks[0], relative_path: 'Disc 1/01 - Bohemian Rhapsody.flac' }],
        truncated: false,
      })
    } else {
      await json(route, { root_id: rootId, folders: [], items: [], truncated: false })
    }
    return true
  }
  const ignoredFolderRoute = path.match(/^\/api\/library\/roots\/(\d+)\/ignored-folders$/)
  if (ignoredFolderRoute && (method === 'POST' || method === 'DELETE')) {
    const folderPath = String(body.path ?? '')
    if (method === 'POST' && !state.ignoredFolderPaths.includes(folderPath)) {
      state.ignoredFolderPaths.push(folderPath)
    } else if (method === 'DELETE') {
      state.ignoredFolderPaths = state.ignoredFolderPaths.filter((item) => item !== folderPath)
    }
    await json(route, method === 'POST' ? { path: folderPath } : { ok: true })
    return true
  }
  if (path === '/api/library/browse') {
    const rootId = Number(url.searchParams.get('root_id'))
    const pathName = url.searchParams.get('path') ?? ''
    if (rootId === 1 && pathName === '') {
      await json(route, {
        root_id: 1,
        path: '',
        parent_path: null,
        directories: [browseDirectory(state, 'Disc 1', 'Disc 1')],
        items: [state.tracks[0]],
      })
    } else if (rootId === 1 && pathName === 'Disc 1') {
      await json(route, {
        root_id: 1,
        path: 'Disc 1',
        parent_path: '',
        directories: [],
        items: [state.tracks[0], state.tracks[1]],
      })
    } else if (rootId === 2 && pathName === '') {
      await json(route, {
        root_id: 2,
        path: '',
        parent_path: null,
        directories: [browseDirectory(state, 'Shows', 'Shows')],
        items: [state.tracks[4]],
      })
    } else {
      await json(route, {
        root_id: rootId,
        path: pathName,
        parent_path: null,
        directories: [],
        items: [],
      })
    }
    return true
  }
  if (path === '/api/library/recently-added') {
    await json(route, [state.tracks[0], state.tracks[2]])
    return true
  }
  if (path === '/api/library/recently-played') {
    await json(route, [state.tracks[1]])
    return true
  }
  if (path === '/api/library/recently-played-sources') {
    const sources: Array<{
      kind: string
      label: string
      payload: Record<string, unknown>
      updated_at: string
      art_media_item_id: number | null
      preview_media_item_ids: number[]
    }> = [
      {
        kind: 'album',
        label: 'A Night at the Opera',
        payload: { artist: 'Queen', album: 'A Night at the Opera' },
        updated_at: '2026-01-05T00:00:00Z',
        art_media_item_id: 1,
        preview_media_item_ids: [],
      },
      {
        kind: 'artist',
        label: 'Queen',
        payload: { artist: 'Queen' },
        updated_at: '2026-01-04T00:00:00Z',
        art_media_item_id: 1,
        preview_media_item_ids: [],
      },
      {
        kind: 'playlist',
        label: 'Workout',
        payload: { playlist_id: 2 },
        updated_at: '2026-01-03T00:00:00Z',
        art_media_item_id: null,
        preview_media_item_ids: [1, 2],
      },
      {
        kind: 'radio',
        label: 'Radio Paradise',
        payload: { station_id: 10 },
        updated_at: '2026-01-02T00:00:00Z',
        art_media_item_id: null,
        preview_media_item_ids: [],
      },
      {
        kind: 'library',
        label: 'Rock songs',
        payload: { filters: { genre: 'Rock' } },
        updated_at: '2026-01-01T00:00:00Z',
        art_media_item_id: 1,
        preview_media_item_ids: [],
      },
    ]
    if (!state.mixesRefreshed) {
      sources.unshift({
        kind: 'playlist',
        label: 'Rock Mix',
        payload: { playlist_id: 4, auto_kind: 'mix' },
        updated_at: '2026-01-06T00:00:00Z',
        art_media_item_id: 1,
        preview_media_item_ids: [],
      })
    }
    await json(route, sources)
    return true
  }
  if (path === '/api/library/play-source' && method === 'POST') {
    await empty(route)
    return true
  }
  return false
}
