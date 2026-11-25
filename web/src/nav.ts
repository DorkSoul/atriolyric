import { settingsSectionFromSlug, settingsSectionSlug } from './settingsSections'

export type Tab =
  | 'home'
  | 'songs'
  | 'albums'
  | 'artists'
  | 'library-info'
  | 'recent-played'
  | 'videos'
  | 'files'
  | 'radio'
  | 'playlists'
  | 'now-playing'
  | 'want-list'
  | 'admin'
  | 'settings'

const VALID_TABS: Tab[] = [
  'home',
  'songs',
  'albums',
  'artists',
  'library-info',
  'recent-played',
  'videos',
  'files',
  'radio',
  'playlists',
  'now-playing',
  'want-list',
  'admin',
  'settings',
]

export interface NavState {
  tab: Tab
  browseArtist?: string
  browseAlbum?: { artist: string | null; album: string }
  browseVideoFolder?: string
  filesRootId?: number
  filesPath?: string
  playlistId?: number
  /** Home's search box text, so Back and refresh keep the results. */
  homeQuery?: string
  /** The Settings section to land on (an id from settingsSections.ts). */
  settingsSection?: string
  /** Where in the Want list tab (its search, a wanted artist, a catalog
   * artist or album). */
  want?: WantNav
}

export type WantNav =
  | { page: 'home'; query?: string }
  | { page: 'artist'; name: string; mbid?: string }
  | { page: 'catalog'; mbid: string; name?: string }
  | { page: 'album'; mbid: string; artist?: string }

/** An album's owner as a path segment. `-` alone is "no artist"; an artist
 * whose name is literally "-" is written `%2D`, which decodes to the same
 * name and cannot be mistaken for it. */
function ownerSegment(artist: string | null): string {
  if (!artist) return '-'
  return artist === '-' ? '%2D' : encodeURIComponent(artist)
}

/** Encodes navigation state into a URL hash, so both the browser's
 * back/forward buttons and a plain page refresh land back on the exact
 * screen the user was looking at - hash fragments never reach the server,
 * so this needs no backend routing support. */
export function navToHash(nav: NavState): string {
  switch (nav.tab) {
    case 'home': {
      if (nav.homeQuery) return `#/home/${encodeURIComponent(nav.homeQuery)}`
      return '#/home'
    }
    case 'artists': {
      if (nav.browseArtist && nav.browseAlbum) {
        const page = `#/artists/${encodeURIComponent(nav.browseArtist)}/${encodeURIComponent(nav.browseAlbum.album)}`
        // An "Appears on" album belongs to someone else (or to nobody): its
        // owner is a further segment, so a reload or Back asks for the same
        // album. The artist's own albums keep the short address.
        const owner = nav.browseAlbum.artist
        return owner === nav.browseArtist ? page : `${page}/${ownerSegment(owner)}`
      }
      if (nav.browseArtist) return `#/artists/${encodeURIComponent(nav.browseArtist)}`
      return '#/artists'
    }
    case 'albums': {
      if (nav.browseAlbum) {
        return `#/albums/${ownerSegment(nav.browseAlbum.artist)}/${encodeURIComponent(nav.browseAlbum.album)}`
      }
      return '#/albums'
    }
    case 'videos': {
      if (nav.browseVideoFolder) return `#/videos/${encodeURIComponent(nav.browseVideoFolder)}`
      return '#/videos'
    }
    case 'files': {
      if (nav.filesRootId != null) {
        const pathPart = nav.filesPath ? `/${encodeURIComponent(nav.filesPath)}` : ''
        return `#/files/${nav.filesRootId}${pathPart}`
      }
      return '#/files'
    }
    case 'playlists': {
      if (nav.playlistId != null) return `#/playlists/${nav.playlistId}`
      return '#/playlists'
    }
    case 'want-list': {
      const want = nav.want
      const e = encodeURIComponent
      if (!want || want.page === 'home') return want?.query ? `#/want-list/search/${e(want.query)}` : '#/want-list'
      if (want.page === 'artist') return `#/want-list/artist/${e(want.name)}${want.mbid ? `/${e(want.mbid)}` : ''}`
      if (want.page === 'catalog') return `#/want-list/catalog/${e(want.mbid)}${want.name ? `/${e(want.name)}` : ''}`
      return `#/want-list/album/${e(want.mbid)}${want.artist ? `/${e(want.artist)}` : ''}`
    }
    case 'settings': {
      return nav.settingsSection ? `#/settings/${settingsSectionSlug(nav.settingsSection)}` : '#/settings'
    }
    default:
      return `#/${nav.tab}`
  }
}

export function hashToNav(hash: string): NavState {
  const clean = hash.replace(/^#\/?/, '')
  const parts = clean.split('/').filter(Boolean)
  // A Recently added address opens Library info, which holds that list.
  const tab = (parts[0] === 'recent-added' ? 'library-info' : parts[0] || 'home') as Tab
  if (!VALID_TABS.includes(tab)) return { tab: 'home' }
  const decode = (value: string | undefined) => {
    if (!value) return undefined
    try { return decodeURIComponent(value) } catch { return undefined }
  }
  // The reverse of ownerSegment: a bare `-` is "no artist".
  const decodeOwner = (value: string) => (value === '-' ? null : decode(value) ?? null)

  if (tab === 'home') {
    return { tab, homeQuery: decode(parts[1]) }
  }
  if (tab === 'artists') {
    const artist = decode(parts[1])
    const album = decode(parts[2])
    // No owner segment: the page's artist owns the album (and every address
    // without the segment reads that way).
    const owner = parts[3] === undefined ? artist ?? null : decodeOwner(parts[3])
    return {
      tab,
      browseArtist: artist,
      browseAlbum: artist && album ? { artist: owner, album } : undefined,
    }
  }
  if (tab === 'albums') {
    const artistPart = parts[1]
    const album = decode(parts[2])
    if (artistPart && album) {
      return {
        tab,
        browseAlbum: { artist: decodeOwner(artistPart), album },
      }
    }
    return { tab }
  }
  if (tab === 'videos') {
    const folder = decode(parts[1])
    return { tab, browseVideoFolder: folder }
  }
  if (tab === 'files') {
    const rootId = parts[1] ? Number(parts[1]) : NaN
    const path = decode(parts[2])
    return { tab, filesRootId: Number.isFinite(rootId) ? rootId : undefined, filesPath: path }
  }
  if (tab === 'settings') {
    return { tab, settingsSection: settingsSectionFromSlug(decode(parts[1])) }
  }
  if (tab === 'playlists') {
    const playlistId = parts[1] ? Number(parts[1]) : NaN
    return { tab, playlistId: Number.isFinite(playlistId) ? playlistId : undefined }
  }
  if (tab === 'want-list') {
    const [page, first, second] = [parts[1], decode(parts[2]), decode(parts[3])]
    if (page === 'search' && first) return { tab, want: { page: 'home', query: first } }
    if (page === 'artist' && first) return { tab, want: { page: 'artist', name: first, mbid: second } }
    if (page === 'catalog' && first) return { tab, want: { page: 'catalog', mbid: first, name: second } }
    if (page === 'album' && first) return { tab, want: { page: 'album', mbid: first, artist: second } }
    return { tab, want: { page: 'home' } }
  }
  return { tab }
}
