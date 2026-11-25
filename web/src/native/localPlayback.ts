// Playback in the Android app while its server cannot be reached.
//
// The play queue lives on the server and the player follows it over the
// playback socket (docs/playback-protocol.md). With no connection there is no
// server to ask, so the socket is replaced by this stand-in: it takes the
// same messages and answers with the same snapshots, for one device (this
// one) and for the songs it can play with no network: downloads, and the
// ones the app still holds from streaming (a lift or a tunnel should not stop
// the music while those last). To the player it is a playback socket
// like any other, so the queue, shuffle, repeat, Next and the notification
// all work as they do online.
//
// What it holds is kept on the device: the last state the server sent while
// it could be reached, and from then on whatever is played offline. When the
// server is back, a queue that was changed offline is handed to it (see
// `handback`), so the server carries on from where this device is.

import type { MediaItem, PlaybackStateSnapshot } from '../types'
import { nativeCall } from './bridge'
import { downloadOf, downloadsReady, downloadsSnapshot, offlineMediaItems } from './downloadStore'
import { nativeHeldTrackIds } from './nativeAudio'

/** This device's id while it is its own server. Not a row id of any server. */
export const LOCAL_DEVICE_ID = 2_000_000_001
const FIRST_LOCAL_ROW_ID = 1_500_000_000
const STORAGE_KEY = 'atriolyric.localPlayback'

const KEEPS_QUEUE = new Set(['play', 'pause', 'seek', 'position_report', 'set_repeat'])

export const NOT_DOWNLOADED_MESSAGE = 'Not downloaded. With no connection only downloads play.'

interface Row {
  /** The queue row id: the server's while its queue is carried on, else our own. */
  qid: number
  id: number
  /** The order the rows were queued in, which turning shuffle off goes back to. */
  seq: number
}

interface LocalState {
  rows: Row[]
  index: number
  position: number
  playing: boolean
  shuffled: boolean
  repeat: 'off' | 'all' | 'one'
  revision: number
  nextRowId: number
  /** Changed by a command given offline: the server has not heard of it. */
  dirty: boolean
}

function emptyState(): LocalState {
  return {
    rows: [], index: 0, position: 0, playing: false, shuffled: false, repeat: 'off',
    revision: 1, nextRowId: FIRST_LOCAL_ROW_ID, dirty: false,
  }
}

function readState(): LocalState {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? 'null') as Partial<LocalState> | null
    if (saved && Array.isArray(saved.rows)) {
      const state = { ...emptyState(), ...saved }
      state.rows = saved.rows.filter((row) => Number.isInteger(row?.qid) && Number.isInteger(row?.id))
      state.index = Math.max(0, Math.min(Number(state.index) || 0, Math.max(0, state.rows.length - 1)))
      return state
    }
  } catch {
    // Unreadable: start empty.
  }
  return emptyState()
}

let state: LocalState | null = null

function current(): LocalState {
  state ??= readState()
  return state
}

function save() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(current()))
  } catch {
    // Kept for this visit only.
  }
}

/** The server's state, taken over each time it says something: what an
 * offline start carries on from. Never over a queue changed offline that the
 * server has not been given yet. */
export function noteServerState(snapshot: PlaybackStateSnapshot): void {
  const local = current()
  if (local.dirty) return
  state = {
    ...local,
    rows: snapshot.queue_media_item_ids.map((id, position) => ({
      qid: snapshot.queue_item_ids[position] ?? FIRST_LOCAL_ROW_ID + position,
      id,
      seq: position,
    })),
    index: Math.max(0, snapshot.queue_index),
    position: snapshot.position_seconds,
    playing: snapshot.is_playing,
    shuffled: snapshot.is_shuffled,
    repeat: snapshot.repeat_mode,
  }
  save()
}

export interface Handback {
  ids: number[]
  startId: number
  startIndex: number
  position: number
  paused: boolean
  shuffled: boolean
  repeat: 'off' | 'all' | 'one'
}

/** What was played offline and the server has not heard of, as the queue to
 * give it; null when nothing was changed offline. */
export function handback(): Handback | null {
  const local = current()
  const row = local.rows[local.index]
  if (!local.dirty || !row) return null
  return {
    ids: local.rows.map((r) => r.id),
    startId: row.id,
    startIndex: local.index,
    position: local.position,
    paused: !local.playing,
    shuffled: local.shuffled,
    repeat: local.repeat,
  }
}

/** Another account signs in, or this one signs out: the queue kept for an
 * offline start, and a queue played offline that the server never heard of,
 * belong to that account - handed back, it would become the next account's
 * queue. */
