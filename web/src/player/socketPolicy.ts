// Playback socket timings, command queueing rules and the device label.

// How long a position this device sent (a seek or a position report) is still
// recognised when it comes back.
export const SEEK_ECHO_WINDOW_MS = 30_000
// Commands from this device whose broadcast must never move its own element:
// `position_report` is the periodic position save, `seek` a seek already
// applied locally, and `track_ended` is only sent after the element already
// advanced or restarted.
export const OWN_ECHO_COMMANDS: ReadonlySet<unknown> = new Set(['seek', 'position_report', 'track_ended'])
// User commands that wait for the replacement socket instead of being dropped
// while the playback socket is down (every return to the foreground replaces
// it; a locked phone's is suspended). They keep their order and are dropped
// together, with one message, once the oldest has waited OFFLINE_COMMAND_TTL_MS.
// play/pause are held separately (latest wins, no time limit: the element has
// already done what they say, see `setLocalPlayback`). Position reports
// (`position_report`, stale by the next one), heartbeats and diagnostics are
// never held: they are dropped while the socket is not ready.
export const OFFLINE_QUEUED_COMMANDS: ReadonlySet<unknown> = new Set([
  'next', 'prev', 'jump_to_index', 'seek', 'set_queue_context', 'add_to_queue', 'add_many_to_queue',
  'remove_from_queue',
  'move_queue_item', 'reorder_queue_item', 'set_shuffle', 'set_repeat', 'transfer_to', 'remove_device',
])
export const OFFLINE_COMMAND_TTL_MS = 10_000
// The most ids one message may name (the server's MAX_EXPLICIT_IDS): a larger
// `add_many_to_queue` batch is sent in parts.
export const MAX_IDS_PER_MESSAGE = 5_000
// Playback socket reconnects: exponential backoff from RECONNECT_BASE_DELAY_MS
// to RECONNECT_MAX_DELAY_MS; a handshake still pending after CONNECT_TIMEOUT_MS
// is abandoned; an open socket that missed two heartbeat answers in a row and
// heard nothing for SOCKET_SILENCE_LIMIT_MS is replaced.
export const RECONNECT_BASE_DELAY_MS = 1_000
export const RECONNECT_MAX_DELAY_MS = 30_000
export const CONNECT_TIMEOUT_MS = 10_000
export const SOCKET_SILENCE_LIMIT_MS = 45_000
export const HEARTBEAT_INTERVAL_MS = 20_000
// A dropped socket is only reported once it has stayed down this long: the
// foreground replacement takes a moment and should not flash a message.
export const RECONNECTING_NOTICE_DELAY_MS = 3_000
// A track end the server has not heard about (the socket was down) is replayed
// after the reconnect while it is younger than PENDING_END_MAX_AGE_MS - or, if
// this device's element is still on the track it moved on to at that end,
// younger than PENDING_END_MAX_AGE_ON_ADVANCE_MS (a locked phone's socket can
// stay suspended for well over a minute while it plays on).
export const PENDING_END_MAX_AGE_MS = 60_000
export const PENDING_END_MAX_AGE_ON_ADVANCE_MS = 30 * 60_000

// How long a play/pause this device already applied to its element (and sent)
// outranks snapshots that still say the opposite because the server had not
// processed it yet. A snapshot that agrees ends it sooner.
export const PLAY_INTENT_CONFIRM_MS = 5_000

// A play/pause this device asked for that the server has not confirmed yet.
// `sentAt` is null while it waits for the socket; `external` marks a
// pause/play the OS made rather than the user. (Where the element stopped
// travels in the `pause` message itself, see `pauseMessage`.)
export interface PlayIntent {
  command: 'play' | 'pause'
  sentAt: number | null
  external: boolean
}

export function guessDeviceLabel(): { name: string; deviceType: string } {
  const ua = navigator.userAgent
  const isMobile = /Mobi|Android|iPhone|iPad/i.test(ua)
  let browser = 'Browser'
  if (ua.includes('Firefox')) browser = 'Firefox'
  else if (ua.includes('Edg')) browser = 'Edge'
  else if (ua.includes('Chrome')) browser = 'Chrome'
  else if (ua.includes('Safari')) browser = 'Safari'
  return { name: `${browser} (${isMobile ? 'Mobile' : 'Desktop'})`, deviceType: isMobile ? 'mobile' : 'web' }
}
