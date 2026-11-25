import type { components } from './api-types'
import { withArtEpoch } from './artEpoch'
import type { AudioSettings } from './audioEffects'
import type {
  ActivityLog,
  AdminUser,
  Album,
  AlbumInfo,
  ArtistInfo,
  ArtistMatchCandidate,
  ArtistTopTracks,
  Artist,
  ArtistSplitRules,
  BackgroundStatus,
  BrowseResult,
  BrowseSort,
  TrackSort,
  CollectionPreference,
  CurrentUser,
  DiscoveryExplore,
  DiscoverySettings,
  DiscoveryState,
  ImageCredit,
  UnrecordedArt,
  GeneratePlaylistResult,
  Labels,
  Integration,
  LibraryRoot,
  LibraryRootSummary,
  LibraryStats,
  LibraryStorage,
  LibrarySearchResult,
  MediaItem,
  MixPlaylist,
  MixOption,
  Playlist,
  PlaylistDetail,
  PlaySource,
  PlaySourceEntry,
  PreferenceState,
  RadioStation,
  LibraryTask,
  ScanMode,
  FileSearchResult,
  TagEdit,
  TrackInfo,
  VideoFolder,
  WantListItem,
} from './types'
import { beginRequest } from './networkActivity'
import { notePlaySourceRecorded } from './player/playReports'
import { isNativeApp } from './native/bridge'
import { checkOffline, isOffline, noteServerAnswered } from './native/offline'
import { keepsAnswer, rememberAnswer, rememberedAnswer, rememberItems } from './native/offlineCache'
import { offlineMediaItems } from './native/downloadStore'
import { offlineLibraryAnswer, scheduleOfflinePrime } from './native/offlineLibrary'

export class ApiError extends Error {
  /** HTTP status, or 0 when there was no answer at all (offline, a timeout). */
  status: number
  /** Seconds from the response's Retry-After header (sent with a 429). */
  retryAfterSeconds: number | null
  constructor(status: number, message: string, retryAfterSeconds: number | null = null) {
    super(message)
    this.status = status
    this.retryAfterSeconds = retryAfterSeconds
  }
}

// What to do when the server says the session is gone (expired after its
// fixed lifetime, logged out elsewhere, revoked by a password change or an
// admin). Without it the app would keep showing the signed-in shell with
// empty lists and error toasts. AuthContext registers it.
let unauthorizedHandler: (() => void) | null = null

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler
}

/** For a 401 that did not come through `request` (the playback socket). */
export function notifyUnauthorized(): void {
  unauthorizedHandler?.()
}

// A 401 from these means wrong credentials or "not signed in yet", not that a
// session expired. The first /api/auth/me probe opts out with `authProbe`.
const NO_SESSION_PATHS = new Set(['/api/auth/login', '/api/auth/logout', '/api/setup', '/api/setup/status'])

function formatApiErrorDetail(detail: unknown): string | null {
  if (typeof detail === 'string') return detail
  if (!Array.isArray(detail)) return null

  const messages = detail
    .map((item) => {
      if (!item || typeof item !== 'object') return null
      const message = 'msg' in item && typeof item.msg === 'string' ? item.msg : null
      const location = 'loc' in item && Array.isArray(item.loc)
        ? item.loc.filter((part: unknown) => part !== 'body').join('.')
        : ''
      return message ? (location ? `${location}: ${message}` : message) : null
    })
    .filter((message): message is string => Boolean(message))

  return messages.length ? messages.join('; ') : null
}

// Albums, artists, and Home are unmounted when their tabs are inactive. Home
// also fans out into several read requests, so keep its read-only pieces in
// the same short-lived, memory-only cache. Library changes invalidate it.
const BROWSE_CACHE_MAX_AGE_MS = 60_000
const HOME_CACHE_MAX_AGE_MS = 60_000

interface BrowseCacheEntry {
  value?: unknown
  receivedAt?: number
  inFlight?: Promise<unknown>
}

const browseCache = new Map<string, BrowseCacheEntry>()
const homeCache = new Map<string, BrowseCacheEntry>()
let browseCacheGeneration = 0

/** Clears the Home snapshot after a play or Home-owned mutation. */
export function clearHomeCache() {
  browseCacheGeneration += 1
  homeCache.clear()
}

// Bumped by every library change (not by a play): kept-mounted tabs that
// skip a refetch while their data is fresh (useAsync's `staleMs`) refetch
// after one anyway.
let libraryGeneration = 0

/** Changes whenever the library (or the account) changed: data loaded under
 * an older value may be out of date. */
export function currentLibraryGeneration(): number {
  return libraryGeneration
}

const libraryChangeListeners = new Set<() => void>()

/** Hears every library change (the sidebar counts refresh on it). */
export function onLibraryChange(listener: () => void): () => void {
  libraryChangeListeners.add(listener)
  return () => {
    libraryChangeListeners.delete(listener)
  }
}

/** Something counted in the library changed without making the cached
 * lists wrong (a radio station added or removed). */
export function noteLibraryChange() {
  libraryGeneration += 1
  libraryChangeListeners.forEach((listener) => listener())
}

/** Clears all in-memory read caches after login/logout or a library change. */
export function clearBrowseCache() {
  // A request already on its way may predate the change: the next caller
  // asks afresh instead of sharing it.
  inFlightRequests.clear()
  noteLibraryChange()
  browseCacheGeneration += 1
  browseCache.clear()
  homeCache.clear()
}

/** An album page's (artist, album) - `artist` is null for the untagged
 * artist group, which the server reads as "artist is empty". */
function albumPreferenceQuery(artist: string | null, album: string): URLSearchParams {
  const params = new URLSearchParams({ album })
  if (artist != null) params.set('artist', artist)
  return params
}

// What to call a request in the slow-loading indicator, by path prefix. The
// first match wins; paths with no entry (background bookkeeping such as
// play history) are counted but never announced.
const REQUEST_LABELS: [string, string][] = [
  ['/api/library/albums', 'albums'],
  ['/api/library/artists', 'artists'],
  ['/api/library/tracks', 'songs'],
  ['/api/library/videos', 'videos'],
  ['/api/library/mixes', 'mixes'],
  ['/api/library/genres', 'genres'],
  ['/api/library/labels', 'tags'],
  ['/api/library/search', 'search results'],
  ['/api/library/files', 'files'],
  ['/api/library/browse', 'files'],
  ['/api/library/recently-added', 'recently added'],
  ['/api/library/recently-played', 'recently played'],
  ['/api/library/discovery', 'discovery'],
  ['/api/playlists', 'playlists'],
  ['/api/radio', 'radio stations'],
  ['/api/auth/me', 'your account'],
]

