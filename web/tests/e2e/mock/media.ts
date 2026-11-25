// Art, downloads and streams.

import type { Route } from 'playwright/test'

import { type RouteContext, json } from './http'
import type { AppMockState, Schemas } from './types'

export const mockCustomArt = new Map<string, number>()

export function artFields(type: string, key: string | number) {
  const version = mockCustomArt.get(`${type}:${key}`)
  return { custom_art: version != null, art_version: version ?? null }
}

/** Art, downloads and streams. */
export async function handleMediaRoutes({ state, route, request, method, path, body }: RouteContext): Promise<boolean> {
  const creditMatch = path.match(/^\/api\/art\/(media_item|album|artist|playlist)\/([^/]+)\/credit$/)
  if (creditMatch && method === 'GET') {
    await json(route, state.artCredits[`${creditMatch[1]}:${decodeURIComponent(creditMatch[2])}`] ?? null)
    return true
  }
  const artMatch = path.match(/^\/api\/art\/(media_item|album|artist|playlist)\/([^/]+)(\/thumb)?$/)
  if (artMatch) {
    const mapKey = `${artMatch[1]}:${decodeURIComponent(artMatch[2])}`
    if (method === 'PUT') {
      const type = request.headers()['content-type'] ?? ''
      if (!type.startsWith('image/')) await json(route, { detail: 'Not an image' }, 415)
      else if ((request.postDataBuffer()?.length ?? 0) > 30 * 1024 * 1024) await json(route, { detail: 'Too large' }, 413)
      else {
        const version = Date.now()
        mockCustomArt.set(mapKey, version)
        await json(route, { ok: true, custom_art: true, art_version: version })
      }
    } else if (method === 'DELETE') {
      mockCustomArt.delete(mapKey)
      await json(route, { ok: true, custom_art: false, art_version: null })
    } else {
      await route.fulfill({
        status: 200,
        contentType: 'image/svg+xml',
        body: '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" fill="#e0508f"/></svg>',
      })
    }
    return true
  }
  const downloadMatch = path.match(/^\/api\/download\/(media|album|artist|playlist)\/([^/]+?)(\/manifest)?$/)
  if (downloadMatch) {
    if (downloadMatch[3]) {
      await json(route, [
        { id: 1, path: 'Atriolyric/Queen/A Night at the Opera/01 Bohemian Rhapsody.flac', size: 3, url: '/api/download/media/1' },
        { id: 2, path: 'Atriolyric/Queen/A Night at the Opera/02 Love of My Life.flac', size: 3, url: '/api/download/media/2' },
      ])
    } else {
      await route.fulfill({
        status: 200,
        contentType: downloadMatch[1] === 'media' ? 'audio/flac' : 'application/zip',
        headers: { 'Content-Disposition': `attachment; filename="${downloadMatch[1] === 'media' ? 'Queen - Song.flac' : 'Atriolyric-Test.zip'}"` },
        body: 'abc',
      })
    }
    return true
  }

  if (path.startsWith('/api/stream/')) {
    if (path.endsWith('/played')) {
      await answerPlayReport(state, route, body)
    } else if (path.endsWith('/listened')) {
      await answerListenReport(state, route, body)
    } else if (path.endsWith('/playback')) {
      await json(route, { mode: 'direct', url: path.replace(/\/playback$/, '') })
    } else if (path.endsWith('/thumbnail') || path.endsWith('/art') || path.endsWith('/art/full')) {
      await route.fulfill({
        status: 200,
        contentType: 'image/svg+xml',
        body: '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" fill="#5b8def"/></svg>',
      })
    } else {
      await route.fulfill({ status: 200, contentType: 'audio/wav', body: '' })
    }
    return true
  }
  return false
}

/** Play reports and listen outcomes. */
export async function handleStreamPlayedRoute({ state, route, method, path, body }: RouteContext): Promise<boolean> {
  if (path.match(/^\/api\/stream\/\d+\/played$/) && method === 'POST') {
    await answerPlayReport(state, route, body)
    return true
  }
  if (path.match(/^\/api\/stream\/\d+\/listened$/) && method === 'POST') {
    await answerListenReport(state, route, body)
    return true
  }
  return false
}

/** `POST /api/stream/{id}/played`: like the server, a report needs a body
 * with `started_at` (422 otherwise, recorded as a protocol violation). */
async function answerPlayReport(state: AppMockState, route: Route, body: Record<string, unknown>): Promise<void> {
  if (typeof body.started_at !== 'string' || !body.started_at) {
    state.protocolViolations.push(`play report without started_at: ${route.request().url()}`)
    await json(route, { detail: 'started_at is required' }, 422)
    return
  }
  await json(route, { ok: true, recorded: true })
}

const LISTEN_END_REASONS: ReadonlyArray<Schemas['ListenedReport']['end_reason']> = [
  'finished', 'skipped', 'stopped', 'transferred', 'error', 'unknown',
]
const LISTEN_SOURCE_KINDS: ReadonlyArray<NonNullable<Schemas['ListenedReport']['source_kind']>> = [
  'album', 'artist', 'mix', 'playlist', 'library',
]

/** `POST /api/stream/{id}/listened`: like the server, the body needs
 * `started_at`, `listened_seconds` (>= 0) and a known `end_reason`; the
 * optional fields must be what the server accepts (422 otherwise, recorded
 * as a protocol violation). */
async function answerListenReport(state: AppMockState, route: Route, body: Record<string, unknown>): Promise<void> {
  const problems: string[] = []
  if (typeof body.started_at !== 'string' || !body.started_at) problems.push('started_at')
  if (body.ended_at != null && typeof body.ended_at !== 'string') problems.push('ended_at')
  if (typeof body.listened_seconds !== 'number' || body.listened_seconds < 0) problems.push('listened_seconds')
  for (const key of ['duration_seconds', 'position_seconds'] as const) {
    if (body[key] != null && (typeof body[key] !== 'number' || (body[key] as number) < 0)) problems.push(key)
  }
  if (!LISTEN_END_REASONS.includes(body.end_reason as never)) problems.push('end_reason')
  if (body.start_kind != null && body.start_kind !== 'auto' && body.start_kind !== 'user') problems.push('start_kind')
  if (body.source_kind != null && !LISTEN_SOURCE_KINDS.includes(body.source_kind as never)) problems.push('source_kind')
  if (problems.length) {
    state.protocolViolations.push(`listen report with bad ${problems.join(', ')}: ${route.request().url()}`)
    await json(route, { detail: `invalid ${problems.join(', ')}` }, 422)
    return
  }
  await json(route, { ok: true, recorded: true })
}
