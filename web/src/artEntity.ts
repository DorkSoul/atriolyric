import { useSyncExternalStore } from 'react'
import { clearBrowseCache } from './api'
import { onArtEpochChange } from './artEpoch'
import type { Album, Artist, MediaItem, Playlist } from './types'

export type ArtEntityType = 'media_item' | 'album' | 'artist' | 'playlist'

/** Identifies one piece of custom-art-capable library content. `key` is the
 * numeric id for media items/playlists and the server's `art_key` for
 * albums/artists. */
export interface ArtRef {
  type: ArtEntityType
  key: string | number
  custom_art?: boolean
  art_version?: number | null
}

interface Override {
  custom: boolean
  version: number | null
  stamp: number
}

// What this session just changed. It wins over whatever the (cached) list
// data says, so a freshly uploaded or reset image shows up everywhere at
// once without re-fetching every list.
const overrides = new Map<string, Override>()
const listeners = new Set<() => void>()
let revision = 0

function idOf(ref: ArtRef): string {
  return `${ref.type}:${ref.key}`
}

function bump() {
  revision += 1
  listeners.forEach((listener) => listener())
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

// A new art epoch changes every track art address: tiles on screen reload.
onArtEpochChange(bump)

/** Re-renders the caller whenever any art is uploaded or reset, or the
 * server reports that a rescan changed art. */
export function useArtRevision(): number {
  return useSyncExternalStore(subscribe, () => revision)
}

export function effectiveArt(ref: ArtRef): { custom: boolean; version: string | number | null } {
  const o = overrides.get(idOf(ref))
  if (o) return { custom: o.custom, version: o.custom ? o.version : `r${o.stamp}` }
  return { custom: !!ref.custom_art, version: ref.art_version ?? null }
}

export function appendVersion(url: string, version: string | number | null): string {
  if (version == null) return url
  return `${url}${url.includes('?') ? '&' : '?'}v=${version}`
}

/** `url` with the ref's current cache-busting version, if it has one. */
export function withArtVersion(url: string, ref: ArtRef | undefined): string {
  return ref ? appendVersion(url, effectiveArt(ref).version) : url
}

export function customArtUrl(ref: ArtRef, full: boolean): string {
  const base = `/api/art/${ref.type}/${encodeURIComponent(String(ref.key))}${full ? '' : '/thumb'}`
  return appendVersion(base, effectiveArt(ref).version)
}

export function mediaRef(item: MediaItem): ArtRef {
  return { type: 'media_item', key: item.id, custom_art: item.custom_art, art_version: item.art_version }
}

export function albumRef(album: Album | undefined | null): ArtRef | undefined {
  return album?.art_key
    ? { type: 'album', key: album.art_key, custom_art: album.custom_art, art_version: album.art_version }
    : undefined
}

export function artistRef(artist: Artist | undefined | null): ArtRef | undefined {
  return artist?.art_key
    ? { type: 'artist', key: artist.art_key, custom_art: artist.custom_art, art_version: artist.art_version }
    : undefined
}

export function playlistRef(playlist: Pick<Playlist, 'id' | 'custom_art' | 'art_version'>): ArtRef {
  return { type: 'playlist', key: playlist.id, custom_art: playlist.custom_art, art_version: playlist.art_version }
}

function artPath(ref: ArtRef): string {
  return `/api/art/${ref.type}/${encodeURIComponent(String(ref.key))}`
}

async function failure(res: Response, fallback: string): Promise<Error> {
  if (res.status === 413) return new Error('That image is too large (30 MB maximum).')
  if (res.status === 415) return new Error('That file is not a supported image.')
  if (res.status === 404) return new Error('That item no longer exists.')
  let detail = ''
  try {
    const body = await res.json()
    if (typeof body?.detail === 'string') detail = body.detail
  } catch {
    // not JSON
  }
  return new Error(detail || fallback)
}

function applyResult(ref: ArtRef, body: { custom_art?: boolean; art_version?: number | null }) {
  overrides.set(idOf(ref), {
    custom: !!body.custom_art,
    version: body.art_version ?? null,
    stamp: Date.now(),
  })
  clearBrowseCache()
  bump()
}

/** Uploads `file` as the library-wide custom art for `ref`. Throws an Error
 * with a user-facing message on failure. */
export async function uploadArt(ref: ArtRef, file: File): Promise<void> {
  let res: Response
  try {
    res = await fetch(artPath(ref), {
      method: 'PUT',
      body: file,
      headers: { 'Content-Type': file.type },
    })
  } catch {
    throw new Error('Could not upload the artwork.')
  }
  if (!res.ok) throw await failure(res, 'Could not upload the artwork.')
  applyResult(ref, await res.json().catch(() => ({ custom_art: true, art_version: Date.now() })))
}

export async function resetArt(ref: ArtRef): Promise<void> {
  let res: Response
  try {
    res = await fetch(artPath(ref), { method: 'DELETE' })
  } catch {
    throw new Error('Could not reset the artwork.')
  }
  if (!res.ok) throw await failure(res, 'Could not reset the artwork.')
  applyResult(ref, await res.json().catch(() => ({ custom_art: false, art_version: null })))
}

/** One image offered by "Choose artwork…" (app.art_candidates). */
export interface ArtCandidate {
  id: string
  source: 'Cover Art Archive' | 'Embedded' | 'Folder' | 'Wikimedia Commons' | string
  label: string
  url: string
  thumb_url: string
}

export async function listArtCandidates(ref: ArtRef): Promise<ArtCandidate[]> {
  const res = await fetch(`${artPath(ref)}/candidates`)
  if (!res.ok) throw await failure(res, 'Could not load artwork to choose from.')
  return res.json()
}

/** Uses one candidate as `ref`'s custom art (the server downloads it). */
export async function chooseArtCandidate(ref: ArtRef, candidateId: string): Promise<void> {
  let res: Response
  try {
    res = await fetch(`${artPath(ref)}/candidates`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ candidate_id: candidateId }),
    })
  } catch {
    throw new Error('Could not set the artwork.')
  }
  if (!res.ok) throw await failure(res, 'Could not set the artwork.')
  applyResult(ref, await res.json().catch(() => ({ custom_art: true, art_version: Date.now() })))
}