export function forgetLocalPlayback(): void {
  state = null
  try {
    localStorage.removeItem(STORAGE_KEY)
  } catch {
    // Nothing kept to forget.
  }
}

/** The server has the queue now (or it is not wanted any more). */
export function clearHandback(): void {
  current().dirty = false
  save()
}

/** Whether a track can play with no connection: a downloaded song, or one
 * the app holds from streaming it (the playing and next tracks, and the ones
 * held ahead of the queue), so a short loss of signal does not stop the music. */
function isPlayable(id: number): boolean {
  const download = downloadOf(id)
  if (download?.state === 'completed') return download.kind === 'track'
  return nativeHeldTrackIds().has(id)
}

function shuffle<T>(items: T[]): T[] {
  const out = [...items]
  for (let i = out.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1))
    ;[out[i], out[j]] = [out[j], out[i]]
  }
  return out
}

function sameText(a: string | null | undefined, b: string): boolean {
  return (a ?? '').localeCompare(b, undefined, { sensitivity: 'accent' }) === 0
}

interface LibraryFilters {
  q?: string | null
  genre?: string | null
  artist?: string | null
  album?: string | null
  artist_is_null?: boolean | null
}

/** The downloaded songs a library filter (the Songs tab, an artist, an album)
 * matches, in artist, album and track order. A tag filter cannot be told
 * from here and is left out. */
function matchesLibrary(item: MediaItem, filters: LibraryFilters): boolean {
  if (filters.artist_is_null && item.artist) return false
  if (filters.artist && !sameText(item.artist, filters.artist) && !sameText(item.album_artist, filters.artist) &&
      !(item.artists ?? []).some((credit) => sameText(credit.name, filters.artist!))) return false
  if (filters.album && !sameText(item.album, filters.album)) return false
  if (filters.genre && !(item.genre ?? '').toLowerCase().includes(filters.genre.toLowerCase())) return false
  if (filters.q) {
    const q = filters.q.toLowerCase()
    if (![item.title, item.artist, item.album].some((text) => (text ?? '').toLowerCase().includes(q))) return false
  }
  return true
}

async function libraryIds(filters: LibraryFilters): Promise<number[]> {
  const ids = downloadsSnapshot().items.filter((d) => d.state === 'completed' && d.kind === 'track').map((d) => d.mediaId)
  const items = (await offlineMediaItems(ids)).filter((item) => matchesLibrary(item, filters))
  const text = (value: string | null | undefined) => value ?? ''
  items.sort((a, b) =>
    text(a.artist).localeCompare(text(b.artist)) ||
    text(a.album).localeCompare(text(b.album)) ||
    (a.track_number ?? 0) - (b.track_number ?? 0) ||
    a.title.localeCompare(b.title))
  return items.map((item) => item.id)
}

type Message = Record<string, unknown>

/** A playback socket answered on the device. Shaped like the WebSocket the
 * player opens (usePlaybackSocket.ts): the same fields, callbacks and frames. */
export class LocalPlaybackSocket {
  readyState: number = WebSocket.CONNECTING
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  /** Messages are answered one at a time, in the order they were sent. */
  private work: Promise<void> = Promise.resolve()

  constructor() {
    window.setTimeout(() => {
      if (this.readyState !== WebSocket.CONNECTING) return
      this.readyState = WebSocket.OPEN
      this.onopen?.(new Event('open'))
    }, 0)
  }

  send(text: string): void {
    if (this.readyState !== WebSocket.OPEN) return
    let message: Message
    try {
      message = JSON.parse(text) as Message
    } catch {
      return
    }
    this.work = this.work.then(() => this.handle(message)).catch(() => {})
  }

  close(code = 1000, reason = ''): void {
    if (this.readyState === WebSocket.CLOSING || this.readyState === WebSocket.CLOSED) return
    this.readyState = WebSocket.CLOSED
    window.setTimeout(() => this.onclose?.({ code, reason, wasClean: true } as CloseEvent), 0)
  }

  private emit(message: Message) {
    if (this.readyState !== WebSocket.OPEN) return
    this.onmessage?.({ data: JSON.stringify(message) } as MessageEvent)
  }

  private error(message: string) {
    this.emit({ type: 'error', message })
  }

