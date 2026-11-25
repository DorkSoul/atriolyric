// Playlists.

import { type RouteContext, empty, json } from './http'
import { artFields } from './media'
import { copyTrack } from './state'
import type { AppMockState, MediaItem, Playlist, PlaylistDetail } from './types'

/** A playlist detail as the server sends it: total_count is always the whole
 * playlist's length; `limit`/`offset` (when given) page the items. */
export function wireDetail(detail: PlaylistDetail, url?: URL) {
  const limit = url?.searchParams.get('limit')
  const offset = Number(url?.searchParams.get('offset') ?? 0) || 0
  const items = limit != null ? detail.items.slice(offset, offset + Number(limit)) : detail.items
  return { ...detail, items, total_count: detail.items.length }
}

export function syncPlaylistSummary(state: AppMockState, playlistId: number) {
  const playlist = state.playlists.find((item) => item.id === playlistId)
  const detail = state.playlistDetails[playlistId]
  if (!playlist || !detail) return
  playlist.item_count = detail.items.length
  playlist.preview_media_item_ids = detail.items.slice(0, 4).map((item) => item.id)
}

export function updatePlaylistCopies(state: AppMockState, mediaItemId: number, patch: Partial<MediaItem>) {
  for (const detail of Object.values(state.playlistDetails)) {
    for (const item of detail.items) {
      if (item.id === mediaItemId) Object.assign(item, patch)
    }
  }
}

/** Playlists. */
export async function handlePlaylistRoutes({ state, route, method, path, body, url }: RouteContext): Promise<boolean> {
  if (path === '/api/playlists' && method === 'GET') {
    await json(route, state.playlists.map((pl) => ({ ...pl, ...artFields('playlist', pl.id) })))
    return true
  }
  if (path === '/api/playlists' && method === 'POST') {
    const name = typeof body.name === 'string' ? body.name : 'Untitled'
    const playlist: Playlist = {
      id: state.nextPlaylistId++,
      name,
      created_at: '2026-02-01T00:00:00Z',
      item_count: 0,
      is_auto_generated: false,
      auto_kind: null,
      preview_media_item_ids: [],
    }
    state.playlists.push(playlist)
    state.playlistDetails[playlist.id] = {
      id: playlist.id,
      name,
      created_at: playlist.created_at,
      items: [],
    }
    await json(route, playlist)
    return true
  }
  const playlistMatch = path.match(/^\/api\/playlists\/(\d+)(?:\/(.*))?$/)
  if (playlistMatch) {
    const playlistId = Number(playlistMatch[1])
    const suffix = playlistMatch[2] ?? ''
    const detail = state.playlistDetails[playlistId]
    if (suffix === 'export.m3u') {
      await route.fulfill({ status: 200, contentType: 'audio/x-mpegurl', body: '#EXTM3U\n' })
      return true
    }
    if (suffix === '' && method === 'GET') {
      await json(route, detail ? wireDetail(detail, url) : { detail: 'Playlist not found' }, detail ? 200 : 404)
      return true
    }
    if (suffix === '' && method === 'PATCH' && detail) {
      const summary = state.playlists.find((playlist) => playlist.id === playlistId)
      if (typeof body.name === 'string') {
        detail.name = body.name
        if (summary) summary.name = body.name
      }
      if ('description' in body) {
        const description = typeof body.description === 'string' && body.description.trim() ? body.description.trim() : null
        detail.description = description
        if (summary) summary.description = description
      }
      await json(route, wireDetail(detail))
      return true
    }
    if (suffix === '' && method === 'DELETE') {
      state.playlists = state.playlists.filter((playlist) => playlist.id !== playlistId)
      delete state.playlistDetails[playlistId]
      await empty(route)
      return true
    }
    if (suffix === 'add-more' && method === 'POST' && detail) {
      await json(route, wireDetail(detail))
      return true
    }
    if (suffix === 'items' && method === 'POST' && detail) {
      const ids = Array.isArray(body.media_item_ids)
        ? body.media_item_ids.filter((id): id is number => typeof id === 'number')
        : []
      for (const id of ids) {
        const item = state.tracks.find((track) => track.id === id)
        if (item) detail.items.push(copyTrack(item, state.nextPlaylistItemId++))
      }
      syncPlaylistSummary(state, playlistId)
      await json(route, wireDetail(detail))
      return true
    }
    if (suffix.startsWith('items/') && method === 'DELETE' && detail) {
      const playlistItemId = Number(suffix.slice('items/'.length))
      detail.items = detail.items.filter((item) => item.playlist_item_id !== playlistItemId)
      syncPlaylistSummary(state, playlistId)
      await json(route, { ok: true })
      return true
    }
    if (suffix === 'reorder' && method === 'PUT' && detail) {
      const ids = Array.isArray(body.ordered_item_ids)
        ? body.ordered_item_ids.filter((id): id is number => typeof id === 'number')
        : []
      const byId = new Map(detail.items.map((item) => [item.playlist_item_id, item]))
      if (playlistId === 1) {
        await json(route, { detail: 'The Favourites playlist cannot be reordered' }, 409)
        return true
      }
      if (new Set(ids).size !== ids.length || ids.some((id) => !byId.has(id))) {
        await json(route, { detail: 'ordered_item_ids must name this playlist\'s items' }, 400)
        return true
      }
      // Like the server: the named entries take the slots they already
      // held, in the new order; any entry not named keeps its place.
      const slots = detail.items.map((item, index) => (ids.includes(item.playlist_item_id as number) ? index : -1)).filter((i) => i >= 0)
      const next = [...detail.items]
      slots.forEach((slot, n) => {
        next[slot] = byId.get(ids[n])!
      })
      detail.items = next
      await json(route, wireDetail(detail))
      return true
    }
    if (suffix === 'save-copy' && method === 'POST' && detail) {
      const name = typeof body.name === 'string' && body.name ? body.name : `${detail.name} copy`
      const copy: Playlist = {
        id: state.nextPlaylistId++,
        name,
        created_at: '2026-02-01T00:00:00Z',
        item_count: detail.items.length,
        is_auto_generated: false,
        auto_kind: null,
        preview_media_item_ids: detail.items.slice(0, 4).map((item) => item.id),
      }
      state.playlists.push(copy)
      state.playlistDetails[copy.id] = {
        id: copy.id,
        name,
        created_at: copy.created_at,
        items: detail.items.map((item) => ({ ...item, playlist_item_id: state.nextPlaylistItemId++ })),
      }
      await json(route, copy)
      return true
    }
  }
  return false
}