function requestLabel(path: string, method: string | undefined): string | undefined {
  if (method && method !== 'GET') return undefined
  return REQUEST_LABELS.find(([prefix]) => path.startsWith(prefix))?.[1]
}

/** How long a request may take before it is given up on. A request that hangs
 * (a network change mid-flight, a proxy holding the socket) would otherwise
 * leave its view on "Loading…" for ever and, for the cached browse calls,
 * every later caller waiting on the same dead promise. */
export const REQUEST_TIMEOUT_MS = 30_000
/** For calls that legitimately take longer: generating mixes and playlists
 * (ListenBrainz and the whole library), re-tagging many files, relinking artists. */
export const LONG_REQUEST_TIMEOUT_MS = 5 * 60_000

const LONG = { timeoutMs: LONG_REQUEST_TIMEOUT_MS }

export const TIMEOUT_MESSAGE = 'The server took too long to respond. Check your connection and try again.'
export const UNREACHABLE_MESSAGE = 'Could not reach the server. Check your connection and try again.'

/** What to tell the user when the server answered with an error and no
 * message of its own (a proxy's 502 page, an unhandled 500), instead of the
 * raw "POST /api/… failed: 502". */
function plainStatusMessage(status: number): string {
  if (status === 401) return 'Your session has ended. Sign in again.'
  if (status === 403) return 'You do not have permission to do that.'
  if (status === 404) return 'That could not be found. It may have been removed.'
  if (status === 408 || status === 504) return TIMEOUT_MESSAGE
  if (status === 429) return 'Too many requests. Wait a moment and try again.'
  if (status >= 500) return 'The server ran into a problem. Try again in a moment.'
  return 'That did not work. Try again.'
}

interface RequestOptions {
  authProbe?: boolean
  /** A request nobody asked for (the sidebar counts, fetched once the page is
   * idle): its 401 is not acted on. Signing the user out is left to the
   * requests that make up what they are looking at, so a background call that
   * happens to land first never pulls the page out from under them. */
  background?: boolean
  /** The Android app does not keep this answer for offline use (the caller
   * keeps it some other way). */
  unkept?: boolean
  /** Overrides the 30-second default; `null` means no limit. */
  timeoutMs?: number | null
  /** Hears a successful answer's headers (the Want list's "still matching"). */
  onHeaders?: (headers: Headers) => void
}

/** No answer from the server itself: nothing came back, or the proxy in
 * front of it answered for a server that is down. */
function isNoAnswer(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 0 || (error.status >= 502 && error.status <= 504))
}

/** In the Android app with no answer from the server: the last answer kept
 * for this read request, if there is one; otherwise the error itself. Also
 * starts the check that decides whether the app is offline. */
async function keptAnswerOr<T>(path: string, keep: boolean, error: unknown): Promise<T> {
  if (isNativeApp()) {
    if (isNoAnswer(error)) {
      void checkOffline()
      const kept = keep ? (await rememberedAnswer<T>(path)) ?? (await offlineLibraryAnswer<T>(path)) : undefined
      if (kept) return kept.body
    } else if (error instanceof ApiError) {
      noteServerAnswered()
    }
  }
  throw error
}

export async function request<T>(path: string, init?: RequestInit, options: RequestOptions = {}): Promise<T> {
  // The Android app keeps the answers to read requests (native/offlineCache.ts):
  // with no connection a page shows what it showed last, at once.
  const keep = keepsAnswer(path, init?.method)
  if (keep && isOffline()) {
    // The answer from last time, or one worked out from the app's copy of
    // the library (native/offlineLibrary.ts).
    const kept = (await rememberedAnswer<T>(path)) ?? (await offlineLibraryAnswer<T>(path))
    if (kept) return kept.body
  }
  let body: T
  try {
    body = await requestServer<T>(path, init, options)
  } catch (error) {
    return keptAnswerOr<T>(path, keep, error)
  }
  if (isNativeApp()) {
    noteServerAnswered()
    // Not an answer the server is still completing (`pending`: lyrics, info
    // pages): offline it would be shown, and asked for again, for good.
    const pending = (body as { pending?: unknown } | null)?.pending === true
    if (keep && !options.unkept && !pending) void rememberAnswer(path, body)
  }
  if (init?.method && init.method !== 'GET' && (COLLECTION_PATHS.some((prefix) => path.startsWith(prefix)) || path.includes('/preference'))) {
    for (const listener of [...collectionListeners]) listener()
  }
  return body
}

// Changes to what a playlist holds (a like or dislike changes Favourites).
const COLLECTION_PATHS = ['/api/playlists']
const collectionListeners = new Set<() => void>()

/** Calls `listener` after every change to a playlist (the Android app then
 * brings a downloaded one in step). */
export function onCollectionsChanged(listener: () => void): () => void {
  collectionListeners.add(listener)
  return () => {
    collectionListeners.delete(listener)
  }
}

/** In the Android app: fetch the whole library in the background (once a
 * day) so every list has something to show with no connection. Called once
 * someone is signed in. */
export function primeOfflineLibrary(): void {
  scheduleOfflinePrime(
    (path) => request(path, undefined, { background: true }),
    (path) => request(path, undefined, { background: true, unkept: true }),
  )
}

