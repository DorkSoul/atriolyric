import type { WebSocketRoute } from 'playwright/test'
import type { components } from '../../../src/api-types'

// The mock's records are the server's response shapes (src/api-types.ts,
// generated from the OpenAPI document), so a renamed or removed server field
// breaks the mock at compile time. Fields the server always sends with a
// default may be left out of mock data; `WithDefaults` lists them per type.
export type Schemas = components['schemas']
export type WithDefaults<T, K extends keyof T> = Omit<T, K> & Partial<Pick<T, K>>

export type MediaKind = Schemas['MediaKind']

export type MediaItem = WithDefaults<
  Schemas['MediaItemOut'],
  | 'replaygain_track_gain'
  | 'replaygain_album_gain'
  | 'replaygain_track_peak'
  | 'playlist_item_id'
  | 'album_artist'
  | 'artists'
  | 'custom_art'
  | 'art_version'
  | 'year'
  | 'play_count'
> & {
  /** Mock-only: when the user last played it (epoch ms), for the Songs
   * tab's "Last played" sort. Not part of a media item on the wire. */
  last_played?: number
  /** Mock-only: the artist-page section of the album this track is on
   * (AlbumOut.section). Not part of a media item on the wire. */
  section?: string
}

export type ArtistCredit = Schemas['ArtistCreditOut']

export type Playlist = WithDefaults<Schemas['PlaylistOut'], 'description' | 'custom_art' | 'art_version'>

// total_count is derived from items when the mock answers (wireDetail).
export type PlaylistDetail = WithDefaults<
  Omit<Schemas['PlaylistDetailOut'], 'items' | 'total_count'>,
  'description' | 'custom_art' | 'art_version'
> & { items: MediaItem[] }

// A row of GET /api/library/mixes. Default mixes fill the Home rail up to its
// 13-item cap; specified mixes are the temporary extras the user asked for by
// name, which the server always sorts after the defaults.
export type MockMix = WithDefaults<Schemas['MixPlaylistOut'], 'custom_art' | 'art_version' | 'full_match_count'>

export type Station = Schemas['StationOut']

export type AdminUser = WithDefaults<Schemas['AdminUserOut'], 'accent_color'>

export type Root = Omit<Schemas['LibraryRootOut'], 'active_task' | 'last_task'> & {
  active_task: LibraryTask | null
  last_task: LibraryTask | null
}

// kind/status are plain text on the server; the web app's narrower unions.
export type LibraryTask = WithDefaults<
  Omit<Schemas['LibraryTaskOut'], 'kind' | 'status'>,
  'stage' | 'missing_held' | 'note'
> & {
  kind: 'scan' | 'mb_isrc' | 'mb_mbid' | 'lb_tags' | 'lb_tags_all' | 'enrich' | 'cover_art' | 'artist_info' | 'full_setup' | 'full_setup_new'
  status: 'running' | 'stopping' | 'stopped' | 'completed' | 'failed'
}

/** The server's snapshot without `current_queue_item_id`, which
 * `playbackSnapshot` derives when sending. `revision` grows by one with every
 * broadcast, like the server's. */
export type PlaybackState = Omit<Schemas['PlaybackStateSnapshot'], 'current_queue_item_id'>

export interface ApiCall {
  method: string
  path: string
  // Full path + query string. `path` alone can't distinguish e.g. the
  // albums page fetch from the full-list (full=1) fetch, which share a
  // pathname; tests that care about the query match on this.
  url: string
  body: Record<string, unknown>
}

