// Radio stations and radio bookmarks.

import { type RouteContext, json } from './http'
import type { Station } from './types'

/** A radio station's audio stream. */
export async function handleRadioStreamRoute({ route, path }: RouteContext): Promise<boolean> {
  if (path.match(/^\/api\/radio\/stations\/\d+\/stream$/)) {
    await route.fulfill({ status: 200, contentType: 'audio/mpeg', body: '' })
    return true
  }
  return false
}

/** Radio stations and radio bookmarks. */
export async function handleRadioRoutes({ state, route, method, path, body }: RouteContext): Promise<boolean> {
  if (path === '/api/radio/stations' && method === 'GET') {
    await json(route, state.stations)
    return true
  }
  if (path === '/api/radio/stations' && method === 'POST') {
    const station: Station = {
      id: state.nextStationId++,
      name: String(body.name ?? 'New station'),
      stream_url: String(body.stream_url ?? 'https://radio.example/new'),
      description: typeof body.description === 'string' ? body.description : null,
      provider: null,
      is_shared: Boolean(body.is_shared),
      owner_user_id: state.user.id,
      created_at: '2026-02-01T00:00:00Z',
      is_favorite: false,
    }
    state.stations.push(station)
    await json(route, station)
    return true
  }
  const stationMatch = path.match(/^\/api\/radio\/stations\/(\d+)(?:\/(.*))?$/)
  if (stationMatch) {
    const stationId = Number(stationMatch[1])
    const suffix = stationMatch[2] ?? ''
    const station = state.stations.find((item) => item.id === stationId)
    if (suffix === 'now-playing') {
      await json(route, { title: 'Miles Davis - So What', updated_at: 123 })
    } else if (suffix === 'favorite' && station) {
      station.is_favorite = method === 'PUT'
      await json(route, { is_favorite: station.is_favorite })
    } else if (suffix === '' && method === 'DELETE') {
      state.stations = state.stations.filter((item) => item.id !== stationId)
      await json(route, { ok: true })
    } else await json(route, station ?? { detail: 'Station not found' }, station ? 200 : 404)
    return true
  }
  if (path === '/api/radio/want-list' && method === 'POST') {
    const item = {
      id: 100 + state.wantList.length,
      artist: String(body.artist ?? 'Unknown artist'),
      title: String(body.title ?? 'Unknown title'),
      station_name: typeof body.station_name === 'string' ? body.station_name : null,
      created_at: '2026-02-01T00:00:00Z',
      media_item_id: null,
    }
    state.wantList.push(item)
    await json(route, item)
    return true
  }
  return false
}
