// The Android app's own copy of the library, so every list has something to
// show with no connection - not only the screens that were opened before.
//
// Two halves. While the server answers, `primeOfflineLibrary` fetches the
// whole library in the background, once a day: every album and artist, every
// song, the playlists with their contents, the video folders, the radio
// stations, and which songs carry which genre and tag. With no connection, `offlineLibraryAnswer` answers the list
// requests the pages make (songs, albums, artists, with their filters, order
// and paging, and the genres and tags of a song, album or artist) from that copy, when the exact answer was never kept.

import type { Album, Artist, MediaItem } from '../types'
import { isNativeApp } from './bridge'
import { shouldWaitForWifi } from './downloadSettings'
import { isOffline } from './offline'
import { forgetAnswer, rememberAnswer, rememberedAnswer, rememberItems } from './offlineCache'

const ALBUMS_URL = '/api/library/albums?sort=alphabetical&direction=asc&full=1'
const ARTISTS_URL = '/api/library/artists?sort=alphabetical&direction=asc&full=1'
/** Every genre and tag with the ids of the songs that carry it. */
const LABELS_URL = '/api/library/labels/all'
/** Not an address of the server: where the whole song list is kept. */
const TRACKS_KEY = 'offline:tracks'
const PRIMED_AT_KEY = 'atriolyric.offlinePrimedAt'
/** How far an unfinished fetch got: { startedAt, done: step names, trackOffset }. */
const PROGRESS_KEY = 'atriolyric.offlinePrimeProgress'
/** Where each page of songs waits until the whole list is in (offline:*
 * answers are never trimmed). */
const TRACK_PAGE_KEY = (offset: number) => `offline:tracks-page:${offset}`
const PRIME_EVERY_MS = 24 * 60 * 60 * 1000
// Well after the start: the first half minute belongs to the playback
// connection and the song being loaded, which a slow mobile link cannot
// carry together with megabytes of library lists.
const PRIME_DELAY_MS = 30_000
const TRACK_PAGE = 500
const MAX_VIDEO_FOLDERS = 300

type Ask = <T>(path: string) => Promise<T>

interface AllLabels {
  genres: Record<string, number[]>
  tags: Record<string, number[]>
}

let priming = false
let primeTimer: number | undefined

function lastPrimedAt(): number {
  try {
    return Number(localStorage.getItem(PRIMED_AT_KEY)) || 0
  } catch {
    return 0
  }
}

const pause = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms))

interface PrimeProgress {
  startedAt: number
  done: string[]
  trackOffset: number
}

function readProgress(): PrimeProgress {
  try {
    const saved = JSON.parse(localStorage.getItem(PROGRESS_KEY) ?? 'null') as Partial<PrimeProgress> | null
    // A fetch begun more than a day ago starts over: what it got is a day old too.
    if (saved && Array.isArray(saved.done) && Date.now() - (saved.startedAt ?? 0) < PRIME_EVERY_MS) {
      return { startedAt: Number(saved.startedAt), done: saved.done.map(String), trackOffset: Math.max(0, Number(saved.trackOffset) || 0) }
    }
  } catch {
    // Unreadable: from the start.
  }
  return { startedAt: Date.now(), done: [], trackOffset: 0 }
}

function saveProgress(progress: PrimeProgress | null) {
  try {
    if (progress) localStorage.setItem(PROGRESS_KEY, JSON.stringify(progress))
    else localStorage.removeItem(PROGRESS_KEY)
  } catch {
    // Not resumable this time: the next start begins again.
  }
}

/** Whether the background fetch should not run now: the phone asks apps to
 * save data, or Wi-Fi only is chosen and the network is metered. */
async function mustWait(): Promise<boolean> {
  const saveData = (navigator as Navigator & { connection?: { saveData?: boolean } }).connection?.saveData === true
  return saveData || (await shouldWaitForWifi())
}

/** Thrown to stop a fetch that should wait (see mustWait); it resumes later. */
class Waiting extends Error {}

/** Fetches the library for offline use, one request at a time so it never
 * crowds what the user is doing. Done step by step, and each finished step
 * (and each page of songs) is noted: a fetch the connection cut short carries
 * on from there at the next start instead of fetching megabytes again from
 * the first page - on a flaky mobile link it might otherwise never finish.
 * `ask` is the API client's `request` (which keeps each answer); `askUnkept`
 * is the same without keeping the answer. */
