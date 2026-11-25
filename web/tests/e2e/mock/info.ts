// Artist, album and track info, and lyrics.

import { type RouteContext, json, takePending } from './http'
import { mediaForIds } from './library'
import type { AppMockState, MediaItem } from './types'

export const QUEEN_MBID = '0383dadf-2a4e-4d10-a46a-e9e041da8eb3'
export const QUEEN_TRIBUTE_MBID = '11111111-2222-3333-4444-555555555555'

export function mockArtistInfo(state: AppMockState, name: string) {
  const isQueen = name === 'Queen'
  const fixed = name in state.artistMatches
  const mbid = fixed ? state.artistMatches[name] : isQueen ? QUEEN_MBID : null
  const tribute = mbid === QUEEN_TRIBUTE_MBID
  const base = {
    name,
    mbid,
    mbid_locked: fixed && mbid != null,
    disambiguation: null as string | null,
    type: null as string | null,
    gender: null,
    country: null as string | null,
    area: null as string | null,
    begin_area: null as string | null,
    begin: null as string | null,
    end: null,
    ended: false,
    listeners: null as number | null,
    playcount: null as number | null,
    listenbrainz_url: null as string | null,
    tags: [] as string[],
    bio: null as Record<string, unknown> | null,
    members: [] as Record<string, unknown>[],
    member_of: [],
    links: [] as Record<string, unknown>[],
    similar: [] as Record<string, unknown>[],
    sources: [] as string[],
    image_credit: null as Record<string, unknown> | null,
  }
  if (!isQueen || mbid == null) return base
  if (tribute) {
    return { ...base, disambiguation: 'tribute band', type: 'Group', country: 'GB', sources: ['MusicBrainz'] }
  }
  return {
    ...base,
    disambiguation: 'UK rock group',
    type: 'Group',
    country: 'GB',
    area: 'United Kingdom',
    begin_area: 'London',
    begin: '1970',
    listeners: 6_100_000,
    playcount: 312_000_000,
    listenbrainz_url: `https://listenbrainz.org/artist/${QUEEN_MBID}/`,
    tags: ['classic rock', 'rock', 'british'],
    bio: {
      text: 'Queen were a British rock band formed in London in 1970.\n\nTheir classic line-up was <b>Freddie Mercury</b>, Brian May, Roger Taylor and John Deacon. <a href="https://example.org/queen-more">Read more on Wikipedia</a>',
      html: true,
      source: 'Wikipedia',
      url: 'https://en.wikipedia.org/wiki/Queen_(band)',
    },
    members: [
      { name: 'Freddie Mercury', mbid: 'm-1', begin: '1970', end: '1991', ended: true, attributes: ['lead vocals', 'piano'], in_library: false },
      { name: 'Brian May', mbid: 'm-2', begin: '1970', end: null, ended: false, attributes: ['guitar'], in_library: true },
    ],
    links: [
      { type: 'official homepage', label: 'Official site', url: 'https://queenonline.com/' },
      { type: 'wikipedia', label: 'Wikipedia', url: 'https://en.wikipedia.org/wiki/Queen_(band)' },
    ],
    similar: [
      { name: 'M83', in_library: true },
      { name: 'David Bowie', in_library: false },
    ],
    sources: ['MusicBrainz', 'ListenBrainz'],
    image_credit: {
      author: 'Jane Photographer',
      license: 'CC BY-SA 4.0',
      license_url: 'https://creativecommons.org/licenses/by-sa/4.0',
      page_url: 'https://commons.wikimedia.org/wiki/File:Queen.jpg',
    },
  }
}

export function mockArtistTopTracks(state: AppMockState, name: string) {
  if (name !== 'Queen') return { available: true, tracks: [] }
  const owned = (id: number) => mediaForIds(state, [id])[0] ?? null
  const rows: Array<[string, number, MediaItem | null]> = [
    ['Bohemian Rhapsody', 4_800_000, owned(1)],
    ['Another One Bites the Dust', 3_100_000, null],
    ["Don't Stop Me Now", 2_900_000, owned(2)],
    ['Under Pressure', 2_500_000, null],
    ['We Will Rock You', 2_200_000, null],
    ['Somebody to Love', 1_900_000, null],
    ['Radio Ga Ga', 1_400_000, null],
    ['Killer Queen', 1_100_000, null],
  ]
  return {
    available: true,
    tracks: rows.map(([title, playcount, mediaItem], index) => ({
      title,
      mbid: null,
      playcount,
      listeners: Math.round(playcount / 10),
      rank: index + 1,
      media_item: mediaItem,
    })),
  }
}

export function mockAlbumInfo(state: AppMockState, artist: string | null, album: string) {
  const chosen = state.albumEditions[`${artist ?? ''}::${album}`] ?? null
  const editions = [
    { mbid: 'rel-uk-1975', title: album, date: '1975-11-21', country: 'GB', status: 'Official', disambiguation: null, track_count: 12, format: 'Vinyl' },
    { mbid: 'rel-2011-remaster', title: album, date: '2011-03-14', country: 'XE', status: 'Official', disambiguation: '2011 remaster', track_count: 17, format: 'CD' },
  ]
  const release = chosen ?? 'rel-uk-1975'
  const edition = editions.find((item) => item.mbid === release) ?? editions[0]
  return {
    artist,
    album,
    release_group_mbid: 'rg-opera',
    release_mbid: release,
    primary_type: 'Album',
    secondary_types: [],
    section: 'Albums',
    original_release_date: '1975-11-21',
    edition_date: edition.date,
    country: edition.country,
    barcode: release === 'rel-2011-remaster' ? '602527645611' : null,
    labels: [{ label: release === 'rel-2011-remaster' ? 'Island' : 'EMI', catalog_number: release === 'rel-2011-remaster' ? '2764561' : 'EMTC 103' }],
    status: 'Official',
    listeners: 1_500_000,
    playcount: 45_000_000,
    listenbrainz_url: 'https://listenbrainz.org/album/rg-opera/',
    tags: ['classic rock', '1975'],
    wiki: null,
    editions,
    release_locked: chosen != null,
  }
}