async function requestServer<T>(path: string, init: RequestInit | undefined, options: RequestOptions): Promise<T> {
  // Counted so the art loader's background downloads wait for data requests,
  // and labelled so a slow one can be named to the user.
  // A background request (the offline copy of the library, the sidebar
  // counts) is not something the user is waiting for: never announced.
  const end = beginRequest(options.background ? undefined : requestLabel(path, init?.method))
  const timeoutMs = options.timeoutMs === undefined ? REQUEST_TIMEOUT_MS : options.timeoutMs
  // One controller for both ways out: the caller's own signal and the timeout.
  // A plain timer rather than AbortSignal.timeout so a test's fake clock (and
  // older browsers, which lack AbortSignal.any) behave the same.
  const controller = new AbortController()
  const callerSignal = init?.signal ?? null
  let timedOut = false
  const timer =
    timeoutMs == null
      ? undefined
      : setTimeout(() => {
          timedOut = true
          controller.abort()
        }, timeoutMs)
  const onCallerAbort = () => controller.abort(callerSignal?.reason)
  if (callerSignal?.aborted) onCallerAbort()
  else callerSignal?.addEventListener('abort', onCallerAbort, { once: true })
  try {
    let res: Response
    try {
      res = await fetch(path, {
        headers: { 'Content-Type': 'application/json' },
        ...init,
        signal: controller.signal,
      })
    } catch (error) {
      // The caller's own abort stays an AbortError (views ignore it); only a
      // timeout or a dead connection becomes a message for the user.
      if (callerSignal?.aborted) throw error
      if (timedOut) throw new ApiError(0, TIMEOUT_MESSAGE)
      if (error instanceof TypeError) throw new ApiError(0, UNREACHABLE_MESSAGE)
      throw error
    }
    try {
      return await readResponse<T>(res, path, options)
    } catch (error) {
      // The body is read under the same signal, so a stall here is a timeout too.
      if (!(error instanceof ApiError) && !callerSignal?.aborted && timedOut) throw new ApiError(0, TIMEOUT_MESSAGE)
      throw error
    }
  } finally {
    clearTimeout(timer)
    callerSignal?.removeEventListener('abort', onCallerAbort)
    end()
  }
}

/** An answer turned into the result or the ApiError a caller expects. */
async function readResponse<T>(res: Response, path: string, options: RequestOptions): Promise<T> {
  if (!res.ok) {
    // FastAPI's HTTPException bodies carry the actually-useful message in
    // `detail` - fall back to a plain one only when that's not there
    // (e.g. a non-FastAPI error, or a body that isn't JSON at all).
    let message = plainStatusMessage(res.status)
    try {
      const body = await res.json()
      const detailMessage = formatApiErrorDetail(body?.detail)
      if (detailMessage) message = detailMessage
    } catch {
      // body wasn't JSON - keep the plain message
    }
    if (res.status === 401 && !options.authProbe && !options.background && !NO_SESSION_PATHS.has(path.split('?')[0])) {
      unauthorizedHandler?.()
    }
    const retryAfter = Number(res.headers.get('Retry-After'))
    throw new ApiError(res.status, message, res.headers.has('Retry-After') && Number.isFinite(retryAfter) ? retryAfter : null)
  }
  options.onHeaders?.(res.headers)
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

// The sign-in and first-run checks public/boot.js starts while the app
// downloads. Taken once each; when the app starts first it marks them taken
// so boot.js sends nothing.
type BootRequests = Partial<Record<'me' | 'setup', Promise<Response>>>

declare global {
  interface Window {
    __atrioBoot?: BootRequests
  }
}

function takeBootRequest(name: keyof BootRequests): Promise<Response> | undefined {
  const boot = (window.__atrioBoot ??= {})
  const pending = boot[name]
  delete boot[name]
  return pending
}

/** A request boot.js already sent, read like `request` would read it (with
 * the same time limit); `fallback` when there is none. */
async function adoptBootRequest<T>(
  name: keyof BootRequests,
  path: string,
  options: RequestOptions,
  fallback: () => Promise<T>,
): Promise<T> {
  const pending = takeBootRequest(name)
  if (!pending) return fallback()
  const keep = keepsAnswer(path, 'GET')
  try {
    const body = await readBootRequest<T>(pending, path, options)
    if (keep) void rememberAnswer(path, body)
    return body
  } catch (error) {
    return keptAnswerOr<T>(path, keep, error)
  }
}

async function readBootRequest<T>(pending: Promise<Response>, path: string, options: RequestOptions): Promise<T> {
  const end = beginRequest(requestLabel(path, 'GET'))
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new ApiError(0, TIMEOUT_MESSAGE)), REQUEST_TIMEOUT_MS)
    })
    let res: Response
    try {
      res = await Promise.race([pending, timeout])
    } catch (error) {
      throw error instanceof ApiError ? error : new ApiError(0, UNREACHABLE_MESSAGE)
    }
    return await Promise.race([readResponse<T>(res, path, options), timeout])
  } finally {
    clearTimeout(timer)
    end()
  }
}

// The first sign-in and first-run checks of this page load, shared while on
// their way: React's development double-run of effects (and any second
// caller at the same moment) gets the same answer instead of asking again.
let initialMe: Promise<CurrentUser> | null = null
let initialSetupStatus: Promise<{ needs_setup: boolean }> | null = null

/**
 * A small stale-while-revalidate cache for expensive, read-only browse
 * endpoints. Matching concurrent requests share one fetch; a recently
 * fetched value returns immediately when the user switches back to a tab.
 */
function cachedRequest<T>(
  path: string,
  cache: Map<string, BrowseCacheEntry>,
  maxAgeMs: number,
): Promise<T> {
  const entry = cache.get(path) ?? {}
  cache.set(path, entry)
  const isFresh =
    entry.value !== undefined &&
    entry.receivedAt !== undefined &&
    Date.now() - entry.receivedAt < maxAgeMs
  if (isFresh) return Promise.resolve(entry.value as T)

  if (!entry.inFlight) {
    const generation = browseCacheGeneration
    entry.inFlight = request<T>(path)
      .then((value) => {
        // Do not let a request started for a previous account repopulate the
        // cache after login or logout cleared it.
        if (generation === browseCacheGeneration) {
          entry.value = value
          entry.receivedAt = Date.now()
        }
        return value
      })
      .finally(() => {
        // Runs for a failure, a 30-second timeout or an abort as well, so a
        // request that died never leaves later callers awaiting a dead promise.
        entry.inFlight = undefined
      })
  }

  // Keep the old list on screen while a stale entry is refreshed. Attach a
  // rejection handler here because callers correctly do not await a
  // background refresh whose stale value was already returned.
  if (entry.value !== undefined) {
    void entry.inFlight.catch(() => {})
    return Promise.resolve(entry.value as T)
  }

  return entry.inFlight as Promise<T>
}

// Requests in flight by path, for sharedRequest.
const inFlightRequests = new Map<string, Promise<unknown>>()

/** A GET that callers asking at the same time share; unlike the caches
 * below, a call after it has answered asks again. */
function sharedRequest<T>(path: string): Promise<T> {
  let pending = inFlightRequests.get(path) as Promise<T> | undefined
  if (!pending) {
    pending = request<T>(path).finally(() => inFlightRequests.delete(path))
    inFlightRequests.set(path, pending)
  }
  return pending
}

