// Discovery and genre mixes.

import { type RouteContext, json } from './http'
import { orderedMixes } from './state'

/** Discovery and genre mixes. */
export async function handleDiscoveryRoutes({ state, route, method, path, body }: RouteContext): Promise<boolean> {
  if (path === '/api/library/discovery/state') {
    await json(route, state.discoveryState)
    return true
  }
  if (path === '/api/library/discovery/refresh' && method === 'POST') {
    state.discoveryState.discovery_updated_at = '2026-02-01T00:00:00Z'
    await json(route, {
      seed_artists: ['Queen'],
      similar_artists: ['Muse'],
      tracks_added: 1,
    })
    return true
  }
  if (path === '/api/library/discovery/explore' && method === 'POST') {
    state.discoveryState.explore_updated_at = '2026-02-01T00:00:00Z'
    await json(route, {
      seed_artists: state.discoveryState.explore_seed_artists,
      results: state.discoveryState.explore_results,
    })
    return true
  }
  if (path === '/api/library/discovery/settings') {
    if (method === 'PUT') {
      state.discoverySettings = {
        auto_refresh_enabled: Boolean(body.auto_refresh_enabled),
        auto_refresh_hour: Number(body.auto_refresh_hour),
      }
    }
    await json(route, state.discoverySettings)
    return true
  }
  if (path === '/api/library/discovery/generate' && method === 'POST') {
    await json(route, { playlist_id: 21, name: 'Song Mix: Bohemian Rhapsody', track_count: 2 })
    return true
  }
  if (path === '/api/library/mixes') {
    await json(route, orderedMixes(state))
    return true
  }
  if (path === '/api/library/mixes/refresh' && method === 'POST') {
    state.mixesRefreshed = true
    // A refresh regenerates the featured mixes; the temporary specified
    // ones are dropped, matching refresh_all_genre_mixes on the server.
    state.mixes = state.mixes.filter((mix) => !mix.is_specified)
    await json(route, orderedMixes(state))
    return true
  }
  if (path === '/api/library/mixes/specified' && method === 'POST') {
    const included = Array.isArray(body.include) ? (body.include as { name: string }[]) : []
    const name = included.length > 0 ? included.map((item) => item.name).join(' + ') : typeof body.name === 'string' && body.name ? body.name : 'Untitled'
    const mix = {
      playlist_id: state.nextMixId++,
      genre: name,
      name: `${name} Mix`,
      track_count: 2,
      preview_media_item_ids: [1, 2],
      is_specified: true,
    }
    state.mixes.push(mix)
    // A specified mix is a real playlist row on the server (auto_kind
    // "mix"), so it shows up in GET /api/playlists too - that is what the
    // Playlists tab's Generated grid lists.
    state.playlists.push({
      id: mix.playlist_id,
      name: mix.name,
      created_at: '2026-02-01T00:00:00Z',
      item_count: 2,
      is_auto_generated: true,
      auto_kind: 'mix',
      preview_media_item_ids: [1, 2],
    })
    // Only the response for the mix just built says how many of its tracks
    // carry every include: here one of the two when several were chosen.
    const fullMatches = included.length > 1 ? 1 : mix.track_count
    await json(route, orderedMixes(state).map((row) => (
      row.playlist_id === mix.playlist_id ? { ...row, full_match_count: fullMatches } : row
    )))
    return true
  }
  return false
}
