import { clearBrowseCache, request } from './api'
import type { components } from './api-types'
import type { MediaItem } from './types'

// Shapes come from api-types.ts, generated from the server's OpenAPI document
// (as in types.ts); only what the client narrows is written here.
type Schemas = components['schemas']

/** One want-list entry (app.want_list.entry_out). `title` is the album or
 * song title, the artist again for an artist entry. */
export type WantEntry = Schemas['WantEntryOut']

export type WantedArtistSummary = Schemas['WantedArtistOut']

export type MissingReleaseGroup = Schemas['MissingReleaseGroupOut']

/** `refreshing`: saved data was shown at once; the server is re-checking it
 * in the background. */
export type WantedArtistDetail = Schemas['WantedArtistDetailOut']

export type CatalogStatus = Schemas['CatalogReleaseGroupOut']['status']

export type OwnedAlbumRef = Schemas['OwnedAlbumOut']

export type CatalogReleaseGroup = Schemas['CatalogReleaseGroupOut']

/** `refreshing`: as on WantedArtistDetail. */
export type CatalogArtist = Schemas['CatalogArtistOut']

export type CatalogTrack = Schemas['CatalogTrackOut']

export type CatalogAlbum = Schemas['CatalogAlbumOut']

export type SearchArtist = Schemas['WantSearchArtistOut']

export type SearchAlbum = Schemas['WantSearchAlbumOut']

export type SearchTrack = Schemas['WantSearchTrackOut']

export type WantSearchResults = Schemas['WantSearchOut']

/** The request body. The generated type marks `excluded` required (the
 * schema is generated in its response form, where a default is always
 * present); the server defaults it to false, so it may be left out. */
export type WantInput = Omit<Schemas['WantIn'], 'excluded'> & { excluded?: boolean }

function qs(params: Record<string, string | boolean | null | undefined>): string {
  const query = new URLSearchParams(
    Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== false)
      .map(([k, v]) => [k, String(v)]),
  ).toString()
  return query ? `?${query}` : ''
}

// Want-list listeners (the tab, a catalog page, a missing-track row) refresh
// when anything is added or removed anywhere.
const listeners = new Set<() => void>()

export function onWantListChange(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** The want list changed somewhere outside wantApi (a radio song added from
 * Now Playing): views showing it load it again. */
export function notifyWantListChanged() {
  changed()
}

function changed() {
  clearBrowseCache()
  listeners.forEach((listener) => listener())
}

/** A want-list answer and whether the server is still re-matching the list
 * against the library in the background (`X-Want-List-Matching: 1`): poll
 * `matchStatus` and load it again once that is done. */
export interface MatchingAnswer<T> {
  rows: T
  matching: boolean
}

async function withMatching<T>(path: string): Promise<MatchingAnswer<T>> {
  let matching = false
  const rows = await request<T>(path, undefined, {
    onHeaders: (headers) => {
      matching = headers.get('X-Want-List-Matching') === '1'
    },
  })
  return { rows, matching }
}

export const wantApi = {
  artists: (allUsers = false) => withMatching<WantedArtistSummary[]>(`/api/want-list/artists${qs({ all_users: allUsers })}`),
  artist: (name: string, mbid?: string | null, allUsers = false) =>
    request<WantedArtistDetail>(`/api/want-list/artist${qs({ name, mbid, all_users: allUsers })}`),
  entries: () => withMatching<WantEntry[]>('/api/want-list/entries'),
  /** `refreshing`: a background re-match of the want list is queued or running. */
  matchStatus: (allUsers = false) =>
    request<Schemas['RefreshingOut']>(`/api/want-list/match-status${qs({ all_users: allUsers })}`),
  add: async (input: WantInput) => {
    const entry = await request<WantEntry>('/api/want-list', { method: 'POST', body: JSON.stringify(input) })
    changed()
    return entry
  },
  remove: async (id: number) => {
    await request<Schemas['OkOut']>(`/api/want-list/${id}`, { method: 'DELETE' })
    changed()
  },
  clearOwned: async () => {
    const result = await request<Schemas['RemovedCountOut']>('/api/want-list/clear-owned', { method: 'POST' })
    changed()
    return result
  },
  exportUrl: (format: 'csv' | 'json', allUsers = false) => `/api/want-list/export${qs({ format, all_users: allUsers })}`,
  search: (q: string) => request<WantSearchResults>(`/api/want-list/search${qs({ q })}`),
  catalogArtist: (params: { mbid?: string | null; name?: string | null; refresh?: boolean }) =>
    request<CatalogArtist>(`/api/catalog/artist${qs(params)}`),
  artistRefreshStatus: (mbid: string) =>
    request<Schemas['RefreshingOut']>(`/api/catalog/artist/refresh-status${qs({ mbid })}`),
  catalogAlbum: (mbid: string, artist?: string | null, refresh = false) =>
    request<CatalogAlbum>(`/api/catalog/release-group/${encodeURIComponent(mbid)}${qs({ artist, refresh })}`),
}

/** A track row that could also be a library song. */
export type MaybeOwned = { media_item_id: number | null } | { media_item: MediaItem | null }
