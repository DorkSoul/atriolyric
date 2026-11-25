import type { Page, Route } from 'playwright/test'

// Mock /api/want-list and /api/catalog, installed after installApiMock (a
// later page.route wins), with its own small state.

interface Entry {
  id: number
  kind: 'artist' | 'album' | 'track'
  artist: string
  title: string
  album: string | null
  station_name: string | null
  artist_mbid: string | null
  release_group_mbid: string | null
  recording_mbid: string | null
  release_type: string | null
  excluded: boolean
  media_item_id: number | null
  owned_count: number | null
  total_count: number | null
  owned: boolean
  created_at: string
}

export interface WantMockState {
  entries: Entry[]
  searches: string[]
  nextId: number
  /** The server's background re-match: while `matching` is true the list
   * routes say so (`X-Want-List-Matching: 1`, `matching: true`), and
   * /match-status reports `refreshing` for `matchStatusAsks` more asks, then
   * ends the re-match. */
  matching: boolean
  matchStatusAsks: number
  /** How many more first looks at a catalog artist / release group answer
   * with the `pending` placeholder before the real answer. */
  catalogPendingAsks: number
  releaseGroupPendingAsks: number
  /** Credit stored with the wanted artist's picture (a Commons photo). */
  artistArtCredit: { author: string | null; license: string | null; license_url: string | null; page_url: string | null } | null
}

const ARTIST_MBID = 'mb-bowie'

const GROUPS = [
  { mbid: 'rg-hunky', title: 'Hunky Dory', primary_type: 'Album', secondary_types: [], section: 'Album', first_release_date: '1971-12-17', disambiguation: null },
  { mbid: 'rg-ziggy', title: 'Ziggy Stardust', primary_type: 'Album', secondary_types: [], section: 'Album', first_release_date: '1972-06-16', disambiguation: null },
  { mbid: 'rg-heroes-single', title: 'Heroes', primary_type: 'Single', secondary_types: [], section: 'Single', first_release_date: '1977-09-23', disambiguation: null },
]

function json(route: Route, body: unknown, status = 200, headers?: Record<string, string>) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body), headers })
}