async function prime(ask: Ask, askUnkept: Ask): Promise<void> {
  const progress = readProgress()
  saveProgress(progress)
  const step = async (name: string, work: () => Promise<void>) => {
    if (progress.done.includes(name)) return
    if (await mustWait()) throw new Waiting()
    await work()
    progress.done.push(name)
    saveProgress(progress)
  }

  await step('albums', async () => void (await ask<Album[]>(ALBUMS_URL)))
  await step('artists', async () => void (await ask<Artist[]>(ARTISTS_URL)))
  await step('labels', async () => void (await ask<AllLabels>(LABELS_URL).catch(() => undefined)))

  // Every song, in pages; each page waits as its own answer, then they are
  // kept as one list.
  await step('tracks', async () => {
    for (let offset = progress.trackOffset; ; offset += TRACK_PAGE) {
      const page = await askUnkept<MediaItem[]>(`/api/library/tracks?limit=${TRACK_PAGE}&offset=${offset}`)
      await rememberItems(page)
      await rememberAnswer(TRACK_PAGE_KEY(offset), page)
      progress.trackOffset = offset + TRACK_PAGE
      saveProgress(progress)
      if (page.length < TRACK_PAGE) break
      if (await mustWait()) throw new Waiting()
      await pause(150)
    }
    const tracks: MediaItem[] = []
    for (let offset = 0; offset < progress.trackOffset; offset += TRACK_PAGE) {
      const page = await rememberedAnswer<MediaItem[]>(TRACK_PAGE_KEY(offset))
      // A page lost meanwhile: the songs fetched again from the start next time.
      if (!page) {
        progress.trackOffset = 0
        saveProgress(progress)
        throw new Error('A page of songs went missing')
      }
      tracks.push(...page.body)
    }
    await rememberAnswer(TRACKS_KEY, tracks)
    for (let offset = 0; offset < progress.trackOffset; offset += TRACK_PAGE) await forgetAnswer(TRACK_PAGE_KEY(offset))
  })

  await step('lists', async () => {
    for (const path of ['/api/library/genres', '/api/radio/stations', '/api/library/mixes', '/api/library/mixes/options']) {
      await ask<unknown>(path).catch(() => undefined)
    }
  })

  await step('playlists', async () => {
    const playlists = await ask<{ id: number }[]>('/api/playlists')
    for (const playlist of playlists) {
      const detail = await ask<{ items?: MediaItem[] }>(`/api/playlists/${playlist.id}`).catch(() => null)
      if (detail?.items) await rememberItems(detail.items)
      await pause(100)
    }
  })

  // The video folders, level by level, with the videos in each.
  await step('videos', async () => {
    const folders = ['']
    for (let i = 0; i < folders.length && i < MAX_VIDEO_FOLDERS; i += 1) {
      const dir = folders[i]
      const videos = await ask<MediaItem[]>(`/api/library/videos?${new URLSearchParams({ dir, limit: '500' })}`).catch(() => [])
      await rememberItems(videos)
      const children = await ask<{ path: string }[]>(`/api/library/videos/folders?parent=${encodeURIComponent(dir)}`).catch(() => [])
      for (const child of children) if (typeof child.path === 'string' && !folders.includes(child.path)) folders.push(child.path)
      await pause(100)
    }
  })
}

/** Starts the background fetch a little after the app is up, unless it ran
 * in the last day. Android app only; a no-op in a browser. */
export function scheduleOfflinePrime(ask: Ask, askUnkept: Ask): void {
  if (!isNativeApp() || priming || primeTimer !== undefined) return
  if (Date.now() - lastPrimedAt() < PRIME_EVERY_MS) return
  primeTimer = window.setTimeout(() => {
    primeTimer = undefined
    if (isOffline() || priming) return
    priming = true
    prime(ask, askUnkept)
      .then(() => {
        saveProgress(null)
        try {
          localStorage.setItem(PRIMED_AT_KEY, String(Date.now()))
        } catch {
          // Fetched again next time.
        }
      })
      .catch(() => {
        // The connection went, the session ended, or the phone is on mobile
        // data with Wi-Fi only chosen: the next start carries on.
      })
      .finally(() => {
        priming = false
        // What the pages are answered from offline changed.
        forgetKeptCopies()
      })
  }, PRIME_DELAY_MS)
}

