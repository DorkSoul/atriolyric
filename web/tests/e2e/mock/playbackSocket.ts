// The playback socket: the mock server's queue, snapshots and command handling.

import type { Page } from 'playwright/test'

import { filteredTracks, sortTracks } from './library'
import type { AppMockState, PlaybackState } from './types'

export function queueIdsForContext(state: AppMockState, context: Record<string, unknown>): number[] {
  if (context.kind === 'ids' && Array.isArray(context.ids)) {
    return context.ids.filter((id): id is number => typeof id === 'number')
  }
  if (context.kind === 'library') {
    const filters = (context.filters ?? {}) as Record<string, string>
    const query = new URL('http://mock.test')
    for (const [key, value] of Object.entries(filters)) {
      if (value) query.searchParams.set(key, value)
    }
    const sort = typeof context.sort === 'string' ? context.sort : null
    const direction = typeof context.direction === 'string' ? context.direction : null
    return sortTracks(filteredTracks(state, query), sort, direction).map((item) => item.id)
  }
  return []
}

export function setQueue(state: AppMockState, ids: number[], startId: number | null) {
  const currentIds = [...ids]
  const index = startId == null ? 0 : Math.max(0, currentIds.indexOf(startId))
  state.playback.queue_media_item_ids = currentIds
  state.playback.queue_item_ids = currentIds.map(() => state.nextQueueItemId++)
  state.playback.queue_index = index
  state.playback.media_item_id = currentIds[index] ?? null
  state.playback.queue_context_count = currentIds.length
  state.playback.position_seconds = 0
  state.playback.is_playing = currentIds.length > 0
}

/** Like the server's claim_output_for_requester (the stopped-playback half
 * is not modelled): a new queue or `play` from the page (device 1) makes it
 * the active device when the active one is offline, gone or silent. */
export function claimIfActiveDeviceOffline(state: AppMockState) {
  const active = state.playback.devices.find((device) => device.id === state.playback.active_device_id)
  if (!active?.is_connected || state.silentDeviceIds.has(active.id)) markActiveDevice(state, 1)
}

export function markActiveDevice(state: AppMockState, deviceId: number | null) {
  state.playback.active_device_id = deviceId
  for (const device of state.playback.devices) device.is_active = device.id === deviceId
}

/** What the server sends as `state`: the playback state plus the queue item
 * at `queue_index` (null when the queue is empty). */
export function playbackSnapshot(state: AppMockState): PlaybackState & { current_queue_item_id: number | null } {
  const playback = state.playback
  return {
    ...playback,
    devices: playback.devices.map((device) => ({ ...device })),
    queue_media_item_ids: [...playback.queue_media_item_ids],
    queue_item_ids: [...playback.queue_item_ids],
    current_queue_item_id: playback.queue_item_ids[playback.queue_index] ?? null,
  }
}

/** A broadcast: one more revision, then the snapshot (with `extra` fields
 * such as `position_changed`, `source_device_id`, `source_command`). */
export function sendPlaybackState(
  ws: { send: (message: string) => void },
  state: AppMockState,
  extra: Record<string, unknown> = {},
) {
  state.playback.revision += 1
  ws.send(JSON.stringify({ type: 'state', state: playbackSnapshot(state), ...extra }))
}

/** Sends the mock's current playback state to the page as a server broadcast
 * would (a new revision), for tests that change `mock.playback` by hand. */
export function pushPlaybackState(state: AppMockState, extra: Record<string, unknown> = {}): void {
  const socket = state.playbackSocket
  if (!socket) throw new Error('no playback socket to send on')
  sendPlaybackState(socket, state, extra)
}

const isRowId = (value: unknown) => typeof value === 'number' && Number.isInteger(value) && value > 0

/** The error the server answers a message with when it lacks a field the
 * protocol requires (docs/playback-protocol.md), or null when it has them:
 * `track_ended` and `seek` name a `queue_item_id`; `pause` has the key
 * `position_seconds` (a number or null); `position_report` has both;
 * `add_many_to_queue` has 1-5000 integer `media_item_ids` and a `mode`. */
export function playbackMessageViolation(message: Record<string, unknown>): string | null {
  const type = message.type
  if (type === 'seek' && typeof message.position_seconds !== 'number') return 'Invalid seek position'
  if ((type === 'track_ended' || type === 'seek') && !isRowId(message.queue_item_id)) return 'Invalid queue item id'
  if (type === 'pause' && !(
    'position_seconds' in message &&
    (message.position_seconds === null || typeof message.position_seconds === 'number')
  )) return 'Invalid pause position'
  if (type === 'position_report' && (!isRowId(message.queue_item_id) || typeof message.position_seconds !== 'number')) {
    return 'Invalid position report'
  }
  if (type === 'add_many_to_queue') {
    const ids = message.media_item_ids
    if (
      !Array.isArray(ids) || ids.length === 0 || !ids.every((id) => Number.isInteger(id)) ||
      (message.mode !== 'next' && message.mode !== 'end')
    ) return 'Invalid add-to-queue command'
    if (ids.length > 5000) return 'Too many items to queue at once'
  }
  return null
}

