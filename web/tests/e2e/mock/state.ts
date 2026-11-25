// The initial mock state: the tracks, playlists, users and roots every test starts with.

import type { AppMockState, MediaItem, MockMix, Playlist, PlaylistDetail, Schemas } from './types'

export function makeTrack(id: number, values: Partial<MediaItem> = {}): MediaItem {
  return {
    id,
    library_root_id: 1,
    kind: 'track',
    title: `Track ${id}`,
    artist: 'Artist',
    album: 'Album',
    genre: 'Rock',
    track_number: id,
    performer: null,
    description: null,
    duration_seconds: 180,
    is_favorite: false,
    is_disliked: false,
    excluded_by: null,
    format_label: null,
    ...values,
  }
}

export function copyTrack(track: MediaItem, playlistItemId: number): MediaItem {
  return { ...track, playlist_item_id: playlistItemId }
}

export function defaultMixes(count: number): MockMix[] {
  return Array.from({ length: count }, (_, index) => ({
    playlist_id: 4 + index,
    genre: index === 0 ? 'Rock' : `Genre ${index + 1}`,
    name: index === 0 ? 'Rock Mix' : `Genre ${index + 1} Mix`,
    track_count: 2,
    preview_media_item_ids: [1, 2],
    is_specified: false,
  }))
}

export function createMockState(authenticated = true, defaultMixCount = 1): AppMockState {
  const tracks = [
    makeTrack(1, {
      title: 'Bohemian Rhapsody',
      artist: 'Queen',
      album: 'A Night at the Opera',
      genre: 'Rock',
      track_number: 1,
      duration_seconds: 355,
      replaygain_track_gain: -6,
      replaygain_album_gain: -8,
    }),
    makeTrack(2, {
      title: "Don't Stop Me Now",
      artist: 'Queen',
      album: 'News of the World',
      genre: 'Rock',
      track_number: 2,
      duration_seconds: 210,
    }),
    makeTrack(3, {
      title: 'Midnight City',
      artist: 'M83',
      album: 'Hurry Up, We Are Dreaming',
      genre: 'Electronic',
      track_number: 1,
      duration_seconds: 242,
      is_favorite: true,
    }),
    makeTrack(4, {
      title: 'Live at Wembley',
      artist: 'Queen',
      album: 'Live',
      genre: 'Rock',
      kind: 'music_video',
      performer: 'Queen',
      duration_seconds: 300,
    }),
    makeTrack(5, {
      title: 'Episode One',
      artist: null,
      album: null,
      genre: null,
      kind: 'video',
      performer: 'The Atriolyric Show',
      library_root_id: 2,
      duration_seconds: 2700,
    }),
  ]

  const playlists: Playlist[] = [
    {
      id: 1,
      name: 'Favourites',
      created_at: '2026-01-01T00:00:00Z',
      item_count: 1,
      is_auto_generated: true,
      auto_kind: 'favorites',
      preview_media_item_ids: [3],
    },
    {
      id: 2,
      name: 'Workout',
      created_at: '2026-01-02T00:00:00Z',
      item_count: 2,
      is_auto_generated: false,
      auto_kind: null,
      preview_media_item_ids: [1, 2],
    },
    {
      id: 3,
      name: 'Discovery',
      created_at: '2026-01-03T00:00:00Z',
      item_count: 2,
      is_auto_generated: true,
      auto_kind: 'discovery',
      preview_media_item_ids: [2, 3],
    },
    {
      id: 4,
      name: 'Rock Mix',
      created_at: '2026-01-04T00:00:00Z',
      item_count: 2,
      is_auto_generated: true,
      auto_kind: 'mix',
      preview_media_item_ids: [1, 2],
    },
  ]

  const playlistDetails: Record<number, PlaylistDetail> = {
    1: {
      id: 1,
      name: 'Favourites',
      created_at: '2026-01-01T00:00:00Z',
      items: [copyTrack(tracks[2], 101)],
    },
    2: {
      id: 2,
      name: 'Workout',
      created_at: '2026-01-02T00:00:00Z',
      items: [copyTrack(tracks[0], 201), copyTrack(tracks[1], 202)],
    },
    3: {
      id: 3,
      name: 'Discovery',
      created_at: '2026-01-03T00:00:00Z',
      items: [copyTrack(tracks[1], 301), copyTrack(tracks[2], 302)],
    },
    4: {
      id: 4,
      name: 'Rock Mix',
      created_at: '2026-01-04T00:00:00Z',
      items: [copyTrack(tracks[0], 401), copyTrack(tracks[1], 402)],
    },
  }

  const user = {
    id: 1,
    username: 'dork_soul',
    is_admin: true,
    volume_leveling: false,
    eq_preset: 'flat',
    eq_gains: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
  }
  const now = '2026-01-01T12:00:00Z'

  return {
    authenticated,
    appVersion: null,
    needsSetup: false,
    user,
    password: 'correct horse battery staple',
    tracks,
    artistSplitRules: { separators: [';', '/'], exceptions: ['AC/DC', 'Tyler, The Creator'] },
    playlists,
    playlistDetails,
    stations: [
      {
        id: 10,
        name: 'Radio Paradise',
        stream_url: 'https://radio.example/paradise',
        description: 'Eclectic internet radio',
        provider: 'Radio Browser',
        is_shared: true,
        owner_user_id: null,
        created_at: '2026-01-01T00:00:00Z',
        is_favorite: true,
      },
      {
        id: 11,
        name: 'Local Jazz',
        stream_url: 'https://radio.example/jazz',
        description: 'Late-night jazz',
        provider: null,
        is_shared: false,
        owner_user_id: 1,
        created_at: '2026-01-02T00:00:00Z',
        is_favorite: false,
      },
    ],
    wantList: [
      {
        id: 50,
        artist: 'Miles Davis',
        title: 'So What',
        station_name: 'Local Jazz',
        created_at: now,
        media_item_id: null,
      },
      {
        id: 51,
        artist: 'Queen',
        title: 'Bohemian Rhapsody',
        station_name: 'Radio Paradise',
        created_at: now,
        media_item_id: 1,
      },
    ],
    users: [
      { id: 1, username: 'dork_soul', is_admin: true, accessible_root_ids: [1, 2] },
      { id: 2, username: 'listener', is_admin: false, accessible_root_ids: [1] },
    ],
    roots: [
      {
        id: 1,
        path: '/media/music',
        display_name: 'Music',
        is_shared: true,
        last_scanned_at: now,
        last_scan_error: null,
        active_task: null,
        last_task: null,
      },
      {
        id: 2,
        path: '/media/video',
        display_name: 'Video vault',
        is_shared: false,
        last_scanned_at: null,
        last_scan_error: null,
        active_task: null,
        last_task: null,
      },
    ],
    ignoredFolderPaths: [],
    collectionPreferences: [],
    discoveryState: {
      discovery_seed_artists: ['Queen'],
      discovery_similar_artists: ['Muse', 'The Killers'],
      discovery_tracks_added: 2,
      explore_pending: false,
      discovery_updated_at: now,
      explore_seed_artists: ['Queen'],
      explore_results: [
        {
          artist: 'Muse',
          title: 'Uprising',
          artwork_url: null,
          preview_url: 'https://cdn.example/muse.mp3',
          view_url: 'https://deezer.example/muse',
        },
      ],
      explore_updated_at: now,
    },
    discoverySettings: { auto_refresh_enabled: true, auto_refresh_hour: 18 },
    mixesRefreshed: false,
    mixes: defaultMixes(defaultMixCount),
    nextMixId: 40,
    integrations: [
      {
        provider: 'listenbrainz',
        linked: false,
        external_username: null,
        available: true,
      },
    ],
    playback: {
      media_item_id: null,
      position_seconds: 0,
      is_playing: false,
      active_device_id: 1,
      queue_index: 0,
      queue_media_item_ids: [],
      queue_item_ids: [],
      is_shuffled: false,
      repeat_mode: 'off',
      queue_context_count: 0,
      revision: 1,
      devices: [
        {
          id: 1,
          name: 'Browser (Desktop)',
          device_type: 'web',
          is_active: true,
          is_connected: true,
          last_seen_at: now,
        },
        {
          id: 2,
          name: 'Living room',
          device_type: 'web',
          is_active: false,
          is_connected: true,
          last_seen_at: now,
        },
      ],
    },
    calls: [],
    unmocked: [],
    protocolViolations: [],
    wsMessages: [],
    holdPlaybackTypes: new Set(),
    silentDeviceIds: new Set(),
    heldPlaybackMessages: [],
    playbackSocketMode: 'accept',
    playbackSocketConnections: 0,
    nextPlaylistId: 20,
    nextStationId: 20,
    nextUserId: 3,
    nextRootId: 3,
    nextPlaylistItemId: 1000,
    nextQueueItemId: 1001,
    // Both passes through and idle: Admin shows "Up to date".
    backgroundStatus: {
      loudness: { state: 'idle', total: 1200, done: 1200, failed: 0, pending: 0, run_checked: null, run_stored: null, rate_per_minute: null },
      audio_analysis: { state: 'idle', total: 1200, done: 1200, failed: 0, pending: 0, run_checked: null, run_stored: null, rate_per_minute: null },
    },
    activityEvents: defaultActivityEvents(),
    activityFailures: 0,
    artistMatches: {},
    albumEditions: {},
    pendingAsks: {},
    routeDelays: {},
    artCredits: {},
    unrecordedArt: 0,
  }
}

