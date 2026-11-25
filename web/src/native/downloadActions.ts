// Starting downloads in the Android app: which URL an item is fetched from at
// the chosen download quality (downloadSettings.ts), and keeping downloaded
// albums, artists and playlists in step with the server.

import { useSyncExternalStore } from 'react'
import { api, ApiError, onCollectionsChanged, request } from '../api'
import type { MediaItem } from '../types'
import { isNativeApp, nativeCall, type NativeDownload } from './bridge'
import {
  downloadOf,
  downloadsReady,
  downloadsSnapshot,
  downloadStatus,
  removeDownload,
  setPreparing,
  subscribeDownloads,
  type DownloadStatus,
} from './downloadStore'
import { isOffline, onOfflineChange } from './offline'
import { rememberAnswer, rememberItems } from './offlineCache'
import {
  downloadSettings,
  sendRequirements,
  shouldWaitForWifi,
  type DownloadSettings,
  type VideoDownloadQuality,
} from './downloadSettings'

export {
  downloadSettings,
  shouldWaitForWifi,
  updateDownloadSettings,
  useDownloadSettings,
  VIDEO_DOWNLOAD_QUALITIES,
  type DownloadSettings,
  type VideoDownloadQuality,
} from './downloadSettings'

export function videoQualityLabel(quality: string): string {
  return quality === 'original' ? 'Original' : `${quality}p`
}

export function musicQualityLabel(kbps: number | null): string {
  return kbps == null ? 'Original' : `${kbps} kbps`
}

interface Rendition {
  quality: string
  status: 'none' | 'queued' | 'preparing' | 'ready' | 'failed'
  progress: number | null
  size: number | null
  url: string
}

function absolute(path: string): string {
  return new URL(path, window.location.origin).href
}

function sendToDevice(item: MediaItem, url: string, quality: string, qualityLabel: string, replace = false): Promise<void> {
  return nativeCall('downloadStart', {
    mediaId: item.id,
    // The app swaps the copy on the device for this one (and finishes the
    // swap after a restart), instead of refusing a second download.
    replace,
    kind: item.kind,
    url: absolute(url),
    quality,
    qualityLabel,
    title: item.title,
    artist: item.artist ?? '',
    album: item.album ?? '',
    duration: item.duration_seconds ?? 0,
    artUrl: absolute(item.kind === 'track' ? api.artUrl(item.id) : api.thumbnailUrl(item.id)),
  })
}

const wait = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms))

/** The level to fetch for a video: the chosen one when the server offers it
 * (it only offers levels below the source's height), else the best one below
 * it, else the original - never an upscale. */
function pickRendition(wanted: VideoDownloadQuality, offered: Rendition[]): Rendition | null {
  if (wanted === 'original') return null
  const below = offered
    .filter((r) => Number(r.quality) <= Number(wanted))
    .sort((a, b) => Number(b.quality) - Number(a.quality))
  return below[0] ?? null
}

/** The level `mediaId` is saved at when `wanted` is chosen; null for the original. */
async function videoRendition(mediaId: number, wanted: VideoDownloadQuality): Promise<Rendition | null> {
  if (wanted === 'original') return null
  const { renditions } = await request<{ renditions: Rendition[] }>(`/api/download/video/${mediaId}/renditions`)
  return pickRendition(wanted, renditions)
}

