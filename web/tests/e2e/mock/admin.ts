// Admin: users, library roots, tasks and tag edits.

import { type RouteContext, json } from './http'
import { updatePlaylistCopies } from './playlists'
import type { AdminUser, LibraryTask, Root } from './types'

/** The artist split rules. */
export async function handleArtistSplitRulesRoute({ state, route, request, method, path }: RouteContext): Promise<boolean> {
  if (path === '/api/admin/artist-split-rules') {
    if (method === 'PUT') {
      state.artistSplitRules = JSON.parse(request.postData() ?? '{}')
      await json(route, { ...state.artistSplitRules, relinked: state.tracks.filter((item) => item.kind === 'track').length })
    } else {
      await json(route, state.artistSplitRules)
    }
    return true
  }
  return false
}

/** The server activity log: the list (filtered and paged like the server's)
 * and its text download. */
export async function handleActivityRoutes({ state, route, url, method, path }: RouteContext): Promise<boolean> {
  if (method !== 'GET' || !path.startsWith('/api/admin/activity')) return false
  const kind = url.searchParams.get('kind')
  const level = url.searchParams.get('level')
  const day = url.searchParams.get('day')
  const offset = Number(url.searchParams.get('tz_offset_minutes') ?? '0')
  const matching = state.activityEvents
    .filter((event) => {
      // "task." is the whole group, as on the server.
      if (kind && (kind.endsWith('.') ? !event.kind.startsWith(kind) : event.kind !== kind)) return false
      if (level === 'problems' ? event.level === 'info' : level && event.level !== level) return false
      if (day) {
        const [y, m, d] = day.split('-').map(Number)
        const start = Date.UTC(y, m - 1, d) + offset * 60_000
        const at = Date.parse(`${event.created_at}Z`)
        if (at < start || at >= start + 86_400_000) return false
      }
      return true
    })
    .sort((a, b) => b.id - a.id)
  if (path === '/api/admin/activity/download') {
    await route.fulfill({
      status: 200,
      contentType: 'text/plain; charset=utf-8',
      headers: { 'Content-Disposition': 'attachment; filename="atriolyric-activity-test.txt"' },
      body: matching.map((event) => `${event.created_at}  ${event.kind}  ${event.summary}\n`).join(''),
    })
    return true
  }
  if (path !== '/api/admin/activity') return false
  if (state.activityFailures > 0) {
    state.activityFailures -= 1
    await json(route, { detail: 'The activity log failed.' }, 500)
    return true
  }
  const beforeId = url.searchParams.get('before_id')
  const limit = Number(url.searchParams.get('limit') ?? '100')
  const older = beforeId ? matching.filter((event) => event.id < Number(beforeId)) : matching
  const events = older.slice(0, limit)
  await json(route, {
    events,
    next_before_id: older.length > limit ? events[events.length - 1].id : null,
    kinds: [...new Set(state.activityEvents.map((event) => event.kind))].sort(),
  })
  return true
}