/** 130 events from "today at 12:00 local" back in ten-second steps (so they
 * all fall on one local day whatever the time of the run), of six kinds, one
 * in ten a warning and one in thirteen an error. `created_at` is naive UTC,
 * like the server's. */
export function defaultActivityEvents(): Schemas['ActivityEventOut'][] {
  const noon = new Date()
  noon.setHours(12, 0, 0, 0)
  const kinds = ['auth.login', 'task.start', 'task.finish', 'root.access_grant', 'user.create', 'media.bulk_tags']
  return Array.from({ length: 130 }, (_, index) => {
    const id = index + 1
    const age = 130 - id
    const level = id % 13 === 0 ? 'error' : id % 10 === 0 ? 'warning' : 'info'
    const kind = kinds[id % kinds.length]
    return {
      id,
      created_at: new Date(noon.getTime() - age * 10_000).toISOString().replace(/\.\d+Z$/, ''),
      kind,
      level,
      actor: 'admin',
      summary: level === 'error' ? `Event ${id} failed (${kind})` : `Event ${id} (${kind})`,
      root_id: null,
      context: null,
      request_id: null,
    }
  })
}

export function orderedMixes(state: AppMockState): MockMix[] {
  // Mirrors list_genre_mix_playlists: the featured defaults first, then the
  // specified mixes in creation order.
  return [
    ...state.mixes.filter((mix) => !mix.is_specified),
    ...state.mixes.filter((mix) => mix.is_specified),
  ]
}
