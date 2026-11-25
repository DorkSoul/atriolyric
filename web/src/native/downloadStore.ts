// The songs and videos kept on the device by the Android app: the list as the
// native side reports it, kept current by its `downloads` event. No server
// calls here - the bundled offline page uses this too.

import { useSyncExternalStore } from 'react'
import type { MediaItem } from '../types'
import { isNativeApp, nativeCall, onNative, type NativeDownload } from './bridge'
import { rememberedItems } from './offlineCache'

/** A video whose rendition the server is still preparing: not a download yet. */
export interface PreparingDownload {
  mediaId: number
  title: string
  qualityLabel: string
  /** 0..1, or null while queued. */
  progress: number | null
}

interface Snapshot {
  items: NativeDownload[]
  preparing: PreparingDownload[]
  loaded: boolean
}

/** Where one item stands: on the device, on its way, or neither. */
export type DownloadStatus = 'none' | 'downloading' | 'downloaded' | 'failed'

let snapshot: Snapshot = { items: [], preparing: [], loaded: false }
let byId = new Map<number, NativeDownload>()
const listeners = new Set<() => void>()
let started = false
let firstList: Promise<void> | null = null

function publish(next: Partial<Snapshot>) {
  snapshot = { ...snapshot, ...next }
  if (next.items) byId = new Map(next.items.map((item) => [item.mediaId, item]))
  for (const listener of [...listeners]) listener()
}

function statusOf(mediaId: number): DownloadStatus {
  const item = byId.get(mediaId)
  if (!item) return snapshot.preparing.some((p) => p.mediaId === mediaId) ? 'downloading' : 'none'
  if (item.state === 'completed') return 'downloaded'
  if (item.state === 'failed') return 'failed'
  return item.state === 'removing' ? 'none' : 'downloading'
}

/** Where `mediaId` stands right now (not a hook). */
export function downloadStatus(mediaId: number): DownloadStatus {
  start()
  return statusOf(mediaId)
}

/** The download of `mediaId` as the device reports it, if there is one. */
export function downloadOf(mediaId: number): NativeDownload | undefined {
  start()
  return byId.get(mediaId)
}

/** Whether `mediaId` can play with no connection. */
export function isDownloaded(mediaId: number): boolean {
  return downloadStatus(mediaId) === 'downloaded'
}

/** Where `mediaId` stands; 'none' in a browser. Re-renders only when that changes. */
export function useDownloadStatus(mediaId: number): DownloadStatus {
  return useSyncExternalStore(subscribe, () => statusOf(mediaId))
}

let completedIds = new Set<number>()
let completedFor: NativeDownload[] | null = null

function completedSnapshot(): Set<number> {
  if (completedFor !== snapshot.items) {
    completedFor = snapshot.items
    const next = new Set(snapshot.items.filter((item) => item.state === 'completed').map((item) => item.mediaId))
    // The same set while nothing finished or went: a progress tick re-renders no list.
    if (next.size !== completedIds.size || [...next].some((id) => !completedIds.has(id))) completedIds = next
  }
  return completedIds
}

/** The ids of everything on this device (empty in a browser). */
export function useDownloadedIds(): Set<number> {
  return useSyncExternalStore(subscribe, completedSnapshot)
}

/** How far a download has come, 0..100, or null when not known. */
export function useDownloadPercent(mediaId: number): number | null {
  return useSyncExternalStore(subscribe, () => {
    const item = byId.get(mediaId)
    return item && item.state === 'downloading' && item.percent != null ? Math.round(item.percent) : null
  })
}

/** The list as the device reports it, once it has been read. */
export function downloadsReady(): Promise<void> {
  start()
  return firstList ?? Promise.resolve()
}

/** A library item made from what a download carries, for a download the app
 * was never told more about (one made by an older version of the app). */
function itemFromDownload(download: NativeDownload): MediaItem {
  return {
    id: download.mediaId,
    kind: download.kind,
    title: download.title || 'Untitled',
    artist: download.artist || null,
    album: download.album || null,
    album_artist: null,
    artists: [],
    art_version: null,
    custom_art: false,
    description: null,
    duration_seconds: download.duration || null,
    excluded_by: null,
    format_label: null,
    genre: null,
    is_disliked: false,
    is_favorite: false,
    library_root_id: 0,
    performer: null,
    play_count: null,
    playlist_item_id: null,
    replaygain_album_gain: null,
    replaygain_track_gain: null,
    replaygain_track_peak: null,
    track_number: null,
    year: null,
  } as MediaItem
}