async function downloadVideo(item: MediaItem, wanted: VideoDownloadQuality, replace = false): Promise<void> {
  const base = `/api/download/video/${item.id}/renditions`
  const rendition = await videoRendition(item.id, wanted)
  if (!rendition) {
    await sendToDevice(item, `/api/download/media/${item.id}`, 'original', 'Original', replace)
    return
  }
  // The server makes the file first (one video at a time); the download starts when it is ready.
  const label = videoQualityLabel(rendition.quality)
  let state = await request<Rendition>(`${base}/${rendition.quality}`, { method: 'POST' })
  try {
    while (state.status === 'queued' || state.status === 'preparing' || state.status === 'none') {
      setPreparing({ mediaId: item.id, title: item.title, qualityLabel: label, progress: state.progress })
      await wait(2000)
      state = await request<Rendition>(`${base}/${rendition.quality}`)
      // A server restart drops its queue: ask again.
      if (state.status === 'none') state = await request<Rendition>(`${base}/${rendition.quality}`, { method: 'POST' })
    }
  } finally {
    setPreparing({ mediaId: item.id, done: true })
  }
  if (state.status !== 'ready') throw new Error(`The server could not prepare "${item.title}" at ${label}.`)
  // Only now, with the new file ready on the server, is the copy on the
  // device replaced: the old one stays playable for all the minutes the
  // server needs, and nothing is lost if the app is closed meanwhile.
  await sendToDevice(item, state.url, rendition.quality, label, replace)
}

/** Downloads one song or video to the device at the download quality.
 * Something already on the device, or on its way, is not fetched again:
 * resolves false for it - unless `replace` is set, which swaps the copy on
 * the device for one at the quality now chosen. */
export async function downloadToDevice(item: MediaItem, options: { replace?: boolean } = {}): Promise<boolean> {
  const replace = options.replace === true
  const status = downloadStatus(item.id)
  if (!replace && (status === 'downloaded' || status === 'downloading')) return false
  const chosen = downloadSettings()
  void nativeCall('requestNotifications').catch(() => {})
  // What the app shows for it with no connection.
  void rememberItems([item])
  if (item.kind === 'track') {
    void keepLyrics(item.id)
    await sendToDevice(
      item,
      api.streamUrl(item.id, chosen.musicKbps),
      chosen.musicKbps == null ? 'original' : String(chosen.musicKbps),
      musicQualityLabel(chosen.musicKbps),
      replace,
    )
    return true
  }
  await downloadVideo(item, chosen.video, replace)
  return true
}

/** A downloaded song's lyrics are kept with it, for reading with no
 * connection. Not while the server is still looking for them (`pending`):
 * that answer would be shown for good. */
async function keepLyrics(mediaId: number): Promise<void> {
  const path = `/api/library/media/${mediaId}/lyrics`
  try {
    // A few tries: the first answer is often "still looking".
    for (let attempt = 0; attempt < 4; attempt += 1) {
      const lyrics = await request<{ pending?: boolean }>(path, undefined, { background: true, unkept: true })
      if (!lyrics.pending) {
        await rememberAnswer(path, lyrics)
        return
      }
      await wait(20_000)
    }
  } catch {
    // No lyrics offline for this one.
  }
}

// --- Changing the quality ------------------------------------------------
// What is on the device stays at the quality it was saved at, so a change of
// quality deletes the copies that no longer match and fetches them again.

function isVideo(download: NativeDownload): boolean {
  return download.kind !== 'track'
}

function musicQualityId(kbps: number | null): string {
  return kbps == null ? 'original' : String(kbps)
}

/** The downloads a change to `patch` would replace: the songs, or the
 * videos, saved at another quality than the new one. (A video smaller than
 * the chosen size is counted too; it is left alone when it comes to it.) */
export function downloadsAtOtherQuality(patch: Partial<DownloadSettings>): NativeDownload[] {
  return downloadsSnapshot().items.filter((download) => {
    if (download.state === 'removing') return false
    if (isVideo(download)) return patch.video !== undefined && download.quality !== patch.video
    return patch.musicKbps !== undefined && download.quality !== musicQualityId(patch.musicKbps)
  })
}

/** Downloads each of `stale` again at the quality now chosen, replacing the
 * copy on the device. Something the library no longer has, and a video that
 * would come out at the size it already has, stays. The copy on the device is
 * only swapped once the new one can start (for a video, once the server has
 * prepared it), and the app finishes a swap after a restart, so closing the
 * app midway loses nothing. One item that fails does not stop the others. */