/** Users, library roots and their tasks, tag edits. */
export async function handleAdminRoutes({ state, route, url, method, path, body }: RouteContext): Promise<boolean> {
  if (path === '/api/admin/custom-art/unrecorded' && method === 'GET') {
    await json(route, { count: state.unrecordedArt })
    return true
  }
  if (path === '/api/admin/custom-art/unrecorded/remove' && method === 'POST') {
    const removed = state.unrecordedArt
    state.unrecordedArt = 0
    await json(route, { removed })
    return true
  }
  if (path === '/api/admin/background-status' && method === 'GET') {
    await json(route, state.backgroundStatus)
    return true
  }
  if (path === '/api/admin/users' && method === 'GET') {
    await json(route, state.users)
    return true
  }
  if (path === '/api/admin/users' && method === 'POST') {
    const created: AdminUser = {
      id: state.nextUserId++,
      username: String(body.username ?? 'new-user'),
      is_admin: Boolean(body.is_admin),
      accessible_root_ids: [],
    }
    state.users.push(created)
    await json(route, created)
    return true
  }
  const adminUserMatch = path.match(/^\/api\/admin\/users\/(\d+)$/)
  if (adminUserMatch && method === 'DELETE') {
    state.users = state.users.filter((user) => user.id !== Number(adminUserMatch[1]))
    await json(route, { ok: true })
    return true
  }
  if (adminUserMatch && method === 'PATCH') {
    const target = state.users.find((user) => user.id === Number(adminUserMatch[1]))
    if (!target) {
      await json(route, { detail: 'User not found' }, 404)
      return true
    }
    target.is_admin = Boolean(body.is_admin)
    await json(route, target)
    return true
  }
  if (path.match(/^\/api\/admin\/users\/\d+\/password$/) && method === 'POST') {
    await json(route, { ok: true })
    return true
  }
  if (path === '/api/admin/roots' && method === 'GET') {
    await json(route, state.roots)
    return true
  }
  if (path === '/api/admin/roots' && method === 'POST') {
    const root: Root = {
      id: state.nextRootId++,
      path: String(body.path ?? '/media/new'),
      display_name: String(body.display_name ?? 'New root'),
      is_shared: Boolean(body.is_shared),
      last_scanned_at: null,
      last_scan_error: null,
      active_task: null,
      last_task: null,
    }
    state.roots.push(root)
    await json(route, root)
    return true
  }
  const accessMatch = path.match(/^\/api\/admin\/roots\/(\d+)\/access\/(\d+)$/)
  if (accessMatch) {
    const rootId = Number(accessMatch[1])
    const user = state.users.find((item) => item.id === Number(accessMatch[2]))
    if (user) {
      if (method === 'POST' && !user.accessible_root_ids.includes(rootId)) user.accessible_root_ids.push(rootId)
      if (method === 'DELETE') user.accessible_root_ids = user.accessible_root_ids.filter((id) => id !== rootId)
    }
    await json(route, { ok: true })
    return true
  }
  const adminRootMatch = path.match(/^\/api\/admin\/roots\/(\d+)(?:\/(.*))?$/)
  if (adminRootMatch) {
    const rootId = Number(adminRootMatch[1])
    const suffix = adminRootMatch[2] ?? ''
    const root = state.roots.find((item) => item.id === rootId)
    if (suffix === '' && method === 'DELETE') {
      state.roots = state.roots.filter((root) => root.id !== rootId)
      await json(route, { ok: true })
    } else if (suffix === 'scan' && method === 'POST') {
      // A confirmed scan removes what the previous scan held back.
      const confirmed = url.searchParams.get('mode') === 'confirm_removals'
      const task: LibraryTask = {
        id: 100 + rootId,
        kind: 'scan',
        status: 'completed',
        total: 3,
        processed: 3,
        added: 2,
        updated: 1,
        removed: confirmed ? root?.last_task?.missing_held ?? 0 : 0,
        missing_held: 0,
        note: null,
        matched: 0,
        error: null,
        started_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        completed_at: new Date().toISOString(),
        items_per_second: 3,
        eta_seconds: null,
      }
      if (root) root.last_task = task
      await json(route, { ...task, status: 'running', completed_at: null })
    } else if (suffix === 'enrich' && method === 'POST') {
      const kinds: Record<string, LibraryTask['kind']> = {
        mbids: 'mb_mbid',
        musicbrainz_missing: 'enrich',
        isrc_mbid: 'mb_isrc',
        musicbrainz_fallback: 'enrich',
        listenbrainz_missing: 'lb_tags',
        listenbrainz_all: 'lb_tags_all',
        artist_info: 'artist_info',
      }
      const mode = typeof body.mode === 'string' ? body.mode : ''
      const task: LibraryTask = {
        id: 200 + rootId,
        kind: kinds[mode] ?? 'lb_tags',
        status: 'completed',
        total: 3,
        processed: 3,
        added: 0,
        updated: 0,
        removed: 0,
        matched: 2,
        error: null,
        started_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        completed_at: new Date().toISOString(),
        items_per_second: 3,
        eta_seconds: null,
      }
      if (root) root.last_task = task
      await json(route, { ...task, status: 'running', completed_at: null })
    } else if (suffix === 'full-setup' && method === 'POST') {
      const task: LibraryTask = {
        id: 400 + rootId,
        kind: 'full_setup',
        status: 'completed',
        stage: 'Done',
        total: 3,
        processed: 3,
        added: 0,
        updated: 0,
        removed: 0,
        matched: 3,
        error: null,
        started_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        completed_at: new Date().toISOString(),
        items_per_second: 3,
        eta_seconds: null,
      }
      if (root) root.last_task = task
      await json(route, { ...task, status: 'running', stage: '1/6 Full rescan', completed_at: null })
    } else if (suffix === 'full-setup-new' && method === 'POST') {
      const task: LibraryTask = {
        id: 400 + rootId,
        kind: 'full_setup_new',
        status: 'completed',
        stage: 'Done',
        total: 3,
        processed: 3,
        added: 0,
        updated: 0,
        removed: 0,
        matched: 3,
        error: null,
        started_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        completed_at: new Date().toISOString(),
        items_per_second: 3,
        eta_seconds: null,
      }
      if (root) root.last_task = task
      await json(route, { ...task, status: 'running', stage: '1/6 Scan', completed_at: null })
    } else if (method === 'POST') {
      const stopMatch = suffix.match(/^tasks\/(\d+)\/stop$/)
      const task = root?.active_task
      if (stopMatch && task && task.id === Number(stopMatch[1])) {
        const stopped = { ...task, status: 'stopped' as const, completed_at: new Date().toISOString() }
        if (root) {
          root.active_task = null
          root.last_task = stopped
        }
        await json(route, stopped)
      } else {
        await json(route, { detail: 'Library task not found' }, 404)
      }
    } else {
      await json(route, { ok: true })
    }
    return true
  }
  const tagMatch = path.match(/^\/api\/admin\/media\/(\d+)\/tags$/)
  if (tagMatch && method === 'PATCH') {
    const item = state.tracks.find((track) => track.id === Number(tagMatch[1]))
    if (item) {
      for (const key of ['title', 'artist', 'album', 'genre'] as const) {
        if (typeof body[key] === 'string') item[key] = body[key]
      }
      updatePlaylistCopies(state, item.id, {
        title: item.title,
        artist: item.artist,
        album: item.album,
        genre: item.genre,
      })
    }
    await json(route, { ok: true })
    return true
  }
  if (path === '/api/admin/media/bulk-tags' && method === 'POST') {
    const ids = Array.isArray(body.media_item_ids)
      ? body.media_item_ids.filter((id): id is number => typeof id === 'number')
      : []
    for (const id of ids) {
      const item = state.tracks.find((track) => track.id === id)
      if (item) {
        for (const key of ['artist', 'album', 'genre'] as const) {
          if (typeof body[key] === 'string') item[key] = body[key]
        }
      }
    }
    // The server's BulkTagEditOut.
    await json(route, { updated: ids.length, succeeded: ids, failed: [] })
    return true
  }
  if (path === '/api/admin/media/bulk-remove' && method === 'POST') {
    const ids = Array.isArray(body.media_item_ids)
      ? body.media_item_ids.filter((id): id is number => typeof id === 'number')
      : []
    state.tracks = state.tracks.filter((item) => !ids.includes(item.id))
    await json(route, { removed: ids, failed: [] })
    return true
  }
  return false
}
