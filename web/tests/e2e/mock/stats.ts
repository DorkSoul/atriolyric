// Library info: /api/library/stats and /api/library/stats/storage, built from
// the mock library so the numbers agree with the other lists.

import { json, type RouteContext } from './http'
import type { AppMockState, Schemas } from './types'

const FORMATS = ['FLAC 16/44', 'MP3 320', 'Opus']

export function buildStats(state: AppMockState): Schemas['LibraryStatsOut'] {
  const songs = state.tracks.filter((item) => item.kind === 'track')
  const videos = state.tracks.filter((item) => item.kind !== 'track')
  const albums = new Map<string, MediaItemLike[]>()
  for (const item of songs) {
    const key = `${item.artist ?? ''}\u0000${item.album ?? ''}`
    albums.set(key, [...(albums.get(key) ?? []), item])
  }
  const recent = [...albums.values()].map((items, index) => ({
    artist: items[0].artist,
    album: items[0].album ?? 'Unknown album',
    track_count: items.length,
    representative_media_item_id: items[0].id,
    art_key: null,
    custom_art: false,
    art_version: null,
    release_date: null,
    year: 1975 + index * 10,
    format_label: FORMATS[index % FORMATS.length],
    added_at: `2026-11-${String(9 - index).padStart(2, '0')}T10:00:00`,
  }))
  const genres = new Map<string, number>()
  for (const item of songs) genres.set(item.genre ?? 'Unknown', (genres.get(item.genre ?? 'Unknown') ?? 0) + 1)
  const songCount = songs.length
  return {
    totals: {
      albums: albums.size,
      songs: songCount,
      artists: new Set(songs.map((item) => item.artist)).size,
      videos: videos.length,
      total_bytes: 412 * 1024 ** 3,
      total_seconds: songs.reduce((sum, item) => sum + (item.duration_seconds ?? 0), 0) + 51 * 86400,
      last_scanned_at: '2026-11-10T14:32:00',
    },
    recently_added: recent,
    contents: {
      formats: [
        { format: 'flac', label: 'FLAC', lossless: true, songs: Math.max(1, songCount - 1), bytes: 400 * 1024 ** 3 },
        { format: 'mp3', label: 'MP3', lossless: false, songs: 1, bytes: 12 * 1024 ** 3 },
      ],
      with_lyrics: Math.max(0, songCount - 1),
      loudness_measured: songCount,
      liked_songs: songs.filter((item) => item.is_favorite).length,
      genre_count: genres.size,
    },
    quality: {
      lossless_songs: Math.max(1, songCount - 1),
      lossless_share: songCount > 1 ? (songCount - 1) / songCount : 1,
      hi_res_songs: 1,
      average_mp3_kbps: 287,
      below_192_kbps_songs: 0,
      longest_song_seconds: 1634,
      average_album_seconds: 2760,
      quality_scanned: songCount,
      quality_total: songCount,
    },
    listening: {
      plays_this_week: 212,
      plays_this_year: 9480,
      listening_seconds_this_year: 24 * 86400 + 6 * 3600,
      listening_capped: false,
      most_played_artist: { name: 'Queen', plays: 340 },
      most_played_album: { artist: 'Queen', album: 'A Night at the Opera', plays: 120 },
      never_played_songs: 6318,
    },
    by_genre: {
      top: [...genres].map(([name, count]) => ({ name, songs: count })).sort((a, b) => b.songs - a.songs),
      distinct: genres.size,
    },
    by_decade: { decades: [{ decade: '1970s', songs: 1 }, { decade: '1990s', songs: 2 }], unknown: 1 },
    growth: {
      albums_added_this_month: 14,
      albums_added_this_year: 214,
      largest_album: { artist: 'Queen', album: 'A Night at the Opera', bytes: 2.1 * 1024 ** 3 },
      video_bytes: 96 * 1024 ** 3,
    },
    nav_counts: {
      artists: new Set(songs.map((item) => item.artist)).size,
      albums: albums.size,
      songs: songCount,
      videos: videos.length,
      playlists: state.playlists.length,
      radio_stations: state.stations.length,
      want_list: state.wantList.length,
    },
  }
}

type MediaItemLike = AppMockState['tracks'][number]

export async function handleStatsRoutes({ state, route, path }: RouteContext): Promise<boolean> {
  if (path === '/api/library/stats') {
    if ((state.libraryStatsFailures ?? 0) > 0) {
      state.libraryStatsFailures = (state.libraryStatsFailures ?? 0) - 1
      await json(route, { detail: 'Could not read the library stats.' }, 500)
      return true
    }
    const stats = buildStats(state)
    await json(route, state.libraryStatsPatch ? state.libraryStatsPatch(stats) : stats)
    return true
  }
  if (path === '/api/library/stats/nav-counts') {
    if ((state.navCountsFailures ?? 0) > 0) {
      state.navCountsFailures = (state.navCountsFailures ?? 0) - 1
      await json(route, { detail: 'Could not count the library.' }, 500)
      return true
    }
    // The same numbers as the full stats' nav_counts, without the rest.
    const stats = buildStats(state)
    await json(route, (state.libraryStatsPatch ? state.libraryStatsPatch(stats) : stats).nav_counts)
    return true
  }
  if (path === '/api/library/stats/storage') {
    const mode = state.libraryStorage
    if (mode === 'fail') {
      await json(route, { detail: 'Storage check failed.' }, 500)
    } else if (mode === 'hold') {
      await new Promise<void>((resolve) => {
        state.releaseStorage = resolve
      })
      await json(route, [{ roots: ['Music'], free_bytes: 1.3 * 1024 ** 4, total_bytes: 4 * 1024 ** 4 }])
    } else {
      await json(route, mode ?? [{ roots: ['Music'], free_bytes: 1.3 * 1024 ** 4, total_bytes: 4 * 1024 ** 4 }])
    }
    return true
  }
  return false
}