export async function redownloadAtChosenQuality(stale: NativeDownload[]): Promise<{ started: number; failed: number }> {
  const chosen = downloadSettings()
  const items = new Map((await api.getMediaByIds(stale.map((download) => download.mediaId))).map((item) => [item.id, item]))
  let started = 0
  let failed = 0
  // Songs first: a video may wait minutes for the server to prepare it.
  for (const download of [...stale.filter((d) => !isVideo(d)), ...stale.filter(isVideo)]) {
    const item = items.get(download.mediaId)
    if (!item) continue
    try {
      if (isVideo(download)) {
        const rendition = await videoRendition(item.id, chosen.video)
        if ((rendition?.quality ?? 'original') === download.quality) continue
      }
      if (await downloadToDevice(item, { replace: true })) started += 1
    } catch {
      // Left at the quality it has; the setting's next change offers it again.
      failed += 1
    }
  }
  return { started, failed }
}

// --- Albums, artists and playlists ---------------------------------------
// A collection the user downloaded is kept downloaded: it is remembered with
// the items it holds, what it gains later is fetched and what leaves it is
// taken off the device, unless something else downloaded holds it (see
// `syncCollections`). Its menu and its mark read that record, without asking
// the server. A generated mix is not offered: it is saved as a playlist first.

type CollectionRecords = Record<string, number[]>

const COLLECTIONS_KEY = 'atriolyric.downloadCollections'
let records: CollectionRecords | null = null
const recordListeners = new Set<() => void>()

function collectionRecords(): CollectionRecords {
  if (records) return records
  try {
    const parsed = JSON.parse(localStorage.getItem(COLLECTIONS_KEY) ?? '{}') as CollectionRecords
    records = parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    records = {}
  }
  return records
}

function writeRecords(next: CollectionRecords) {
  records = next
  try {
    localStorage.setItem(COLLECTIONS_KEY, JSON.stringify(next))
  } catch {
    // Kept for this visit only.
  }
  for (const listener of [...recordListeners]) listener()
}

function recordKey(kind: string, key: string | number): string {
  return `${kind}:${key}`
}

function collectionStatus(kind: string, key: string | number): DownloadStatus {
  const ids = collectionRecords()[recordKey(kind, key)]
  if (!ids) return 'none'
  // Kept downloaded, also while it is empty or its items were removed one by one.
  return ids.some((id) => downloadStatus(id) === 'downloading') ? 'downloading' : 'downloaded'
}

/** Where an album, artist or playlist that is kept downloaded stands;
 * 'none' when it is not (or in a browser). */
export function useCollectionDownloadStatus(kind: string, key: string | number): DownloadStatus {
  return useSyncExternalStore(
    (listener) => {
      recordListeners.add(listener)
      const stop = subscribeDownloads(listener)
      return () => {
        recordListeners.delete(listener)
        stop()
      }
    },
    () => collectionStatus(kind, key),
  )
}

/** What the collection holds now: ids from the library, not from the disk. */
function idsPath(kind: string, key: string | number): string {
  return `/api/download/${kind}/${encodeURIComponent(String(key))}/ids`
}

function askIds(kind: string, key: string | number): Promise<number[]> {
  // Never a kept answer: what was added and what left is decided from this.
  return request<number[]>(idsPath(kind, key), undefined, { background: true, unkept: true })
}

// A song or video downloaded on its own is remembered too ("media:<id>"),
// so that leaving a downloaded playlist does not take it off the device.
const LOOSE_KEPT_KEY = 'atriolyric.downloadSinglesKept'
/** A single download just asked for is not on the device's list yet. */
const SINGLE_GRACE_MS = 10 * 60_000
const singleAskedAt = new Map<string, number>()

/** Another account signs in, or this one signs out: which collections were
 * kept downloaded belongs to the account that chose them. (The files on the
 * device stay: they are the device's, and removing them is the user's call.) */