/** The items of `ids` the app can describe with no connection: those it was
 * told about before, and its downloads. In the order asked for. */
export async function offlineMediaItems(ids: number[]): Promise<MediaItem[]> {
  await downloadsReady()
  const known = new Map((await rememberedItems(ids)).map((item) => [item.id, item]))
  const items: MediaItem[] = []
  for (const id of ids) {
    const download = byId.get(id)
    const item = known.get(id) ?? (download ? itemFromDownload(download) : null)
    if (item) items.push(item)
  }
  return items
}

/** One active download's progress (the app's `downloadProgress` event). */
interface DownloadProgress {
  mediaId: number
  state: NativeDownload['state']
  bytes: number
  totalBytes: number | null
  percent: number | null
}

/** Folds a progress tick into the list. The app sends the whole list only
 * when something is added, removed or changes state, and these small ticks
 * once a second while anything downloads: with thousands of downloads the
 * whole list every second would be a big read on the phone and a big message for
 * the page. Only the rows that moved get new objects, so the other rows (and
 * the set of completed ids) stay the same and re-render nothing. */
function applyProgress(progress: DownloadProgress[]) {
  let changed = false
  const moved = new Map(progress.map((entry) => [entry.mediaId, entry]))
  const items = snapshot.items.map((item) => {
    const entry = moved.get(item.mediaId)
    if (!entry) return item
    if (entry.state === item.state && entry.bytes === item.bytes && entry.percent === item.percent && entry.totalBytes === item.totalBytes) {
      return item
    }
    changed = true
    return { ...item, state: entry.state, bytes: entry.bytes, totalBytes: entry.totalBytes, percent: entry.percent }
  })
  if (changed) publish({ items })
}

function start() {
  if (started || !isNativeApp()) return
  started = true
  onNative<{ items: NativeDownload[] }>('downloads', ({ items }) => publish({ items, loaded: true }))
  onNative<{ items: DownloadProgress[] }>('downloadProgress', ({ items }) => applyProgress(items ?? []))
  firstList = refreshDownloads()
}

export async function refreshDownloads(): Promise<void> {
  try {
    const { items } = await nativeCall<{ items: NativeDownload[] }>('downloadList')
    publish({ items, loaded: true })
  } catch {
    publish({ loaded: true })
  }
}

/** Hears every change to the downloads. */
export function subscribeDownloads(listener: () => void): () => void {
  return subscribe(listener)
}

function subscribe(listener: () => void): () => void {
  start()
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function useDownloads(): Snapshot {
  return useSyncExternalStore(subscribe, () => snapshot)
}

export function downloadsSnapshot(): Snapshot {
  start()
  return snapshot
}

export function setPreparing(entry: PreparingDownload | { mediaId: number; done: true }): void {
  const others = snapshot.preparing.filter((p) => p.mediaId !== entry.mediaId)
  publish({ preparing: 'done' in entry ? others : [...others, entry] })
}

export function removeDownload(mediaId: number): Promise<void> {
  return nativeCall('downloadRemove', { mediaId })
}

/** Where the page loads a download's saved artwork from. The app answers
 * this address itself from the file on the device (so it works with no
 * server), as a plain image the WebView can load lazily and cache - not as a
 * data: URL carried over the bridge, which would hold every picture in the
 * page's memory and queue behind the player's own calls. `?v=` changes when the
 * art file does. Null when the download has no artwork (or the app is too
 * old to say which version it has). */
export function downloadArtUrl(download: Pick<NativeDownload, 'mediaId' | 'hasArt' | 'artVersion'>): string | null {
  if (!download.hasArt || download.artVersion == null) return null
  return `/__atrio/download-art/${download.mediaId}?v=${download.artVersion}`
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 MB'
  const mb = bytes / (1024 * 1024)
  if (mb >= 1024) return `${(mb / 1024).toFixed(mb >= 10240 ? 0 : 1)} GB`
  return `${mb >= 10 ? Math.round(mb) : mb.toFixed(1)} MB`
}

/** The device's total for the downloads that are complete. */
export function totalDownloadedBytes(items: NativeDownload[]): number {
  return items.reduce((sum, item) => sum + (item.state === 'completed' ? item.bytes : 0), 0)
}