export function mockTrackInfo(state: AppMockState, id: number) {
  const item = state.tracks.find((track) => track.id === id)
  if (!item) return null
  return {
    id,
    title: item.title,
    artist: item.artist,
    album: item.album,
    listeners: 340_000,
    playcount: 1_234_567,
    library_playcount: 17,
    listenbrainz_url: 'https://listenbrainz.org/track/rec-bohemian/',
    tags: ['rock', 'opera'],
    wiki: null,
    recording_mbid: 'rec-bohemian',
    artist_mbid: QUEEN_MBID,
    release_mbid: 'rel-uk-1975',
    release_group_mbid: 'rg-opera',
    isrc: 'GBUM71029604',
    original_release_date: '1975-10-31',
    release_date: '1975-11-21',
    duration_seconds: item.duration_seconds,
    credits: [
      { role: 'composer', name: 'Freddie Mercury', detail: null },
      { role: 'producer', name: 'Roy Thomas Baker', detail: null },
      { role: 'producer', name: 'Queen', detail: null },
      { role: 'instrument', name: 'Brian May', detail: 'guitar' },
    ],
    works: ['Bohemian Rhapsody'],
  }
}

/** Artist, album and track info. */
export async function handleInfoRoutes({ state, route, url, method, path, body }: RouteContext): Promise<boolean> {
  if (path === '/api/library/artist-info') {
    const name = url.searchParams.get('name') ?? ''
    const info = mockArtistInfo(state, name)
    // Pending: what is stored (the MusicBrainz match) without ListenBrainz's part.
    if (takePending(state, `artist-info:${name}`)) {
      await json(route, { ...info, listeners: null, playcount: null, bio: null, tags: [], similar: [], pending: true })
    } else {
      await json(route, { ...info, pending: false })
    }
    return true
  }
  if (path === '/api/library/artist-top-tracks') {
    const name = url.searchParams.get('name') ?? ''
    if (takePending(state, `top-tracks:${name}`)) {
      await json(route, { available: true, tracks: [], pending: true })
    } else {
      await json(route, { ...mockArtistTopTracks(state, name), pending: false })
    }
    return true
  }
  if (path === '/api/library/artist-match-candidates') {
    await json(route, [
      { mbid: QUEEN_MBID, name: 'Queen', disambiguation: 'UK rock group', type: 'Group', country: 'GB', begin: '1970', score: 100 },
      { mbid: QUEEN_TRIBUTE_MBID, name: 'Queen', disambiguation: 'tribute band', type: 'Group', country: 'GB', begin: '2001', score: 80 },
    ])
    return true
  }
  if (path === '/api/library/artist-match' && method === 'PUT') {
    const name = String(body.name ?? '')
    state.artistMatches[name] = typeof body.mbid === 'string' ? body.mbid : null
    await json(route, mockArtistInfo(state, name))
    return true
  }
  if (path === '/api/library/album-info') {
    const album = url.searchParams.get('album') ?? ''
    const info = mockAlbumInfo(state, url.searchParams.get('artist'), album)
    if (takePending(state, `album-info:${album}`)) {
      await json(route, { ...info, listeners: null, playcount: null, wiki: null, tags: [], pending: true })
    } else {
      await json(route, { ...info, pending: false })
    }
    return true
  }
  if (path === '/api/library/album-edition' && method === 'PUT') {
    const artist = typeof body.artist === 'string' ? body.artist : null
    const album = String(body.album ?? '')
    state.albumEditions[`${artist ?? ''}::${album}`] = typeof body.release_mbid === 'string' ? body.release_mbid : null
    await json(route, mockAlbumInfo(state, artist, album))
    return true
  }
  const mediaInfoMatch = path.match(/^\/api\/library\/media\/(\d+)\/info$/)
  if (mediaInfoMatch) {
    const id = Number(mediaInfoMatch[1])
    const info = mockTrackInfo(state, id)
    const shaped = info
    if (!shaped) await json(route, { detail: 'Not found' }, 404)
    else if (takePending(state, `track-info:${id}`)) {
      await json(route, { ...shaped, playcount: null, listeners: null, wiki: null, tags: [], pending: true })
    } else await json(route, { ...shaped, pending: false })
    return true
  }
  return false
}

/** Lyrics. */
export async function handleLyricsRoutes({ state, route, path }: RouteContext): Promise<boolean> {
  const lyricsMatch = path.match(/^\/api\/library\/media\/(\d+)\/lyrics$/)
  if (lyricsMatch && takePending(state, `lyrics:${lyricsMatch[1]}`)) {
    // Not looked up yet: the server fetches them and says so.
    await json(route, { plain: null, synced: null, source: null, pending: true })
    return true
  }
  if (path === '/api/library/media/1/lyrics') {
    await json(route, {
      plain: '[ar:Queen]\n[ti:Bohemian Rhapsody]\n[00:01.00] Is this the real life?\n[00:10.50] Is this just fantasy?\n[00:20:25]Caught in a landslide\n[00:30] No escape from reality',
      synced: null,
      source: 'lrclib',
    })
    return true
  }
  if (path.match(/^\/api\/library\/media\/\d+\/lyrics$/)) {
    await json(route, { plain: null, synced: null, source: null })
    return true
  }
  return false
}