export function forgetDownloadRecords(): void {
  records = {}
  singleAskedAt.clear()
  syncedAt = 0
  try {
    localStorage.removeItem(COLLECTIONS_KEY)
    localStorage.removeItem(LOOSE_KEPT_KEY)
  } catch {
    // Nothing kept to forget.
  }
  for (const listener of [...recordListeners]) listener()
}

/** Once: what is on the device and belongs to no downloaded collection is
 * remembered as downloaded on its own. Then, every time: forgets the single
 * downloads that were removed since. */
async function tidySingles(): Promise<void> {
  await downloadsReady()
  const all = collectionRecords()
  const next: CollectionRecords = {}
  let changed = false
  for (const [name, ids] of Object.entries(all)) {
    const settled = Date.now() - (singleAskedAt.get(name) ?? 0) > SINGLE_GRACE_MS
    if (name.startsWith('media:') && settled && ids.every((id) => downloadStatus(id) === 'none')) changed = true
    else next[name] = ids
  }
  let migrated = true
  try {
    migrated = localStorage.getItem(LOOSE_KEPT_KEY) === '1'
    localStorage.setItem(LOOSE_KEPT_KEY, '1')
  } catch {
    // No storage: nothing to migrate from either.
  }
  if (!migrated) {
    const held = new Set(Object.values(next).flat())
    for (const download of downloadsSnapshot().items) {
      if (held.has(download.mediaId)) continue
      next[recordKey('media', download.mediaId)] = [download.mediaId]
      changed = true
    }
  }
  if (changed) writeRecords(next)
}

/** Takes off the device those of `ids` that nothing downloaded holds any
 * more. Resolves with how many went. */
async function removeUnheld(ids: number[]): Promise<number> {
  const held = new Set(Object.values(collectionRecords()).flat())
  let removed = 0
  for (const id of ids) {
    if (held.has(id) || !downloadOf(id)) continue
    await removeDownload(id)
    removed += 1
  }
  return removed
}

/** Downloads the items `ids` that are not on the device yet; how many were started. */
async function downloadIds(ids: number[]): Promise<number> {
  const items = await api.getMediaByIds(ids)
  let started = 0
  // Songs go straight to the device's queue; videos wait their turn on the server.
  for (const item of [...items.filter((i) => i.kind === 'track'), ...items.filter((i) => i.kind !== 'track')]) {
    if (await downloadToDevice(item)) started += 1
  }
  return started
}

/** Downloads every item of an album, artist or playlist (or one media item
 * by id) that is not on the device yet, and keeps the collection downloaded
 * from then on. A song two downloaded collections share is on the device
 * once. Resolves with how many were started and how many were already there. */
export async function downloadCollectionToDevice(
  kind: string,
  key: string | number,
): Promise<{ started: number; already: number }> {
  await tidySingles()
  const ids = kind === 'media' ? [Number(key)] : await askIds(kind, key)
  if (kind === 'media') singleAskedAt.set(recordKey(kind, key), Date.now())
  writeRecords({ ...collectionRecords(), [recordKey(kind, key)]: ids })
  const started = await downloadIds(ids)
  return { started, already: ids.length - started }
}

/** Stops keeping an album, artist or playlist (or one item) downloaded and
 * takes it off the device. An item that something else downloaded also
 * holds stays. Resolves with how many went. */
export async function removeCollectionFromDevice(kind: string, key: string | number): Promise<number> {
  await tidySingles()
  const { [recordKey(kind, key)]: ids = kind === 'media' ? [Number(key)] : [], ...others } = collectionRecords()
  writeRecords(others)
  // One item, asked for by name: off the device whatever else holds it. (The
  // collections that hold it do not fetch it again.)
  if (kind === 'media') {
    if (!downloadOf(Number(key))) return 0
    await removeDownload(Number(key))
    return 1
  }
  return removeUnheld(ids)
}