function cachedBrowseRequest<T>(path: string): Promise<T> {
  return cachedRequest(path, browseCache, BROWSE_CACHE_MAX_AGE_MS)
}

function cachedHomeRequest<T>(path: string): Promise<T> {
  return cachedRequest(path, homeCache, HOME_CACHE_MAX_AGE_MS)
}

function prefetchBrowseRequest<T>(path: string) {
  // This is deliberately best-effort: a prefetch must never surface an
  // error or delay the tab the user is already reading.
  void cachedBrowseRequest<T>(path).catch(() => {})
}

/** One file a bulk change could not handle (a bulk removal gives no reason). */
export interface BulkFailure {
  id: number
  reason: string | null
}

export interface BulkTagResult {
  updated: number
  failed: BulkFailure[]
}

/** What the Admin activity log can be narrowed by. `day` is YYYY-MM-DD on the
 * viewer's own clock; the offset tells the server where that day starts. */
export interface ActivityFilters {
  kind?: string
  /** 'problems' is warnings and errors together. */
  level?: 'info' | 'warning' | 'error' | 'problems'
  day?: string
  tzOffsetMinutes: number
}

function activityParams(filters: ActivityFilters): URLSearchParams {
  const params = new URLSearchParams()
  if (filters.kind) params.set('kind', filters.kind)
  if (filters.level) params.set('level', filters.level)
  if (filters.day) params.set('day', filters.day)
  params.set('tz_offset_minutes', String(filters.tzOffsetMinutes))
  return params
}