export interface AppMockState {
  authenticated: boolean
  /** What /api/version reports; null like a development server. */
  appVersion: string | null
  needsSetup: boolean
  user: WithDefaults<Schemas['UserOut'], 'accent_color' | 'dismissed_notices' | 'online_recommendations'>
  password: string
  tracks: MediaItem[]
  artistSplitRules: Schemas['ArtistSplitRulesOut']
  playlists: Playlist[]
  playlistDetails: Record<number, PlaylistDetail>
  stations: Station[]
  wantList: Schemas['WantListOut'][]
  users: AdminUser[]
  roots: Root[]
  ignoredFolderPaths: string[]
  collectionPreferences: MockCollectionPreference[]
  discoveryState: Schemas['DiscoveryStateOut']
  discoverySettings: Schemas['DiscoverySettingsOut']
  integrations: WithDefaults<Schemas['IntegrationOut'], 'needs_reconnect'>[]
  /** Credits stored with entity art, by `type:key` (GET /api/art/.../credit). */
  artCredits: Record<string, Schemas['ImageCreditOut']>
  /** Library art saved before sources were recorded (Admin clean-up). */
  unrecordedArt: number
  playback: PlaybackState
  calls: ApiCall[]
  /** `METHOD /path` of every request no mock route answered (served as 501). */
  unmocked: string[]
  /** Messages and requests the page sent without a field the server
   * requires (see `playbackMessageViolation`, the `/played` route). The mock
   * answers them the way the server does - an error, nothing changed - and
   * fixtures.ts fails the test if this list is not empty. */
  protocolViolations: string[]
  wsMessages: Array<Record<string, unknown>>
  playbackSocket?: WebSocketRoute
  /** Socket message types the mock server records but does not act on yet,
   * as if it had not processed them; `releaseHeldPlaybackMessages` does. */
  holdPlaybackTypes: Set<string>
  /** Devices the server has not heard from lately: their rows still say
   * connected, but the server's claim rule treats them as offline (a socket
   * that went quiet without closing, ACTIVE_DEVICE_SILENT_SECONDS). */
  silentDeviceIds: Set<number>
  heldPlaybackMessages: Array<Record<string, unknown>>
  /** What a new playback socket meets: `accept` (the normal mock server) or
   * `refuse`: closed at once with code 1013, before the page sees it open,
   * like a handshake the server turned down. */
  playbackSocketMode: 'accept' | 'refuse'
  /** Every playback socket the page has opened, whatever happened to it. */
  playbackSocketConnections: number
  mixesRefreshed: boolean
  mixes: MockMix[]
  nextMixId: number
  nextPlaylistId: number
  nextStationId: number
  nextUserId: number
  nextRootId: number
  nextPlaylistItemId: number
  /** Queue rows get fresh ids like the server's, so one track queued twice
   * has two different queue item ids. */
  nextQueueItemId: number
  /** Info: admin-fixed artist matches (name -> mbid) and chosen
   * album editions ("artist::album" -> mbid) are kept below. */
  /** What /api/admin/background-status reports. */
  backgroundStatus: Schemas['BackgroundStatusOut']
  /** The server activity log (newest has the highest id), and the number of
   * next /api/admin/activity requests to answer with a 500. */
  activityEvents: Schemas['ActivityEventOut'][]
  activityFailures: number
  artistMatches: Record<string, string | null>
  albumEditions: Record<string, string | null>
  /** Library info: changes to the stats the mock builds from the tracks, the
   * number of next /stats requests to fail (500), and the storage request's
   * behaviour: a list of entries, 'fail', or 'hold' (never answered until
   * `releaseStorage` is called). */
  libraryStatsPatch?: (stats: Schemas['LibraryStatsOut']) => Schemas['LibraryStatsOut']
  libraryStatsFailures?: number
  /** The number of next /stats/nav-counts requests to fail (500). */
  navCountsFailures?: number
  libraryStorage?: Schemas['StatsStorageOut'][] | 'fail' | 'hold'
  releaseStorage?: () => void
  /** The server's `pending` convention: how many more asks of a resource
   * answer with stored data only (`pending: true`) before the full answer.
   * Keys: `lyrics:<id>`, `track-info:<id>`, `artist-info:<name>`,
   * `album-info:<album>`, `top-tracks:<name>`. */
  pendingAsks: Record<string, number>
  /** Delays (ms) before answering a path, for loading-order tests. */
  routeDelays: Record<string, number>
}

export interface MockCollectionPreference {
  kind: 'album' | 'artist'
  artist: string
  album: string | null
  state: 'none' | 'liked' | 'disliked'
}
