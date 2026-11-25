import type { BrowseSort, TrackSort } from './types'

export type BrowseViewMode = 'grid' | 'list'
export type BrowseDirection = 'asc' | 'desc'

const BROWSE_SORTS: BrowseSort[] = [
  'alphabetical',
  'date_added',
  'last_played',
  'most_played',
  'date_published',
]

// What each sort means when it is first picked - the server's own default
// per sort (A-Z for the names; newest / most played / last played first for
// the rest). `desc` is always the literal "biggest / newest first" order on
// the server, so the arrow shown (down for desc) matches the list.
export const DEFAULT_SORT_DIRECTION: Record<BrowseSort, BrowseDirection> = {
  alphabetical: 'asc',
  date_added: 'desc',
  last_played: 'desc',
  most_played: 'desc',
  date_published: 'desc',
}

export interface BrowsePrefs {
  sort: BrowseSort
  direction: BrowseDirection
  viewMode: BrowseViewMode
}

// Stored prefs without a `v` can hold a `desc` for "Most played" / "Last
// played" that stood for the opposite order (least played / oldest first).
// Reading such a stored direction literally would give an arrow the user never
// chose, so for these two sorts it is dropped once and the sort starts from its
// default (`desc` = most played / most recent first). Other sorts keep their
// stored direction.
const PREFS_VERSION = 2

const DEFAULTS: BrowsePrefs = { sort: 'alphabetical', direction: 'asc', viewMode: 'grid' }

function storageKey(context: string): string {
  return `atriolyric.browse.${context}`
}

// Per-tab (Albums vs Artists get their own key) - a device preference like
// theme/tile size, so it's kept in localStorage rather than synced to the
// account, and survives the component remounting (navigating away and back,
// or a page reload).
export function loadBrowsePrefs(context: string): BrowsePrefs {
  try {
    const raw = localStorage.getItem(storageKey(context))
    if (!raw) return DEFAULTS
    const parsed = JSON.parse(raw)
    const sort: BrowseSort = BROWSE_SORTS.includes(parsed.sort) ? parsed.sort : DEFAULTS.sort
    return {
      sort,
      direction: (parsed.direction === 'asc' || parsed.direction === 'desc') &&
        (parsed.v === PREFS_VERSION || (sort !== 'most_played' && sort !== 'last_played'))
        ? parsed.direction
        : DEFAULT_SORT_DIRECTION[sort],
      viewMode: parsed.viewMode === 'list' ? 'list' : 'grid',
    }
  } catch {
    return DEFAULTS
  }
}

export function saveBrowsePrefs(context: string, prefs: BrowsePrefs) {
  try {
    localStorage.setItem(storageKey(context), JSON.stringify({ ...prefs, v: PREFS_VERSION }))
  } catch {
    // Storage can be unavailable (private browsing, quota) - the
    // preference just won't persist across reloads, not worth surfacing.
  }
}

export type SongsViewMode = 'table' | 'cards'

const SONGS_KEY = 'atriolyric.browse.songs'

export const SONGS_SORTS: TrackSort[] = [
  'artist',
  'title',
  'album',
  'year',
  'date_added',
  'most_played',
  'last_played',
]

// What a Songs sort means when it is first picked - the server's own default
// per sort (app.library_query.DEFAULT_TRACK_DIRECTION): A-Z for the names,
// newest / most first for the rest.
export const DEFAULT_SONGS_DIRECTION: Record<TrackSort, BrowseDirection> = {
  artist: 'asc',
  title: 'asc',
  album: 'asc',
  year: 'desc',
  date_added: 'desc',
  most_played: 'desc',
  last_played: 'desc',
}

export interface SongsSort {
  sort: TrackSort
  direction: BrowseDirection
}

/** The order the server lists songs in when asked for nothing else. */
export const DEFAULT_SONGS_SORT: SongsSort = { sort: 'artist', direction: 'asc' }

function readSongsPrefs(): Record<string, unknown> {
  try {
    const parsed = JSON.parse(localStorage.getItem(SONGS_KEY) ?? 'null')
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function writeSongsPrefs(fields: Record<string, unknown>) {
  try {
    localStorage.setItem(SONGS_KEY, JSON.stringify({ ...readSongsPrefs(), ...fields }))
  } catch {
    // Not persisted when storage is unavailable; the choice still applies now.
  }
}

/** The Songs tab's Table / Cards choice - per device, like the Albums and
 * Artists grid/list choice above. */
export function loadSongsViewMode(): SongsViewMode {
  return readSongsPrefs().viewMode === 'cards' ? 'cards' : 'table'
}

export function saveSongsViewMode(viewMode: SongsViewMode) {
  writeSongsPrefs({ viewMode })
}

/** The Songs tab's sort - per device, kept beside its view mode. */
export function loadSongsSort(): SongsSort {
  const prefs = readSongsPrefs()
  const sort = SONGS_SORTS.find((s) => s === prefs.sort)
  if (!sort) return DEFAULT_SONGS_SORT
  const direction = prefs.direction === 'asc' || prefs.direction === 'desc' ? prefs.direction : DEFAULT_SONGS_DIRECTION[sort]
  return { sort, direction }
}

export function saveSongsSort(value: SongsSort) {
  writeSongsPrefs({ sort: value.sort, direction: value.direction })
}