  private snapshot(): PlaybackStateSnapshot {
    const local = current()
    const row = local.rows[local.index]
    return {
      media_item_id: row?.id ?? null,
      position_seconds: local.position,
      is_playing: row ? local.playing : false,
      active_device_id: LOCAL_DEVICE_ID,
      queue_index: row ? local.index : 0,
      queue_media_item_ids: local.rows.map((r) => r.id),
      queue_item_ids: local.rows.map((r) => r.qid),
      is_shuffled: local.shuffled,
      repeat_mode: local.repeat,
      queue_context_count: local.rows.length,
      devices: [{
        id: LOCAL_DEVICE_ID,
        name: 'This device',
        device_type: 'mobile',
        is_active: true,
        is_connected: true,
        last_seen_at: new Date().toISOString().replace('Z', ''),
      }],
      revision: local.revision,
      current_queue_item_id: row?.qid ?? null,
    } as PlaybackStateSnapshot
  }

  /** The state after a change, to this device (there is no other). */
  private broadcast(command: string | null, positionChanged = false, changed = true) {
    const local = current()
    if (changed) {
      local.revision += 1
      // Pausing or seeking in the track the server already has is not a new queue.
      if (command && !KEEPS_QUEUE.has(command)) local.dirty = true
    }
    save()
    this.emit({
      type: 'state',
      state: this.snapshot(),
      position_changed: positionChanged,
      source_device_id: command ? LOCAL_DEVICE_ID : null,
      source_command: command,
    })
  }

  private newRows(ids: number[]): Row[] {
    const local = current()
    const first = local.rows.reduce((most, row) => Math.max(most, row.seq), -1) + 1
    return ids.map((id, offset) => ({ qid: local.nextRowId++, id, seq: first + offset }))
  }

  private step(from: number, direction: 1 | -1): number | null {
    const { rows } = current()
    for (let i = from + direction; i >= 0 && i < rows.length; i += direction) {
      if (isPlayable(rows[i].id)) return i
    }
    return null
  }

  private next() {
    const local = current()
    let target = this.step(local.index, 1)
    if (target == null && local.repeat === 'all') target = this.step(-1, 1)
    local.position = 0
    if (target == null) local.playing = false
    else local.index = target
  }

  private keepCurrent(change: () => void) {
    const local = current()
    const playing = local.rows[local.index]?.qid
    change()
    const index = local.rows.findIndex((row) => row.qid === playing)
    if (index >= 0) local.index = index
  }

  private async setQueue(message: Message) {
    const local = current()
    const context = message.context as { kind?: string; ids?: unknown; filters?: LibraryFilters } | undefined
    const startId = Number(message.start_media_item_id)
    const start = downloadOf(startId)
    if (start?.state !== 'completed') {
      this.error(NOT_DOWNLOADED_MESSAGE)
      return
    }
    if (start.kind !== 'track') {
      // A downloaded video plays in the app's own video player, outside the queue.
      nativeCall('playVideo', { mediaId: startId }).catch(() => this.error('Could not play this video.'))
      return
    }
    let ids: number[]
    let index: number
    if (context?.kind === 'ids' && Array.isArray(context.ids)) {
      const asked = context.ids.map(Number)
      const startIndex = asked[Number(message.start_index)] === startId ? Number(message.start_index) : asked.indexOf(startId)
      ids = asked.filter(isPlayable)
      // The start's place among the rows that are kept.
      index = asked.slice(0, Math.max(0, startIndex)).filter(isPlayable).length
    } else if (context?.kind === 'library') {
      ids = await libraryIds(context.filters ?? {})
      if (!ids.includes(startId)) ids.unshift(startId)
      index = ids.indexOf(startId)
    } else {
      this.error('Invalid queue')
      return
    }
    local.rows = []
    let rows = this.newRows(ids)
    if (local.shuffled) {
      rows = [rows[index], ...shuffle(rows.filter((_, i) => i !== index))]
      index = 0
    }
    local.rows = rows
    local.index = index
    local.position = 0
    local.playing = true
    this.broadcast('set_queue_context', true)
  }

  private addToQueue(ids: number[], mode: unknown) {
    const local = current()
    const playable = ids.filter(isPlayable)
    if (playable.length === 0 || (mode !== 'next' && mode !== 'end')) {
      this.error(playable.length === 0 ? NOT_DOWNLOADED_MESSAGE : "Queue mode must be 'next' or 'end'")
      return
    }
    const rows = this.newRows(playable)
    if (mode === 'next' && local.rows.length > 0) local.rows.splice(local.index + 1, 0, ...rows)
    else local.rows.push(...rows)
    this.broadcast('add_many_to_queue')
  }

