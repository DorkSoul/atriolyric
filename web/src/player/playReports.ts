import type { components } from '../api-types'

// --- Play reports -------------------------------------------------------------
// A play counts - for history and ListenBrainz - once it has been listened to for
// half its length or four minutes, whichever comes first (for a track of 30
// seconds or less: half of it, or played to the end; the server never
// scrobbles those). Listening is measured on the device producing the sound,
// from `timeupdate` deltas, per queue item (see `noteListening`).
export const PLAY_REPORT_MAX_SECONDS = 240
export const SHORT_TRACK_SECONDS = 30
// A forward step of the clock larger than this between two timeupdates is a
// seek (or a jump after a stall), not listening.
export const LISTEN_MAX_STEP_SECONDS = 2
// Reports the server did not confirm (offline, 5xx) are retried, also after
// a reload of this tab; at most this many, for at most 14 days.
export const PENDING_PLAYS_KEY = 'atriolyric.pending-plays'
export const PENDING_PLAYS_MAX = 50
export const PENDING_PLAY_MAX_AGE_MS = 14 * 24 * 3600_000
export const PENDING_PLAY_RETRY_BASE_MS = 5_000
export const PENDING_PLAY_RETRY_MAX_MS = 5 * 60_000

/** How long a play must have been listened to before it counts. */
export function playThresholdSeconds(duration: number | null): number {
  if (duration == null || !Number.isFinite(duration) || duration <= 0) return PLAY_REPORT_MAX_SECONDS
  return Math.min(duration / 2, PLAY_REPORT_MAX_SECONDS)
}

export interface PendingPlay {
  media_item_id: number
  started_at: string
  queue_item_id: number | null
  listened_seconds: number
  attempts: number
  next_at: number
}

function readPending<T extends { media_item_id: number; started_at: string }>(key: string): T[] {
  try {
    const parsed: unknown = JSON.parse(window.sessionStorage.getItem(key) ?? '[]')
    return Array.isArray(parsed)
      ? parsed.filter((entry): entry is T => (
        typeof entry?.media_item_id === 'number' && typeof entry?.started_at === 'string'
      ))
      : []
  } catch {
    return []
  }
}

function writePending(key: string, entries: unknown[]): void {
  try {
    if (entries.length) window.sessionStorage.setItem(key, JSON.stringify(entries))
    else window.sessionStorage.removeItem(key)
  } catch {
    // Storage denied: the in-memory list still retries while the tab lives.
  }
}

export function readPendingPlays(): PendingPlay[] {
  return readPending<PendingPlay>(PENDING_PLAYS_KEY)
}

export function writePendingPlays(entries: PendingPlay[]): void {
  writePending(PENDING_PLAYS_KEY, entries)
}

// --- Listen outcomes ----------------------------------------------------------
// How each listen ended (server: app.listening), reported once by the device
// producing the sound when it leaves a track or music video - also one that
// never counted as a play. `started_at` is the play report's, so the two join.
export type ListenedReport = components['schemas']['ListenedReport']
export type ListenEndReason = ListenedReport['end_reason']
export type ListenStartKind = NonNullable<ListenedReport['start_kind']>
export type ListenSourceKind = NonNullable<ListenedReport['source_kind']>

// A listen left with less than this heard is not reported (an error aside):
// that is scanning past items - Next, Next, Next, or a list picked from
// while the previous track was still loading - not a judgement of them.
// It also leaves out the instant between a source loading and the user's
// next tap, where nothing was really heard.
export const LISTEN_OUTCOME_MIN_SECONDS = 2
// Outcomes waiting for the server, like pending plays (same retries and age).
export const PENDING_LISTENS_KEY = 'atriolyric.pending-listens'
export const PENDING_LISTENS_MAX = 100

export type PendingListen = ListenedReport & {
  media_item_id: number
  attempts: number
  next_at: number
}

export function readPendingListens(): PendingListen[] {
  return readPending<PendingListen>(PENDING_LISTENS_KEY)
}

export function writePendingListens(entries: PendingListen[]): void {
  writePending(PENDING_LISTENS_KEY, entries)
}

/** What the next listen was started by, noted by the command that starts it
 * (a user pick, Next, a natural end) and taken by the first new listen of
 * `mediaId` (any item when null) within LISTEN_START_HINT_MAX_AGE_MS.
 * `newQueue`: the pick started a new list, whose play source (see
 * notePlaySourceRecorded) the listens in it carry. */
export interface ListenStartHint {
  kind: ListenStartKind
  mediaId: number | null
  newQueue: boolean
  at: number
}
export const LISTEN_START_HINT_MAX_AGE_MS = 30_000

export function listenStartHint(kind: ListenStartKind, mediaId: number | null = null, newQueue = false): ListenStartHint {
  return { kind, mediaId, newQueue, at: Date.now() }
}

// The play source the views record when the user starts a list (album,
// artist, mix, playlist, the Songs tab: api.recordPlaySource), and when.
// Views record it right before or after starting the list, so a list
// started within PLAY_SOURCE_PAIRING_MS of it is the one it names.
const LISTEN_SOURCE_KINDS: readonly string[] = ['album', 'artist', 'mix', 'playlist', 'library'] satisfies ListenSourceKind[]
const PLAY_SOURCE_PAIRING_MS = 2_000
let lastPlaySource: { kind: ListenSourceKind; at: number } | null = null

export function notePlaySourceRecorded(kind: string): void {
  lastPlaySource = LISTEN_SOURCE_KINDS.includes(kind) ? { kind: kind as ListenSourceKind, at: Date.now() } : null
}

/** The play source recorded with a list started at `startedAt`, if any. */
export function playSourceKindOfQueue(startedAt: number | null): ListenSourceKind | null {
  if (startedAt == null || lastPlaySource == null) return null
  return Math.abs(lastPlaySource.at - startedAt) <= PLAY_SOURCE_PAIRING_MS ? lastPlaySource.kind : null
}