/** Another account: its copy is gone (offlineCache), so fetch again. */
export function forgetOfflinePrime(): void {
  try {
    localStorage.removeItem(PRIMED_AT_KEY)
    localStorage.removeItem(PROGRESS_KEY)
  } catch {
    // Nothing to forget.
  }
  forgetKeptCopies()
}

// The library lists read from IndexedDB, held for the visit: an offline page
// asks for songs, albums and artists again and again, and reading the whole
// song list back each time would copy tens of megabytes on a big library.
const keptCopies = new Map<string, Promise<unknown>>()

function forgetKeptCopies() {
  keptCopies.clear()
}

/** The kept answer for `key`, read once per visit (and again after a fetch). */
function keptCopy<T>(key: string): Promise<{ body: T } | undefined> {
  let pending = keptCopies.get(key) as Promise<{ body: T } | undefined> | undefined
  if (!pending) {
    pending = rememberedAnswer<T>(key)
    keptCopies.set(key, pending)
    // Nothing kept yet: look again next time rather than remember "nothing".
    void pending.then((found) => {
      if (!found && keptCopies.get(key) === pending) keptCopies.delete(key)
    })
  }
  return pending
}

// --- Answering from the copy ---------------------------------------------

function same(a: string | null | undefined, b: string): boolean {
  return (a ?? '').localeCompare(b, undefined, { sensitivity: 'accent' }) === 0
}

function has(text: string | null | undefined, part: string): boolean {
  return (text ?? '').toLowerCase().includes(part.toLowerCase())
}

function byText(a: string | null | undefined, b: string | null | undefined): number {
  return (a ?? '').localeCompare(b ?? '', undefined, { sensitivity: 'base', numeric: true })
}

function credited(item: { artist?: string | null; album_artist?: string | null; artists?: { name: string }[] }, name: string): boolean {
  return same(item.artist, name) || same(item.album_artist, name) || (item.artists ?? []).some((credit) => same(credit.name, name))
}

function paged<T>(rows: T[], params: URLSearchParams): T[] {
  if (params.get('full') === '1') return rows
  const offset = Math.max(0, Number(params.get('offset')) || 0)
  const limit = Number(params.get('limit'))
  return rows.slice(offset, Number.isFinite(limit) && limit > 0 ? offset + limit : undefined)
}

/** The ids of the songs carrying `name`, or null when the copy does not say. */
function carriers(labels: Record<string, number[]> | undefined, name: string): Set<number> | null {
  if (!labels) return null
  const key = Object.keys(labels).find((label) => same(label.trim(), name.trim()))
  return new Set(key ? labels[key] : [])
}

async function tracksAnswer(params: URLSearchParams): Promise<MediaItem[] | undefined> {
  const kept = await keptCopy<MediaItem[]>(TRACKS_KEY)
  if (!kept) return undefined
  const artist = params.get('artist')
  const album = params.get('album')
  const q = params.get('q')
  const genre = params.get('genre')
  const tag = params.get('tag')
  const labels = genre || tag ? (await keptCopy<AllLabels>(LABELS_URL))?.body : undefined
  const withGenre = genre ? carriers(labels?.genres, genre) : null
  const withTag = tag ? carriers(labels?.tags, tag) : null
  // Without the copy of the tags, what a tag holds cannot be told: nothing rather than everything.
  if (tag && !withTag) return []
  const rows = kept.body.filter((item) =>
    (params.get('artist_is_null') !== 'true' || !item.artist) &&
    (!artist || credited(item, artist)) &&
    (!album || same(item.album, album)) &&
    (!genre || (withGenre ? withGenre.has(item.id) : has(item.genre, genre))) &&
    (!withTag || withTag.has(item.id)) &&
    (!q || has(item.title, q) || has(item.artist, q) || has(item.album, q)))
  const sort = params.get('sort')
  const inAlbum = (a: MediaItem, b: MediaItem) => (a.track_number ?? 0) - (b.track_number ?? 0) || byText(a.title, b.title)
  rows.sort(
    sort === 'title' || sort === 'alphabetical'
      ? (a, b) => byText(a.title, b.title)
      : sort === 'album'
        ? (a, b) => byText(a.album, b.album) || inAlbum(a, b)
        : sort === 'year'
          ? (a, b) => (a.year ?? 0) - (b.year ?? 0) || byText(a.album, b.album) || inAlbum(a, b)
          // The server's default: artist, album, then the album's own order.
          : (a, b) => byText(a.artist, b.artist) || byText(a.album, b.album) || inAlbum(a, b),
  )
  if (params.get('direction') === 'desc') rows.reverse()
  return paged(rows, params)
}