// --- Keeping them downloaded ---------------------------------------------

const SYNC_EVERY_MS = 60 * 60_000
const SYNC_AFTER_CHANGE_MS = 3_000
let syncing = false
let syncAgain = false
let syncedAt = 0
let syncTimer: number | undefined
let syncStarted = false

/** Brings one kept collection in step with the server: fetches what it
 * gained, and takes off the device what left it and nothing else downloaded
 * holds. An item the user removed from the device by hand is not brought back. */
async function syncCollection(name: string): Promise<void> {
  const split = name.indexOf(':')
  const kind = name.slice(0, split)
  if (kind === 'media') return
  let ids: number[]
  try {
    ids = await askIds(kind, name.slice(split + 1))
  } catch (error) {
    // Gone from the server (a deleted playlist): no longer kept, and off the
    // device. Only on the server's own word for it - not while there is no
    // connection, and not from a server too old to know the question.
    if (error instanceof ApiError && error.status === 404 && error.message !== 'Not Found' && !isOffline()) {
      const { [name]: left = [], ...others } = collectionRecords()
      writeRecords(others)
      await removeUnheld(left)
    }
    return
  }
  const known = collectionRecords()[name]
  if (!known || !Array.isArray(ids)) return
  const before = new Set(known)
  const now = new Set(ids)
  const fresh = ids.filter((id) => !before.has(id))
  const left = known.filter((id) => !now.has(id))
  if (fresh.length === 0 && left.length === 0) return
  if (fresh.length > 0) await downloadIds(fresh)
  // Still kept (not turned off meanwhile): remembered as it is now.
  if (!collectionRecords()[name]) return
  writeRecords({ ...collectionRecords(), [name]: ids })
  await removeUnheld(left)
}

/** Looks at every album, artist and playlist that is kept downloaded, one
 * at a time, in the background. `force`: also when the last look was within
 * the hour (something changed in this app). */
export async function syncCollections(force = false): Promise<void> {
  if (!isNativeApp() || isOffline()) return
  if (syncing) {
    syncAgain = syncAgain || force
    return
  }
  if (!force && syncedAt > 0 && Date.now() - syncedAt < SYNC_EVERY_MS) return
  syncing = true
  try {
    // On mobile data with Wi-Fi only chosen: not now (the next look, every
    // quarter hour or when the app comes back, tries again).
    if (await shouldWaitForWifi()) return
    await tidySingles()
    let complete = true
    for (const name of Object.keys(collectionRecords())) {
      if (isOffline()) {
        complete = false
        break
      }
      await syncCollection(name).catch(() => {
        // The connection went, or one item would not start: the next look tries again.
        complete = false
      })
    }
    if (complete) syncedAt = Date.now()
  } finally {
    syncing = false
    if (syncAgain) {
      syncAgain = false
      void syncCollections(true)
    }
  }
}

/** Keeps the downloaded collections current from now on: a look shortly
 * after the app is up, then once an hour (checked when the app comes back
 * to the front or finds its server again, too), and right after a playlist
 * changes in this app. Android app only; a no-op in a browser. */
export function startCollectionSync(): void {
  if (syncStarted || !isNativeApp()) return
  syncStarted = true
  // The app's download manager learns the Wi-Fi choice once per start too
  // (it also keeps it), in case the choice was made in an older app.
  sendRequirements(downloadSettings().wifiOnly)
  window.setTimeout(() => void syncCollections(), 5_000)
  window.setInterval(() => void syncCollections(), SYNC_EVERY_MS / 4)
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') void syncCollections()
  })
  onOfflineChange(() => void syncCollections())
  onCollectionsChanged(() => {
    window.clearTimeout(syncTimer)
    syncTimer = window.setTimeout(() => void syncCollections(true), SYNC_AFTER_CHANGE_MS)
  })
}
