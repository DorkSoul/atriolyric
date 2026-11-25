// API shapes come from api-types.ts, generated from the server's OpenAPI
// document (`npm run gen:api-types`). A type is written by hand
// here only where the client knows more than the server model says (a union
// the server keeps as plain text) or the shape is the client's own.
import type { components, operations } from './api-types'

type Schemas = components['schemas']

export type MediaKind = Schemas['MediaKind']

/** One credited artist on a track (or an album's owner split). */
export type ArtistCredit = Schemas['ArtistCreditOut']

export type MediaItem = Schemas['MediaItemOut']

/** The heart's three states: none -> liked -> disliked -> none. */
export type PreferenceState = Schemas['PreferenceOut']['state']

export type CollectionPreference = Schemas['CollectionPreferenceOut']


export type Playlist = Schemas['PlaylistOut']

export type PlaylistDetail = Schemas['PlaylistDetailOut']

export type Label = Schemas['LabelOut']

/** Genres and tags on a song, or pooled across an album's/artist's tracks. */
export type Labels = Schemas['LabelsOut']

export type MixOption = Schemas['MixOptionOut']

export type CurrentUser = Schemas['UserOut']

export type AdminUser = Schemas['AdminUserOut']

export type LibraryRoot = Omit<Schemas['LibraryRootOut'], 'active_task' | 'last_task'> & {
  active_task: LibraryTask | null
  last_task: LibraryTask | null
}

/** confirm_removals is a normal scan that also removes the missing tracks an
 * earlier scan held back (LibraryTask.missing_held). */
export type ScanMode = NonNullable<
  NonNullable<operations['scan_root_api_admin_roots__root_id__scan_post']['parameters']['query']>['mode']
>

/** The server sends `kind` and `status` as plain text (old rows can hold kinds
 * of removed tasks, so it does not narrow them); these are the values it
 * writes. */
export type LibraryTask = Omit<Schemas['LibraryTaskOut'], 'kind' | 'status'> & {
  kind: 'scan' | 'mb_isrc' | 'mb_mbid' | 'lb_tags' | 'lb_tags_all' | 'enrich' | 'cover_art' | 'artist_info' | 'full_setup' | 'full_setup_new'
  status: 'running' | 'stopping' | 'stopped' | 'completed' | 'failed'
}

export type PlaybackDevice = Schemas['PlaybackDeviceSnapshot']
/** The loudness pass and audio analysis: how far each has got. */
export type BackgroundStatus = Schemas['BackgroundStatusOut']
export type BackgroundPass = Schemas['BackgroundPassOut']

/** The server activity log (Admin): one event, and a page of them. */
export type ActivityEvent = Schemas['ActivityEventOut']
export type ActivityLog = Schemas['ActivityLogOut']

export interface TagEdit {
  title?: string
  artist?: string
  album?: string
  genre?: string
  track_number?: number
}

export type LibraryRootSummary = Schemas['LibraryRootSummary']

export type BrowseDir = Schemas['BrowseDirOut']

export type BrowseResult = Schemas['BrowseOut']

export type FileSearchResult = Schemas['FileSearchOut']

export type BrowseSort = Schemas['BrowseSort']

/** The Songs tab's sorts (GET /api/library/tracks `sort`). */
export type TrackSort = Schemas['TrackSort']

export type Album = Schemas['AlbumOut']

/** The Library info screen's numbers (and the sidebar counts). */
export type LibraryStats = Schemas['LibraryStatsOut']

/** Free and total disk space of one group of library folders. */
export type LibraryStorage = Schemas['StatsStorageOut']

export type VideoFolder = Schemas['VideoFolderOut']

export type Artist = Schemas['ArtistOut']

/** Home search (GET /api/library/search): up to 13 matches per section. */
export type LibrarySearchResult = Schemas['LibrarySearchOut']

export type MixPlaylist = Schemas['MixPlaylistOut']

export type ExploreTrack = Schemas['ExploreTrackOut']

export type DiscoveryExplore = Schemas['DiscoveryExploreOut']

export type DiscoveryState = Schemas['DiscoveryStateOut']

export type ImageCredit = Schemas['ImageCreditOut']

export type UnrecordedArt = Schemas['UnrecordedArtOut']

export type DiscoverySettings = Schemas['DiscoverySettingsOut']

export type GeneratePlaylistResult = Schemas['GeneratePlaylistOut']

export type PlaySourceKind = 'album' | 'artist' | 'mix' | 'playlist' | 'radio' | 'library'

/** What was actually played - an album, artist, playlist, genre mix, or
 * radio station - as opposed to a single track, so "Recently played" can
 * resurface the whole thing to resume. `payload` is resume data specific
 * to `kind`: album {artist, album}, artist {artist}, mix {genre},
 * playlist {playlist_id}, radio {station_id}, library {filters}. */
export interface PlaySource {
  kind: PlaySourceKind
  label: string
  payload: Record<string, unknown>
}

/** One "Recently played" entry. The server keeps `kind` as plain text. */
export type PlaySourceEntry = Omit<Schemas['PlaySourceOut'], 'kind' | 'payload'> & PlaySource

export type RadioStation = Schemas['StationOut']

export type WantListItem = Schemas['WantListOut']

export type Integration = Schemas['IntegrationOut']

export type PlaybackStateSnapshot = Schemas['PlaybackStateSnapshot']

export type ArtistSplitRules = Schemas['ArtistSplitRulesOut']

/** Merged MusicBrainz/ListenBrainz/Wikipedia facts about one artist
 * (GET /api/library/artist-info). Every field may be empty when no source
 * knew anything. */
export type ArtistRelation = Schemas['ArtistMemberOut']

export type ArtistInfo = Schemas['ArtistInfoOut']

export type ArtistTopTrack = Schemas['TopTrackOut']

export type ArtistTopTracks = Schemas['ArtistTopTracksOut']

export type ArtistMatchCandidate = Schemas['ArtistCandidateOut']

export type AlbumEdition = Schemas['AlbumEditionOut']

export type AlbumInfo = Schemas['AlbumInfoOut']

export type TrackInfo = Schemas['TrackInfoOut']