export async function installWantListMock(page: Page): Promise<WantMockState> {
  const state: WantMockState = {
    entries: [],
    searches: [],
    nextId: 1,
    matching: false,
    matchStatusAsks: 0,
    catalogPendingAsks: 0,
    releaseGroupPendingAsks: 0,
    artistArtCredit: null,
  }
  const matchingHeaders = () => (state.matching ? { 'X-Want-List-Matching': '1' } : undefined)

  const wanted = (kind: Entry['kind'], key: (e: Entry) => string | null, value: string) =>
    state.entries.find((e) => e.kind === kind && !e.excluded && key(e) === value)
  const excluded = (mbid: string) => state.entries.some((e) => e.kind === 'album' && e.excluded && e.release_group_mbid === mbid)
  const artistWanted = () => Boolean(wanted('artist', (e) => e.artist_mbid, ARTIST_MBID))

  function catalogGroup(group: (typeof GROUPS)[number]) {
    const owned = group.mbid === 'rg-hunky'
    const entry = wanted('album', (e) => e.release_group_mbid, group.mbid)
    return {
      ...group,
      status: owned ? 'owned' : group.mbid === 'rg-ziggy' ? 'partial' : 'missing',
      owned_count: group.mbid === 'rg-ziggy' ? 1 : null,
      total_count: group.mbid === 'rg-ziggy' ? 3 : null,
      owned_album: owned ? { artist: 'David Bowie', album: 'Hunky Dory' } : null,
      wanted: Boolean(entry) || (artistWanted() && !owned && !excluded(group.mbid)),
      excluded: excluded(group.mbid),
      want_entry_id: entry?.id ?? null,
    }
  }

  await page.route(/\/api\/(want-list|catalog)(\/|\?|$)/, async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const method = request.method()

    if (path === '/api/want-list/search') {
      state.searches.push(url.searchParams.get('q') ?? '')
      await new Promise((resolve) => setTimeout(resolve, 150))
      return json(route, {
        artists: [
          { name: 'David Bowie', mbid: ARTIST_MBID, disambiguation: null, type: 'Person', country: 'GB', in_library: false, library_name: null, wanted: artistWanted() },
          { name: 'Queen', mbid: 'mb-queen', disambiguation: 'UK rock group', type: 'Group', country: 'GB', in_library: true, library_name: 'Queen', wanted: false },
        ],
        albums: [
          { title: 'Ziggy Stardust', artist: 'David Bowie', artist_mbid: ARTIST_MBID, release_group_mbid: 'rg-ziggy', primary_type: 'Album', section: 'Album', year: '1972', in_library: false, owned_album: null, wanted: Boolean(wanted('album', (e) => e.release_group_mbid, 'rg-ziggy')) },
        ],
        tracks: [
          { title: 'Starman', artist: 'David Bowie', artist_mbid: ARTIST_MBID, recording_mbid: 'rec-starman', album: 'Ziggy Stardust', release_group_mbid: 'rg-ziggy', year: '1972', length_seconds: 254, in_library: false, media_item_id: null, wanted: Boolean(wanted('track', (e) => e.recording_mbid, 'rec-starman')) },
        ],
      })
    }
    if (path === '/api/want-list' && method === 'POST') {
      const body = JSON.parse(request.postData() ?? '{}')
      const existing = state.entries.find(
        (e) =>
          e.kind === body.kind &&
          ((body.release_group_mbid && e.release_group_mbid === body.release_group_mbid && body.kind === 'album') ||
            (body.recording_mbid && e.recording_mbid === body.recording_mbid) ||
            (body.kind === 'artist' && e.artist_mbid === body.artist_mbid)),
      )
      const entry: Entry = existing ?? {
        id: state.nextId++,
        kind: body.kind,
        artist: body.artist,
        title: body.title ?? body.artist,
        album: body.album ?? null,
        station_name: body.station_name ?? null,
        artist_mbid: body.artist_mbid ?? null,
        release_group_mbid: body.release_group_mbid ?? null,
        recording_mbid: body.recording_mbid ?? null,
        release_type: body.release_type ?? null,
        excluded: false,
        media_item_id: null,
        owned_count: body.kind === 'album' ? 1 : null,
        total_count: body.kind === 'album' ? 3 : null,
        owned: false,
        created_at: '2026-10-29T00:00:00Z',
      }
      entry.excluded = Boolean(body.excluded)
      if (!existing) state.entries.push(entry)
      return json(route, entry)
    }
    const deleteMatch = path.match(/^\/api\/want-list\/(\d+)$/)
    if (deleteMatch && method === 'DELETE') {
      state.entries = state.entries.filter((e) => e.id !== Number(deleteMatch[1]))
      return json(route, { ok: true })
    }
    if (path === '/api/want-list/artists') {
      const byArtist = new Map<string, Entry[]>()
      for (const e of state.entries.filter((e) => !e.excluded)) byArtist.set(e.artist, [...(byArtist.get(e.artist) ?? []), e])
      return json(
        route,
        [...byArtist.entries()].map(([artist, rows]) => ({
          artist,
          artist_mbid: rows.find((r) => r.artist_mbid)?.artist_mbid ?? null,
          wants_artist: rows.some((r) => r.kind === 'artist'),
          album_count: rows.filter((r) => r.kind === 'album').length,
          track_count: rows.filter((r) => r.kind === 'track').length,
          owned_count: 0,
          users: url.searchParams.get('all_users') ? ['dork_soul'] : null,
          in_library: false,
          library_name: null,
          representative_media_item_id: null,
          updated_at: null,
        })),
        200,
        matchingHeaders(),
      )
    }
    if (path === '/api/want-list/match-status') {
      if (state.matching && state.matchStatusAsks > 0) state.matchStatusAsks -= 1
      else state.matching = false
      return json(route, { refreshing: state.matching })
    }
    if (path === '/api/want-list/artist') {
      const name = url.searchParams.get('name') ?? ''
      const rows = state.entries.filter((e) => e.artist === name)
      const artistEntry = rows.find((e) => e.kind === 'artist') ?? null
      return json(route, {
        artist: name,
        artist_mbid: rows.find((r) => r.artist_mbid)?.artist_mbid ?? null,
        artist_entry: artistEntry,
        missing_release_groups: artistEntry ? GROUPS.filter((g) => g.mbid !== 'rg-hunky' && !excluded(g.mbid)).map(catalogGroup) : null,
        albums: rows.filter((e) => e.kind === 'album'),
        tracks: rows.filter((e) => e.kind === 'track'),
        in_library: false,
        matching: state.matching,
      })
    }
    if (/^\/api\/catalog\/art\/artist\/[^/]+\/credit$/.test(path)) return json(route, state.artistArtCredit)
    if (path === '/api/want-list/clear-owned') return json(route, { removed: 0 })
    if (path === '/api/want-list/entries') return json(route, state.entries, 200, matchingHeaders())
    if (path === '/api/catalog/artist') {
      if (state.catalogPendingAsks > 0) {
        // A first look: an empty placeholder while the server reads MusicBrainz.
        state.catalogPendingAsks -= 1
        return json(route, {
          artist: null,
          artist_name: 'David Bowie',
          artist_mbid: ARTIST_MBID,
          in_library: false,
          artist_wanted: artistWanted(),
          release_groups: [],
          tracklists_known: 0,
          tracklists_total: 0,
          refreshing: true,
          pending: true,
        })
      }
      return json(route, {
        artist: null,
        artist_name: 'David Bowie',
        artist_mbid: ARTIST_MBID,
        in_library: false,
        artist_wanted: artistWanted(),
        release_groups: GROUPS.map(catalogGroup),
        tracklists_known: 1,
        tracklists_total: 3,
      })
    }
    const albumMatch = path.match(/^\/api\/catalog\/release-group\/(.+)$/)
    if (albumMatch && state.releaseGroupPendingAsks > 0) {
      // A first look: no title or tracks yet, fetched in the background.
      state.releaseGroupPendingAsks -= 1
      return json(route, {
        mbid: decodeURIComponent(albumMatch[1]),
        title: null,
        artist: 'David Bowie',
        artist_mbid: ARTIST_MBID,
        primary_type: null,
        secondary_types: [],
        section: '',
        first_release_date: null,
        release_mbid: null,
        tracks: [],
        owned_count: 0,
        wanted: false,
        want_entry_id: null,
        artist_wanted: artistWanted(),
        excluded: false,
        owned_album: null,
        pending: true,
      })
    }
    if (albumMatch) {
      const group = GROUPS.find((g) => g.mbid === decodeURIComponent(albumMatch[1])) ?? GROUPS[1]
      const entry = wanted('album', (e) => e.release_group_mbid, group.mbid)
      const tracks = ['Five Years', 'Starman', 'Suffragette City'].map((title, index) => {
        const recording = `rec-${title.toLowerCase().replace(/\s+/g, '-')}`
        const trackEntry = wanted('track', (e) => e.recording_mbid, recording)
        return {
          disc: 1,
          position: index + 1,
          title,
          recording_mbid: recording,
          length_seconds: 200 + index,
          artist: 'David Bowie',
          media_item_id: index === 0 ? 1 : null,
          wanted: Boolean(trackEntry) || (Boolean(entry) && index !== 0),
          want_entry_id: trackEntry?.id ?? null,
        }
      })
      return json(route, {
        mbid: group.mbid,
        title: group.title,
        artist: 'David Bowie',
        artist_mbid: ARTIST_MBID,
        primary_type: group.primary_type,
        secondary_types: [],
        section: group.section,
        first_release_date: group.first_release_date,
        release_mbid: 'rel-1',
        tracks,
        owned_count: 1,
        wanted: Boolean(entry),
        want_entry_id: entry?.id ?? null,
        artist_wanted: artistWanted(),
        excluded: excluded(group.mbid),
        owned_album: null,
      })
    }
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"not mocked"}' })
  })
  // Cover Art Archive thumbnails: an empty 404 shows the disc fallback.
  await page.route(/coverartarchive\.org/, (route) => route.fulfill({ status: 404, body: '' }))
  return state
}