export const api = {
  login: async (username: string, password: string) => {
    clearHomeCache()
    return request<CurrentUser>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    })
  },
  /** `initial`: the page's first check - picks up boot.js's request. */
  setupStatus: (options: { initial?: boolean } = {}) => {
    const ask = () => request<{ needs_setup: boolean }>('/api/setup/status')
    if (!options.initial) return ask()
    // Shared only while on its way: a later check (after a logout) asks again.
    return (initialSetupStatus ??= adoptBootRequest('setup', '/api/setup/status', {}, ask).finally(() => {
      initialSetupStatus = null
    }))
  },
  completeSetup: (code: string, username: string, password: string) =>
    request<CurrentUser>('/api/setup', {
      method: 'POST',
      body: JSON.stringify({ code, username, password }),
    }),
  logout: async () => {
    try {
      return await request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' })
    } finally {
      clearBrowseCache()
    }
  },
  /** `probe`: the first check on load, where a 401 just means signed out.
   * `initial`: that first check itself - picks up boot.js's request. */
  me: (options: { probe?: boolean; initial?: boolean } = {}) => {
    const requestOptions = { authProbe: options.probe === true }
    const ask = () => request<CurrentUser>('/api/auth/me', undefined, requestOptions)
    if (!options.initial) return ask()
    return (initialMe ??= adoptBootRequest('me', '/api/auth/me', requestOptions, ask).finally(() => {
      initialMe = null
    }))
  },
  setAccentColor: (accentColor: string) =>
    request<CurrentUser>('/api/auth/accent-color', {
      method: 'PUT',
      body: JSON.stringify({ accent_color: accentColor }),
    }),
  setAudioSettings: (patch: Partial<AudioSettings>) =>
    request<AudioSettings>('/api/auth/audio-settings', {
      method: 'PUT',
      body: JSON.stringify(patch),
    }),
  /** Signs this account out on every device, this one included. */
  logoutEverywhere: () => request<{ ok: boolean }>('/api/auth/logout-everywhere', { method: 'POST' }),
  getPrivacySettings: () => request<{ online_recommendations: boolean }>('/api/auth/privacy-settings'),
  setPrivacySettings: (settings: { online_recommendations: boolean }) =>
    request<{ online_recommendations: boolean }>('/api/auth/privacy-settings', {
      method: 'PUT',
      body: JSON.stringify(settings),
    }),
  changePassword: (currentPassword: string, newPassword: string) =>
    request<{ ok: boolean }>('/api/auth/password', {
      method: 'POST',
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    }),
  changeUsername: (currentPassword: string, newUsername: string) =>
    request<CurrentUser>('/api/auth/username', {
      method: 'POST',
      body: JSON.stringify({ current_password: currentPassword, new_username: newUsername }),
    }),

  listTracks: (
    params: {
      q?: string
      genre?: string
      tag?: string
      artist?: string
      artist_is_null?: boolean
      album?: string
      sort?: TrackSort
      direction?: 'asc' | 'desc'
      limit?: number
      offset?: number
    } = {},
  ) => {
    const qs = new URLSearchParams(
      Object.entries(params)
        .filter(([, v]) => v !== undefined && v !== '')
        .map(([k, v]) => [k, String(v)]),
    ).toString()
    return request<MediaItem[]>(`/api/library/tracks${qs ? `?${qs}` : ''}`)
  },
  listGenres: () => cachedBrowseRequest<string[]>('/api/library/genres'),
  getLabels: (
    target: { media_item_id: number } | { artist: string | null; album?: string | null },
  ) => {
    const params = new URLSearchParams()
    if ('media_item_id' in target) {
      params.set('media_item_id', String(target.media_item_id))
    } else {
      if (target.artist == null) params.set('artist_is_null', 'true')
      else params.set('artist', target.artist)
      if (target.album) params.set('album', target.album)
    }
    return request<Labels>(`/api/library/labels?${params}`)
  },
  homeListMixes: () => cachedHomeRequest<MixPlaylist[]>('/api/library/mixes'),
  listMixOptions: () => request<MixOption[]>('/api/library/mixes/options'),
  addSpecifiedMix: async (selection: {
    include: Pick<MixOption, 'kind' | 'name'>[]
    exclude: Pick<MixOption, 'kind' | 'name'>[]
  }) => {
    const result = await request<MixPlaylist[]>('/api/library/mixes/specified', {
      method: 'POST',
      body: JSON.stringify(selection),
    }, LONG)
    clearHomeCache()
    clearBrowseCache()
    return result
  },
  refreshMixes: async () => {
    const result = await request<MixPlaylist[]>('/api/library/mixes/refresh', { method: 'POST' }, LONG)
    // A refresh can delete disposable mix playlists. The Home snapshot owns
    // Recently played, so it must not retain a card for one of those deleted
    // playlist ids until the cache expires.
    clearHomeCache()
    clearBrowseCache()
    return result
  },
  listAlbums: (params: { q?: string; genre?: string; artist?: string; appears_on?: boolean; sort?: BrowseSort; direction?: 'asc' | 'desc'; limit?: number; offset?: number } = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params)
        .filter(([, v]) => v !== undefined && v !== '')
        .map(([k, v]) => [k, String(v)]),
    ).toString()
    return cachedBrowseRequest<Album[]>(`/api/library/albums${qs ? `?${qs}` : ''}`)
  },
  listArtists: (params: { q?: string; genre?: string; sort?: BrowseSort; direction?: 'asc' | 'desc'; limit?: number; offset?: number } = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params)
        .filter(([, v]) => v !== undefined && v !== '')
        .map(([k, v]) => [k, String(v)]),
    ).toString()
    return cachedBrowseRequest<Artist[]>(`/api/library/artists${qs ? `?${qs}` : ''}`)
  },
  // full=1 asks the server for the whole matching list at once (up to its
  // safety cap), ignoring pagination - so the alphabet jump bar can cover
  // every letter. Cached like any other browse request.
  listAlbumsFull: (params: { q?: string; genre?: string; artist?: string; appears_on?: boolean; sort?: BrowseSort; direction?: 'asc' | 'desc' } = {}) => {
    const qs = new URLSearchParams(
      Object.entries({ ...params, full: '1' })
        .filter(([, v]) => v !== undefined && v !== '')
        .map(([k, v]) => [k, String(v)]),
    ).toString()
    return cachedBrowseRequest<Album[]>(`/api/library/albums${qs ? `?${qs}` : ''}`)
  },
  listArtistsFull: (params: { q?: string; genre?: string; sort?: BrowseSort; direction?: 'asc' | 'desc' } = {}) => {
    const qs = new URLSearchParams(
      Object.entries({ ...params, full: '1' })
        .filter(([, v]) => v !== undefined && v !== '')
        .map(([k, v]) => [k, String(v)]),
    ).toString()
    return cachedBrowseRequest<Artist[]>(`/api/library/artists${qs ? `?${qs}` : ''}`)
  },
  artistSummaries: (names: string[]) => {
    const qs = new URLSearchParams(names.filter((n) => n !== '').map((n) => ['names', n])).toString()
    return cachedBrowseRequest<Artist[]>(`/api/library/artist-summaries${qs ? `?${qs}` : ''}`)
  },
  /** One album's list entry (0 or 1 of them), for an album page opened on
   * its own: no need for the whole album list. `artist` null is the album
   * with no artist. */
  albumSummaries: (artist: string | null, album: string) =>
    cachedBrowseRequest<Album[]>(`/api/library/album-summaries?${albumPreferenceQuery(artist, album)}`),
  // MusicBrainz/ListenBrainz/Wikipedia info. `refresh` asks the server to re-fetch instead
  // of answering from its cache.
  // These answer `pending: true` with what is stored while the server fetches
  // the rest; callers ask again (usePendingAsync).
  artistInfo: (name: string, refresh = false, signal?: AbortSignal) =>
    request<ArtistInfo>(`/api/library/artist-info?${new URLSearchParams({ name, ...(refresh ? { refresh: 'true' } : {}) })}`, { signal }, LONG),
  /** Cached like a browse list, but a `pending` answer is not kept: the next
   * ask goes to the server. */
  artistTopTracks: async (name: string, limit = 20) => {
    const path = `/api/library/artist-top-tracks?${new URLSearchParams({ name, limit: String(limit) })}`
    const result = await cachedBrowseRequest<ArtistTopTracks>(path)
    if (result.pending) browseCache.delete(path)
    return result
  },
  artistMatchCandidates: (name: string) =>
    request<ArtistMatchCandidate[]>(`/api/library/artist-match-candidates?${new URLSearchParams({ name })}`, undefined, LONG),
  setArtistMatch: (name: string, mbid: string | null) =>
    request<ArtistInfo>('/api/library/artist-match', {
      method: 'PUT',
      body: JSON.stringify({ name, mbid }),
    }, LONG),
  albumInfo: (artist: string | null, album: string, refresh = false, signal?: AbortSignal) => {
    const params = albumPreferenceQuery(artist, album)
    if (refresh) params.set('refresh', 'true')
    return request<AlbumInfo>(`/api/library/album-info?${params}`, { signal }, LONG)
  },
  setAlbumEdition: (artist: string | null, album: string, releaseMbid: string | null) =>
    request<AlbumInfo>('/api/library/album-edition', {
      method: 'PUT',
      body: JSON.stringify({ artist, album, release_mbid: releaseMbid }),
    }, LONG),
  trackInfo: (mediaItemId: number, refresh = false, options: { signal?: AbortSignal } = {}) => {
    const params = new URLSearchParams()
    if (refresh) params.set('refresh', 'true')
    const qs = params.toString()
    return request<TrackInfo>(`/api/library/media/${mediaItemId}/info${qs ? `?${qs}` : ''}`, { signal: options.signal }, LONG)
  },
  prefetchAlbumsFull: () => prefetchBrowseRequest<Album[]>('/api/library/albums?sort=alphabetical&direction=asc&full=1'),
  prefetchArtistsFull: () => prefetchBrowseRequest<Artist[]>('/api/library/artists?sort=alphabetical&direction=asc&full=1'),
  listAccessibleRoots: () => request<LibraryRootSummary[]>('/api/library/roots'),
  browse: (rootId: number, path: string) =>
    request<BrowseResult>(
      `/api/library/browse?root_id=${rootId}&path=${encodeURIComponent(path)}`,
    ),
  // Deliberately uncached: every typed query is new, and results should
  // reflect the library as it is now.
  librarySearch: (q: string, signal?: AbortSignal) =>
    request<LibrarySearchResult>(`/api/library/search?q=${encodeURIComponent(q)}`, { signal }),
  searchFiles: (rootId: number, query: string) =>
    request<FileSearchResult>(
      `/api/library/files/search?root_id=${rootId}&q=${encodeURIComponent(query)}`,
    ),
  ignoreLibraryFolder: (rootId: number, path: string) =>
    request<{ path: string }>(`/api/library/roots/${rootId}/ignored-folders`, {
      method: 'POST',
      body: JSON.stringify({ path }),
    }),
  restoreLibraryFolder: (rootId: number, path: string) =>
    request<{ ok: boolean }>(`/api/library/roots/${rootId}/ignored-folders`, {
      method: 'DELETE',
      body: JSON.stringify({ path }),
    }),
  getMediaByIds: async (ids: number[]): Promise<MediaItem[]> => {
    if (ids.length === 0) return []
    // With no connection the Android app answers from what it was told
    // before, and from its downloads (which are all that can play then).
    if (isOffline()) return offlineMediaItems(ids)
    try {
      const items = await request<MediaItem[]>(`/api/library/media?ids=${ids.join(',')}`)
      void rememberItems(items)
      return items
    } catch (error) {
      if (isNativeApp() && isNoAnswer(error)) return offlineMediaItems(ids)
      throw error
    }
  },
  listVideos: (params: { q?: string; dir?: string; limit?: number; offset?: number } = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params)
        .filter(([k, v]) => v !== undefined && (v !== '' || k === 'dir'))
        .map(([k, v]) => [k, String(v)]),
    ).toString()
    return request<MediaItem[]>(`/api/library/videos${qs ? `?${qs}` : ''}`)
  },
  listVideoFolders: (parent = '') =>
    request<VideoFolder[]>(`/api/library/videos/folders?parent=${encodeURIComponent(parent)}`),
  // Deliberately uncached: the Library info screen and the sidebar counts are
  // refreshed on their own rules (App.tsx), and the server caches the heavy parts.
  libraryStats: (signal?: AbortSignal) => request<LibraryStats>('/api/library/stats', { signal }),
  /** Just the sidebar counts (the same numbers as the stats' `nav_counts`).
   * A background call: its 401 is left to the next request the user's own
   * screen makes (see RequestOptions.background). */
  navCounts: (signal?: AbortSignal) =>
    request<LibraryStats['nav_counts']>('/api/library/stats/nav-counts', { signal }, { background: true }),
  // Split from the stats so a slow disk check never holds the page up: the
  // Library info screen asks for it after it is showing.
  libraryStorage: (signal?: AbortSignal) => request<LibraryStorage[]>('/api/library/stats/storage', { signal }),
  recentlyAdded: () => request<MediaItem[]>('/api/library/recently-added'),
  recentlyPlayed: () => request<MediaItem[]>('/api/library/recently-played'),
  recordPlaySource: async (source: PlaySource) => {
    // The listens of the list this names carry its kind (see playReports).
    notePlaySourceRecorded(source.kind)
    const result = await request<void>('/api/library/play-source', {
      method: 'POST',
      body: JSON.stringify(source),
    })
    clearHomeCache()
    return result
  },
  recentlyPlayedSources: () => request<PlaySourceEntry[]>('/api/library/recently-played-sources'),
  homeRecentlyPlayedSources: () =>
    cachedHomeRequest<PlaySourceEntry[]>('/api/library/recently-played-sources?limit=13'),

  setTrackPreference: (mediaItemId: number, state: PreferenceState) =>
    request<{ state: PreferenceState }>(`/api/media/${mediaItemId}/preference`, {
      method: 'PUT',
      body: JSON.stringify({ state }),
    }),
  getAlbumPreference: (artist: string | null, album: string) =>
    request<CollectionPreference>(`/api/preferences/album?${albumPreferenceQuery(artist, album)}`),
  setAlbumPreference: (artist: string | null, album: string, state: PreferenceState) =>
    request<CollectionPreference>(`/api/preferences/album?${albumPreferenceQuery(artist, album)}`, {
      method: 'PUT',
      body: JSON.stringify({ state }),
    }),
  getArtistPreference: (artist: string) =>
    request<CollectionPreference>(`/api/preferences/artist?${new URLSearchParams({ artist })}`),
  setArtistPreference: (artist: string, state: PreferenceState) =>
    request<CollectionPreference>(`/api/preferences/artist?${new URLSearchParams({ artist })}`, {
      method: 'PUT',
      body: JSON.stringify({ state }),
    }),
  /** A play listened to past the threshold (PlayerContext's listening
   * tracker); without `play`, a load-time report (plain videos). */
  // The body is required: when the play began, plus the queue row and the
  // listened time for a play counted after listening (a plain video sends
  // only `started_at`, when it loads).
  recordPlayed: (
    mediaItemId: number,
    play: { started_at: string; queue_item_id?: number | null; listened_seconds?: number },
  ) =>
    request<{ ok: boolean }>(`/api/stream/${mediaItemId}/played`, {
      method: 'POST',
      body: JSON.stringify(play),
    }),
  /** How one listen ended (see player/playReports). */
  recordListened: (mediaItemId: number, listen: components['schemas']['ListenedReport']) =>
    request<components['schemas']['ListenedOut']>(`/api/stream/${mediaItemId}/listened`, {
      method: 'POST',
      body: JSON.stringify(listen),
    }),
  /** The same for a page being closed: a keepalive request, which the
   * browser finishes after the page is gone (like sendBeacon, but a plain
   * JSON body). Not awaited; failures are left to the pending queue. */
  sendListenedOnUnload: (mediaItemId: number, listen: components['schemas']['ListenedReport']): void => {
    try {
      void fetch(`/api/stream/${mediaItemId}/listened`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(listen),
        keepalive: true,
      }).catch(() => {})
    } catch {
      // No keepalive support: the pending queue sends it after a reload.
    }
  },
  /** `pending`: not looked up yet - the server is fetching them; ask again
   * (see usePendingAsync). Not the same as "no lyrics". */
  getLyrics: (mediaItemId: number, signal?: AbortSignal) =>
    request<components['schemas']['LyricsOut']>(`/api/library/media/${mediaItemId}/lyrics`, { signal }),
  refreshDiscovery: async () => {
    const result = await request<{ seed_artists: string[]; similar_artists: string[]; tracks_added: number }>(
      '/api/library/discovery/refresh',
      { method: 'POST' }, LONG,
    )
    clearBrowseCache()
    return result
  },
  exploreDiscovery: () =>
    request<DiscoveryExplore>('/api/library/discovery/explore', { method: 'POST' }, LONG),
  homeGetDiscoveryState: () =>
    cachedHomeRequest<DiscoveryState>('/api/library/discovery/state').then((state) => {
      // Explore previews still being looked up (Deezer answers live in the
      // server's memory only): never keep that answer, so the next ask is fresh.
      if (state.explore_pending) homeCache.delete('/api/library/discovery/state')
      return state
    }),
  getDiscoverySettings: () => request<DiscoverySettings>('/api/library/discovery/settings'),
  updateDiscoverySettings: (settings: DiscoverySettings) =>
    request<DiscoverySettings>('/api/library/discovery/settings', {
      method: 'PUT',
      body: JSON.stringify(settings),
    }),
  generatePlaylistFromTrack: async (mediaItemId: number) => {
    const result = await request<GeneratePlaylistResult>('/api/library/discovery/generate', {
      method: 'POST',
      body: JSON.stringify({ media_item_id: mediaItemId }),
    }, LONG)
    clearBrowseCache()
    return result
  },
  generatePlaylistFromCollection: async (body: { label: string; artist?: string; album?: string }) => {
    const result = await request<GeneratePlaylistResult>('/api/library/discovery/generate-collection', {
      method: 'POST',
      body: JSON.stringify(body),
    }, LONG)
    clearBrowseCache()
    return result
  },

  /** Concurrent callers share one request (App's list, Home's Discovery
   * look-up and a refresh right after it are often asked together). */
  listPlaylists: () => sharedRequest<Playlist[]>('/api/playlists'),
  homeListPlaylists: () => cachedHomeRequest<Playlist[]>('/api/playlists'),
  createPlaylist: async (name: string) => {
    const result = await request<Playlist>('/api/playlists', {
      method: 'POST',
      body: JSON.stringify({ name }),
    })
    clearBrowseCache()
    return result
  },
  updatePlaylist: async (id: number, fields: { name?: string; description?: string | null }) => {
    const result = await request<PlaylistDetail>(`/api/playlists/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(fields),
    })
    clearBrowseCache()
    return result
  },
  getPlaylist: (id: number) => request<PlaylistDetail>(`/api/playlists/${id}`),
  homeGetPlaylist: (id: number) => cachedHomeRequest<PlaylistDetail>(`/api/playlists/${id}`),
  addMoreTracks: async (id: number) => {
    const result = await request<PlaylistDetail>(`/api/playlists/${id}/add-more`, { method: 'POST' }, LONG)
    clearBrowseCache()
    return result
  },
  deletePlaylist: async (id: number) => {
    const result = await request<void>(`/api/playlists/${id}`, { method: 'DELETE' })
    clearBrowseCache()
    return result
  },
  addToPlaylist: async (playlistId: number, mediaItemIds: number[], position?: number) => {
    const result = await request<PlaylistDetail>(`/api/playlists/${playlistId}/items`, {
      method: 'POST',
      body: JSON.stringify({ media_item_ids: mediaItemIds, position }),
    })
    clearBrowseCache()
    return result
  },
  removeFromPlaylist: async (playlistId: number, playlistItemId: number) => {
    const result = await request<{ ok: boolean }>(`/api/playlists/${playlistId}/items/${playlistItemId}`, {
      method: 'DELETE',
    })
    clearBrowseCache()
    return result
  },
  /** `orderedItemIds` are the playlist entries (`playlist_item_id`) as the
   * detail shows them, in the new order. */
  reorderPlaylist: async (id: number, orderedItemIds: number[]) => {
    const result = await request<PlaylistDetail>(`/api/playlists/${id}/reorder`, {
      method: 'PUT',
      body: JSON.stringify({ ordered_item_ids: orderedItemIds }),
    })
    clearBrowseCache()
    return result
  },
  savePlaylistCopy: async (playlistId: number, name?: string) => {
    const result = await request<Playlist>(`/api/playlists/${playlistId}/save-copy`, {
      method: 'POST',
      body: JSON.stringify({ name }),
    }, LONG)
    clearBrowseCache()
    return result
  },

  streamUrl: (mediaItemId: number, maxBitrateKbps?: number | null) =>
    maxBitrateKbps ? `/api/stream/${mediaItemId}?max_bitrate=${maxBitrateKbps}` : `/api/stream/${mediaItemId}`,
  // How to load a media item: straight from the stream URL, or through HLS
  // when the browser can't decode the file's codecs (see server app/hls.py).
  playbackInfo: (mediaItemId: number) =>
    request<{ mode: 'direct' | 'hls'; url: string }>(`/api/stream/${mediaItemId}/playback`),
  // Under the art epoch, so a rescan that changed art changes the address.
  thumbnailUrl: (mediaItemId: number) => withArtEpoch(`/api/stream/${mediaItemId}/thumbnail`),
  artUrl: (mediaItemId: number) => withArtEpoch(`/api/stream/${mediaItemId}/art`),
  artFullUrl: (mediaItemId: number) => withArtEpoch(`/api/stream/${mediaItemId}/art/full`),

  // --- Admin ---
  adminListUsers: () => request<AdminUser[]>('/api/admin/users'),
  adminCreateUser: (username: string, password: string, isAdmin: boolean) =>
    request<AdminUser>('/api/admin/users', {
      method: 'POST',
      body: JSON.stringify({ username, password, is_admin: isAdmin }),
    }),
  adminDeleteUser: (id: number) =>
    request<{ ok: boolean }>(`/api/admin/users/${id}`, { method: 'DELETE' }),
  adminUpdateUser: (id: number, isAdmin: boolean) =>
    request<AdminUser>(`/api/admin/users/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ is_admin: isAdmin }),
    }),
  adminResetUserPassword: (id: number, newPassword: string) =>
    request<{ ok: boolean }>(`/api/admin/users/${id}/password`, {
      method: 'POST',
      body: JSON.stringify({ new_password: newPassword }),
    }),

  adminGetArtistSplitRules: () => request<ArtistSplitRules>('/api/admin/artist-split-rules'),
  adminPutArtistSplitRules: async (rules: ArtistSplitRules) => {
    const result = await request<ArtistSplitRules & { relinked: number }>('/api/admin/artist-split-rules', {
      method: 'PUT',
      body: JSON.stringify(rules),
    }, LONG)
    clearBrowseCache()
    return result
  },

  adminListRoots: () => request<LibraryRoot[]>('/api/admin/roots'),
  adminCreateRoot: (path: string, displayName: string, isShared: boolean) =>
    request<LibraryRoot>('/api/admin/roots', {
      method: 'POST',
      body: JSON.stringify({ path, display_name: displayName, is_shared: isShared }),
    }, LONG),
  adminUpdateRoot: (
    id: number,
    changes: { path?: string; display_name?: string; is_shared?: boolean },
  ) =>
    request<LibraryRoot>(`/api/admin/roots/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(changes),
    }, LONG),
  adminDeleteRoot: (id: number) =>
    request<{ ok: boolean }>(`/api/admin/roots/${id}`, { method: 'DELETE' }, LONG),
  adminScanRoot: async (id: number, mode: ScanMode = 'normal') => {
    const result = await request<LibraryTask>(`/api/admin/roots/${id}/scan?mode=${mode}`, { method: 'POST' }, LONG)
    clearBrowseCache()
    return result
  },
  adminEnrichRoot: (id: number, mode: 'mbids' | 'musicbrainz_missing' | 'isrc_mbid' | 'listenbrainz_missing' | 'listenbrainz_all' | 'musicbrainz_fallback' | 'artist_info' = 'musicbrainz_missing') =>
    request<LibraryTask>(`/api/admin/roots/${id}/enrich`, {
      method: 'POST',
      body: JSON.stringify({ mode }),
    }, LONG),
  adminFullSetupNew: (id: number) =>
    request<LibraryTask>(`/api/admin/roots/${id}/full-setup-new`, { method: 'POST' }, LONG),
  adminFullSetup: (id: number) =>
    request<LibraryTask>(`/api/admin/roots/${id}/full-setup`, { method: 'POST' }, LONG),
  /** The credit stored with an entity's own art (a Wikimedia Commons photo); null when none. */
  getArtCredit: (type: string, key: string | number) =>
    request<ImageCredit | null>(`/api/art/${type}/${encodeURIComponent(String(key))}/credit`),
  getCatalogArtCredit: (kind: 'artist' | 'album', mbid: string) =>
    request<ImageCredit | null>(`/api/catalog/art/${kind}/${mbid}/credit`),
  adminUnrecordedArt: () => request<UnrecordedArt>('/api/admin/custom-art/unrecorded'),
  adminRemoveUnrecordedArt: () =>
    request<{ removed: number }>('/api/admin/custom-art/unrecorded/remove', { method: 'POST' }),
  dismissNotice: (notice: string) =>
    request<CurrentUser>('/api/auth/dismissed-notices', { method: 'POST', body: JSON.stringify({ notice }) }),
  adminBackgroundStatus: () => request<BackgroundStatus>('/api/admin/background-status'),
  adminActivity: (filters: ActivityFilters, page: { beforeId?: number; limit?: number } = {}, signal?: AbortSignal) => {
    const params = activityParams(filters)
    if (page.beforeId != null) params.set('before_id', String(page.beforeId))
    params.set('limit', String(page.limit ?? 100))
    return request<ActivityLog>(`/api/admin/activity?${params}`, { signal })
  },
  /** Where the text download of the log lives (same filters, no paging): a
   * plain link, so the browser saves the attachment itself. */
  adminActivityDownloadUrl: (filters: ActivityFilters) => `/api/admin/activity/download?${activityParams(filters)}`,
  adminStopTask: (rootId: number, taskId: number) =>
    request<LibraryTask>(`/api/admin/roots/${rootId}/tasks/${taskId}/stop`, { method: 'POST' }),
  adminEditTags: async (id: number, fields: TagEdit) => {
    const result = await request<{ ok: boolean }>(`/api/admin/media/${id}/tags`, {
      method: 'PATCH',
      body: JSON.stringify(fields),
    }, LONG)
    clearBrowseCache()
    return result
  },
  adminBulkEditTags: async (ids: number[], fields: TagEdit): Promise<BulkTagResult> => {
    const result = await request<components['schemas']['BulkTagEditOut']>('/api/admin/media/bulk-tags', {
      method: 'POST',
      body: JSON.stringify({ media_item_ids: ids, ...fields }),
    }, LONG)
    clearBrowseCache()
    return { updated: result.updated, failed: result.failed }
  },
  adminBulkRemove: async (ids: number[]): Promise<{ removed: number[]; failed: BulkFailure[] }> => {
    const result = await request<components['schemas']['BulkRemoveOut']>('/api/admin/media/bulk-remove', {
      method: 'POST',
      body: JSON.stringify({ media_item_ids: ids }),
    }, LONG)
    clearBrowseCache()
    return { removed: result.removed, failed: result.failed.map((id) => ({ id, reason: null })) }
  },
  adminGrantAccess: (rootId: number, userId: number) =>
    request<{ ok: boolean }>(`/api/admin/roots/${rootId}/access/${userId}`, { method: 'POST' }),
  adminRevokeAccess: (rootId: number, userId: number) =>
    request<{ ok: boolean }>(`/api/admin/roots/${rootId}/access/${userId}`, { method: 'DELETE' }),

  // --- Integrations (scrobbling) ---
  getIntegrations: () => request<Integration[]>('/api/integrations'),
  connectListenBrainz: (token: string) =>
    request<{ ok: boolean }>('/api/integrations/listenbrainz', {
      method: 'POST',
      body: JSON.stringify({ token }),
    }, LONG),
  disconnectListenBrainz: () =>
    request<{ ok: boolean }>('/api/integrations/listenbrainz', { method: 'DELETE' }),

  // --- Radio ---
  listStations: () => request<RadioStation[]>('/api/radio/stations'),
  homeListStations: () => cachedHomeRequest<RadioStation[]>('/api/radio/stations'),
  createStation: async (name: string, streamUrl: string, isShared: boolean, description?: string) => {
    const result = await request<RadioStation>('/api/radio/stations', {
      method: 'POST',
      body: JSON.stringify({
        name,
        stream_url: streamUrl,
        is_shared: isShared,
        description: description || undefined,
      }),
    }, LONG)
    noteLibraryChange()
    return result
  },
  deleteStation: async (id: number) => {
    const result = await request<{ ok: boolean }>(`/api/radio/stations/${id}`, { method: 'DELETE' })
    noteLibraryChange()
    return result
  },
  setStationFavorite: (id: number, favorite: boolean) =>
    request<{ is_favorite: boolean }>(`/api/radio/stations/${id}/favorite`, {
      method: favorite ? 'PUT' : 'DELETE',
    }),
  stationStreamUrl: (id: number) => `/api/radio/stations/${id}/stream`,
  getNowPlaying: (id: number) =>
    request<{ title: string | null; updated_at: number | null }>(
      `/api/radio/stations/${id}/now-playing`,
    ),

  addWantListItem: (artist: string, title: string, stationName: string | null) =>
    request<WantListItem>('/api/radio/want-list', {
      method: 'POST',
      body: JSON.stringify({ artist, title, station_name: stationName }),
    }, LONG),
}