/** Throws on a message the server would reject for a missing field. */
export function assertPlaybackMessageShape(message: Record<string, unknown>): void {
  const violation = playbackMessageViolation(message)
  if (violation) throw new Error(`${violation}: ${JSON.stringify(message)}`)
}

export function handlePlaybackMessage(
  state: AppMockState,
  ws: { send: (message: string) => void },
  message: Record<string, unknown>,
) {
  const type = message.type
  if (type === 'register_device') {
    const device = state.playback.devices.find((item) => item.id === 1)
    if (device && typeof message.name === 'string') device.name = message.name
    if (device && typeof message.device_type === 'string') device.device_type = message.device_type
    if (message.resume_playback === true) markActiveDevice(state, 1)
    ws.send(JSON.stringify({ type: 'registered', device_id: 1 }))
    sendPlaybackState(ws, state)
    return
  }

  // Like the real server, a diagnostic is only logged - no state reply.
  if (type === 'client_diagnostic') return
  // Liveness only: answered to the sender, never broadcast.
  if (type === 'heartbeat') {
    ws.send(JSON.stringify({ type: 'heartbeat_ack' }))
    return
  }

  // Shapes the server rejects get its error reply and change nothing. (A
  // message from the page has already been checked, and recorded as a
  // violation, in installPlaybackSocket.)
  const violation = playbackMessageViolation(message)
  if (violation) {
    ws.send(JSON.stringify({ type: 'error', message: violation }))
    return
  }

  const currentQueueItemId = state.playback.queue_item_ids[state.playback.queue_index] ?? null
  const namedItem = typeof message.queue_item_id === 'number' ? message.queue_item_id : null
  if (type === 'position_report') {
    // The periodic save: only from the active device (the page is device 1)
    // and only for the item still playing; otherwise silently dropped.
    if (
      namedItem !== currentQueueItemId || state.playback.active_device_id !== 1 ||
      typeof message.position_seconds !== 'number'
    ) return
    state.playback.position_seconds = message.position_seconds
    sendPlaybackState(ws, state, { position_changed: true, source_device_id: 1, source_command: 'position_report' })
    return
  }
  if (type === 'track_ended' && namedItem !== currentQueueItemId) {
    // A replay, or a race with another device's Next: nothing moves and
    // nobody else is told; the sender gets the current state, same revision.
    ws.send(JSON.stringify({
      type: 'state',
      state: playbackSnapshot(state),
      position_changed: false,
      source_device_id: 1,
      source_command: 'track_ended',
    }))
    return
  }
  // A scrub of an item that is no longer playing is ignored.
  if (type === 'seek' && namedItem !== currentQueueItemId) return

  const ids = state.playback.queue_media_item_ids
  if (type === 'set_queue_context') {
    const context = (message.context ?? {}) as Record<string, unknown>
    const startId = typeof message.start_media_item_id === 'number' ? message.start_media_item_id : null
    claimIfActiveDeviceOffline(state)
    setQueue(state, queueIdsForContext(state, context), startId)
    // Like the server: the queue's origin, dropped when none is named.
    state.playback.queue_origin = (message.origin as typeof state.playback.queue_origin | undefined) ?? null
  } else if (
    (type === 'add_to_queue' && typeof message.media_item_id === 'number') ||
    type === 'add_many_to_queue'
  ) {
    // Several items in one change (one broadcast), like the server.
    const added = type === 'add_to_queue' ? [message.media_item_id as number] : message.media_item_ids as number[]
    const wasEmpty = ids.length === 0
    const insertAt = Math.max(0, message.mode === 'next' ? state.playback.queue_index + 1 : ids.length)
    ids.splice(insertAt, 0, ...added)
    state.playback.queue_item_ids.splice(insertAt, 0, ...added.map(() => state.nextQueueItemId++))
    state.playback.queue_context_count = ids.length
    if (wasEmpty) state.playback.queue_index = 0
    if (state.playback.media_item_id == null) {
      state.playback.media_item_id = added[0]
      state.playback.is_playing = true
    }
  } else if (type === 'play') {
    claimIfActiveDeviceOffline(state)
    state.playback.is_playing = true
  } else if (type === 'pause') {
    state.playback.is_playing = false
    // Saved only from the device producing the sound.
    if (typeof message.position_seconds === 'number' && state.playback.active_device_id === 1) {
      state.playback.position_seconds = message.position_seconds
    }
  } else if (type === 'seek' && typeof message.position_seconds === 'number') {
    state.playback.position_seconds = message.position_seconds
  } else if (type === 'next' || type === 'track_ended') {
    const next = Math.min(state.playback.queue_index + 1, ids.length - 1)
    state.playback.queue_index = Math.max(0, next)
    state.playback.media_item_id = ids[next] ?? null
    state.playback.position_seconds = 0
    if (type === 'track_ended') {
      sendPlaybackState(ws, state, { position_changed: true, source_device_id: 1, source_command: 'track_ended' })
      return
    }
  } else if (type === 'prev') {
    state.playback.queue_index = Math.max(0, state.playback.queue_index - 1)
    state.playback.media_item_id = ids[state.playback.queue_index] ?? null
    state.playback.position_seconds = 0
  } else if (type === 'jump_to_index' && typeof message.index === 'number') {
    const index = Math.max(0, Math.min(message.index, ids.length - 1))
    state.playback.queue_index = index
    state.playback.media_item_id = ids[index] ?? null
    state.playback.position_seconds = 0
    state.playback.is_playing = ids.length > 0
  } else if (type === 'set_shuffle' && typeof message.enabled === 'boolean') {
    state.playback.is_shuffled = message.enabled
  } else if (type === 'set_repeat' && (message.mode === 'off' || message.mode === 'all' || message.mode === 'one')) {
    state.playback.repeat_mode = message.mode
  } else if (type === 'transfer_to' && typeof message.device_id === 'number') {
    if (state.playback.devices.some((device) => device.id === message.device_id)) {
      markActiveDevice(state, message.device_id)
    }
  } else if (type === 'remove_device' && typeof message.device_id === 'number') {
    state.playback.devices = state.playback.devices.filter((device) => device.id !== message.device_id)
    if (state.playback.active_device_id === message.device_id) markActiveDevice(state, 1)
  } else if (
    type === 'remove_from_queue' &&
    typeof message.queue_item_id === 'number'
  ) {
    const index = state.playback.queue_item_ids.indexOf(message.queue_item_id)
    if (index >= 0) {
      ids.splice(index, 1)
      state.playback.queue_item_ids.splice(index, 1)
      if (index < state.playback.queue_index) state.playback.queue_index -= 1
      if (index === state.playback.queue_index) {
        state.playback.media_item_id = ids[state.playback.queue_index] ?? null
      }
      state.playback.queue_context_count = ids.length
      if (ids.length === 0) state.playback.is_playing = false
    }
  } else if (
    type === 'move_queue_item' &&
    typeof message.queue_item_id === 'number' &&
    typeof message.direction === 'number'
  ) {
    const index = state.playback.queue_item_ids.indexOf(message.queue_item_id)
    const target = index + message.direction
    if (index >= 0 && target >= 0 && target < ids.length) {
      ;[ids[index], ids[target]] = [ids[target], ids[index]]
      ;[state.playback.queue_item_ids[index], state.playback.queue_item_ids[target]] = [
        state.playback.queue_item_ids[target],
        state.playback.queue_item_ids[index],
      ]
      if (index === state.playback.queue_index) state.playback.queue_index = target
      else if (target === state.playback.queue_index) state.playback.queue_index = index
    }
  } else if (
    type === 'reorder_queue_item' &&
    typeof message.queue_item_id === 'number' &&
    typeof message.target_index === 'number'
  ) {
    const index = state.playback.queue_item_ids.indexOf(message.queue_item_id)
    if (index >= 0) {
      const target = Math.max(0, Math.min(message.target_index, ids.length - 1))
      const [id] = ids.splice(index, 1)
      const [queueItemId] = state.playback.queue_item_ids.splice(index, 1)
      ids.splice(target, 0, id)
      state.playback.queue_item_ids.splice(target, 0, queueItemId)
      state.playback.queue_index = ids.indexOf(state.playback.media_item_id ?? -1)
    }
  }
  sendPlaybackState(ws, state)
}

