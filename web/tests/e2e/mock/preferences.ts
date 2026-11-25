// Album and artist likes and dislikes.

import { type RouteContext, json } from './http'
import { syncPlaylistSummary, updatePlaylistCopies } from './playlists'
import { copyTrack } from './state'
import type { AppMockState, MediaItem, MockCollectionPreference } from './types'

export function samePreference(a: MockCollectionPreference, kind: string, artist: string, album: string | null) {
  return (
    a.kind === kind &&
    a.artist.toLowerCase() === artist.toLowerCase() &&
    (a.album ?? '').toLowerCase() === (album ?? '').toLowerCase()
  )
}

export function collectionState(state: AppMockState, kind: string, artist: string | null, album: string | null) {
  if (!artist) return 'none'
  return state.collectionPreferences.find((p) => samePreference(p, kind, artist, album))?.state ?? 'none'
}

/** The server's per-user `excluded_by`: only set while the song's own heart
 * is neutral, album before artist. */
export function withExclusion(state: AppMockState, track: MediaItem): MediaItem {
  let excluded: MediaItem['excluded_by'] = null
  if (!track.is_favorite && !track.is_disliked) {
    const owner = track.album_artist || track.artist
    const albumState = collectionState(state, 'album', owner, track.album)
    const artistState = collectionState(state, 'artist', track.artist, null)
    if (albumState === 'disliked') excluded = 'album'
    else if (albumState !== 'liked' && artistState === 'disliked') excluded = 'artist'
  }
  return { ...track, excluded_by: excluded }
}

export function setCollectionPreference(
  state: AppMockState,
  kind: 'album' | 'artist',
  artist: string,
  album: string | null,
  next: MockCollectionPreference['state'],
) {
  state.collectionPreferences = state.collectionPreferences.filter(
    (p) => !samePreference(p, kind, artist, album),
  )
  if (next !== 'none') state.collectionPreferences.push({ kind, artist, album, state: next })
}

/** Mirrors app.artist_credits: a track is "by" an artist it credits as a
 * main or album artist (featured guests don't count). */

/** Song, album and artist likes and dislikes. */
export async function handlePreferenceRoutes({ state, route, url, method, path, body }: RouteContext): Promise<boolean> {
  const mediaAction = path.match(/^\/api\/media\/(\d+)\/preference$/)
  if (mediaAction && method === 'PUT') {
    const item = state.tracks.find((track) => track.id === Number(mediaAction[1]))
    if (!item) {
      await json(route, { detail: 'Media item not found' }, 404)
      return true
    }
    const next = String(body.state)
    item.is_favorite = next === 'liked'
    item.is_disliked = next === 'disliked'
    updatePlaylistCopies(state, item.id, { is_favorite: item.is_favorite, is_disliked: item.is_disliked })
    const favorites = state.playlists.find((playlist) => playlist.id === 1)
    const favoriteDetail = state.playlistDetails[1]
    if (favorites && favoriteDetail) {
      favoriteDetail.items = state.tracks
        .filter((track) => track.is_favorite)
        .map((track, index) => copyTrack(track, 100 + index))
      syncPlaylistSummary(state, 1)
    }
    await json(route, { state: next })
    return true
  }
  if (path === '/api/preferences/album') {
    const artist = url.searchParams.get('artist')
    const album = url.searchParams.get('album') ?? ''
    const albumTracks = state.tracks.filter(
      (track) => track.kind === 'track' && track.album === album && (artist == null ? !track.artist : track.artist === artist || track.album_artist === artist),
    )
    const owner = albumTracks.map((track) => track.album_artist || track.artist).find(Boolean) ?? (method === 'PUT' && body.state === 'none' ? artist : null)
    if (!owner) {
      await json(route, { detail: 'Album not found' }, 404)
      return true
    }
    if (method === 'PUT') setCollectionPreference(state, 'album', owner, album, body.state as MockCollectionPreference['state'])
    await json(route, { kind: 'album', artist: owner, album, state: collectionState(state, 'album', owner, album) })
    return true
  }
  if (path === '/api/preferences/artist') {
    const artist = url.searchParams.get('artist') ?? ''
    const known = state.tracks.some((track) => track.artist === artist || track.album_artist === artist)
    if (!known && !(method === 'PUT' && body.state === 'none')) {
      await json(route, { detail: 'Artist not found' }, 404)
      return true
    }
    if (method === 'PUT') setCollectionPreference(state, 'artist', artist, null, body.state as MockCollectionPreference['state'])
    await json(route, { kind: 'artist', artist, album: null, state: collectionState(state, 'artist', artist, null) })
    return true
  }
  return false
}