async function albumsAnswer(params: URLSearchParams, exact: boolean): Promise<Album[] | undefined> {
  const kept = await keptCopy<Album[]>(ALBUMS_URL)
  if (!kept) return undefined
  const artist = params.get('artist')
  const album = params.get('album')
  const q = params.get('q')
  // Albums an artist only appears on are not told apart in the copy.
  if (params.get('appears_on') === 'true') return []
  const rows = kept.body.filter((entry) =>
    (!exact || (same(entry.album, album ?? '') && (artist == null ? !entry.artist : same(entry.artist, artist)))) &&
    (exact || !artist || credited(entry, artist)) &&
    (!q || has(entry.album, q) || has(entry.artist, q)))
  if (params.get('direction') === 'desc') rows.reverse()
  return exact ? rows : paged(rows, params)
}

async function artistsAnswer(params: URLSearchParams, names: string[] | null): Promise<Artist[] | undefined> {
  const kept = await keptCopy<Artist[]>(ARTISTS_URL)
  if (!kept) return undefined
  const q = params.get('q')
  const rows = kept.body.filter((entry) =>
    (!names || names.some((name) => same(entry.artist, name))) && (!q || has(entry.artist, q)))
  if (params.get('direction') === 'desc') rows.reverse()
  return names ? rows : paged(rows, params)
}

/** GET /api/library/labels: the genres and tags of one song, or pooled over
 * an artist's or album's songs, most common first (at most 100 of each). */
async function labelsAnswer(params: URLSearchParams): Promise<{ genres: unknown[]; tags: unknown[] } | undefined> {
  const labels = (await keptCopy<AllLabels>(LABELS_URL))?.body
  if (!labels) return undefined
  let ids: Set<number>
  const one = Number(params.get('media_item_id'))
  if (one > 0) {
    ids = new Set([one])
  } else {
    const only = new URLSearchParams()
    for (const name of ['artist', 'artist_is_null', 'album']) {
      const value = params.get(name)
      if (value) only.set(name, value)
    }
    const tracks = await tracksAnswer(only)
    if (!tracks) return undefined
    ids = new Set(tracks.map((track) => track.id))
  }
  const pooled = (all: Record<string, number[]>) =>
    Object.entries(all)
      .map(([name, carrying]) => ({ name, track_count: carrying.filter((id) => ids.has(id)).length }))
      .filter((label) => label.track_count > 0)
      .sort((a, b) => b.track_count - a.track_count || byText(a.name, b.name))
      .slice(0, 100)
  return { genres: pooled(labels.genres), tags: pooled(labels.tags) }
}

/** With no connection and no kept answer for exactly this request: the
 * answer worked out from the app's copy of the library, for the song, album
 * and artist lists and for genres and tags. Undefined for anything else, or before the copy exists. */
export async function offlineLibraryAnswer<T>(path: string): Promise<{ body: T } | undefined> {
  if (!isNativeApp()) return undefined
  const [pathname, query = ''] = path.split('?')
  const params = new URLSearchParams(query)
  let body: unknown
  if (pathname === '/api/library/tracks') body = await tracksAnswer(params)
  else if (pathname === '/api/library/albums') body = await albumsAnswer(params, false)
  else if (pathname === '/api/library/album-summaries') body = await albumsAnswer(params, true)
  else if (pathname === '/api/library/artists') body = await artistsAnswer(params, null)
  else if (pathname === '/api/library/artist-summaries') body = await artistsAnswer(params, params.getAll('names'))
  else if (pathname === '/api/library/labels') body = await labelsAnswer(params)
  return body === undefined ? undefined : { body: body as T }
}