/** Processes the held socket messages (see `holdPlaybackTypes`) in order and
 * stops holding further ones, like a server catching up. */
export function releaseHeldPlaybackMessages(state: AppMockState): void {
  state.holdPlaybackTypes.clear()
  const socket = state.playbackSocket
  if (!socket) throw new Error('no playback socket to reply on')
  for (const message of state.heldPlaybackMessages.splice(0)) handlePlaybackMessage(state, socket, message)
}

/** Custom art set in this mock ("type:key" -> art_version). Cleared per install. */

/** Answers the page's playback socket with the mock server. */
export async function installPlaybackSocket(page: Page, state: AppMockState): Promise<void> {
  await page.routeWebSocket('**/ws/playback', (websocket) => {
    state.playbackSocketConnections += 1
    if (state.playbackSocketMode === 'refuse') {
      void websocket.close({ code: 1013, reason: 'mock server refused' })
      return
    }
    if (!state.authenticated) {
      // Like the server: a socket without a valid session is
      // accepted and closed at once with 4401, so the page reads the code.
      void websocket.close({ code: 4401, reason: 'not authenticated' })
      return
    }
    state.playbackSocket = websocket
    websocket.onMessage((raw) => {
      const message = JSON.parse(typeof raw === 'string' ? raw : raw.toString()) as Record<string, unknown>
      state.wsMessages.push(message)
      try {
        assertPlaybackMessageShape(message)
      } catch (error) {
        state.protocolViolations.push(error instanceof Error ? error.message : String(error))
        websocket.send(JSON.stringify({ type: 'error', message: playbackMessageViolation(message) }))
        return
      }
      if (typeof message.type === 'string' && state.holdPlaybackTypes.has(message.type)) {
        state.heldPlaybackMessages.push(message)
        return
      }
      handlePlaybackMessage(state, websocket, message)
    })
  })
}