  private async handle(message: Message) {
    await downloadsReady()
    const local = current()
    const row = local.rows[local.index]
    const position = typeof message.position_seconds === 'number' && Number.isFinite(message.position_seconds)
      ? Math.max(0, message.position_seconds)
      : null
    switch (message.type) {
      case 'register_device':
        // As on the server: a page that was really closed does not start by itself.
        if (message.resume_playback !== true) local.playing = false
        this.emit({ type: 'registered', device_id: LOCAL_DEVICE_ID })
        this.broadcast(null, false, false)
        return
      case 'heartbeat':
        this.emit({ type: 'heartbeat_ack' })
        return
      case 'client_diagnostic':
        return
      case 'set_queue_context':
        await this.setQueue(message)
        return
      case 'add_to_queue':
        this.addToQueue([Number(message.media_item_id)], message.mode ?? 'end')
        return
      case 'add_many_to_queue':
        this.addToQueue(Array.isArray(message.media_item_ids) ? message.media_item_ids.map(Number) : [], message.mode)
        return
      case 'play':
        if (!row) return
        if (!isPlayable(row.id)) {
          this.error(NOT_DOWNLOADED_MESSAGE)
          return
        }
        local.playing = true
        this.broadcast('play')
        return
      case 'pause':
        local.playing = false
        if (position != null) local.position = position
        this.broadcast('pause')
        return
      case 'seek':
        if (position == null || !row || message.queue_item_id !== row.qid) return
        local.position = position
        this.broadcast('seek', true)
        return
      case 'position_report':
        if (position == null || !row || message.queue_item_id !== row.qid) return
        local.position = position
        this.broadcast('position_report', true)
        return
      case 'track_ended':
        if (!row || message.queue_item_id !== row.qid) {
          // Not about the current row any more: the sender gets the state to converge on.
          this.emit({
            type: 'state', state: this.snapshot(), position_changed: false,
            source_device_id: LOCAL_DEVICE_ID, source_command: 'track_ended',
          })
          return
        }
        if (local.repeat === 'one') {
          local.position = 0
          local.playing = true
        } else {
          this.next()
        }
        this.broadcast('track_ended', true)
        return
      case 'next':
        if (!row) return
        this.next()
        this.broadcast('next', true)
        return
      case 'prev':
        if (!row) return
        local.index = this.step(local.index, -1) ?? local.index
        local.position = 0
        this.broadcast('prev', true)
        return
      case 'jump_to_index': {
        const index = Math.max(0, Math.min(Number(message.index) || 0, local.rows.length - 1))
        const target = local.rows[index]
        if (!target) return
        if (!isPlayable(target.id)) {
          this.error(NOT_DOWNLOADED_MESSAGE)
          return
        }
        local.index = index
        local.position = 0
        local.playing = true
        this.broadcast('jump_to_index', true)
        return
      }
      case 'set_shuffle':
        local.shuffled = message.enabled === true
        if (row) {
          const others = local.rows.filter((r) => r.qid !== row.qid)
          if (local.shuffled) {
            // The playing row first, the rest in a new order.
            local.rows = [row, ...shuffle(others)]
            local.index = 0
          } else {
            this.keepCurrent(() => local.rows.sort((a, b) => a.seq - b.seq))
          }
        }
        this.broadcast('set_shuffle')
        return
      case 'set_repeat':
        if (message.mode !== 'off' && message.mode !== 'all' && message.mode !== 'one') {
          this.error('Invalid repeat mode')
          return
        }
        local.repeat = message.mode
        this.broadcast('set_repeat')
        return
      case 'remove_from_queue': {
        const at = local.rows.findIndex((r) => r.qid === message.queue_item_id)
        if (at < 0) return
        local.rows.splice(at, 1)
        if (at < local.index) local.index -= 1
        else if (at === local.index) {
          // The playing row went: the one that took its place plays, from its start.
          local.index = Math.min(local.index, Math.max(0, local.rows.length - 1))
          local.position = 0
          if (local.rows.length === 0 || !isPlayable(local.rows[local.index].id)) local.playing = false
        }
        this.broadcast('remove_from_queue', true)
        return
      }
      case 'move_queue_item':
      case 'reorder_queue_item': {
        const from = local.rows.findIndex((r) => r.qid === message.queue_item_id)
        if (from < 0) return
        const wanted = message.type === 'move_queue_item' ? from + Number(message.direction) : Number(message.target_index)
        const to = Math.max(0, Math.min(Number.isFinite(wanted) ? wanted : from, local.rows.length - 1))
        this.keepCurrent(() => {
          const [moved] = local.rows.splice(from, 1)
          local.rows.splice(to, 0, moved)
        })
        this.broadcast(message.type)
        return
      }
      case 'transfer_to':
      case 'remove_device':
        this.error('Other devices cannot be reached with no connection.')
        return
      default:
        this.error(`Unknown message type: ${String(message.type)}`)
    }
  }
}
