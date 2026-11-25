// "Is this page the output right now?" - decided here, once.
//
// Every part of the player that needs the answer (the active-device flag, the
// element sync, the media session, the silent keep-alive, a backgrounded
// video's sound, the play reports) reads this one state instead of its own mix
// of "connected", "replaced by another tab" and the active device, and says
// what it does with it through the named policies below.

/**
 * - `active`: this page is registered and is the server's active device.
 * - `dropout`: the socket is down, this page was the active device when it
 *   went, and nothing has replaced it. A lost control plane, not a hand-off:
 *   a locked phone's socket is suspended routinely while its element plays on.
 * - `offline`: the socket is down and this page was not the active device (a
 *   remote control, or nothing was playing here).
 * - `replaced`: another tab of this browser registered as this device and
 *   the server closed this page's socket. That tab is the output; this page
 *   must be silent.
 * - `remote`: registered, and another device is the active one.
 * - `none`: registered, and no device is active.
 */
export type OutputState = 'active' | 'dropout' | 'offline' | 'replaced' | 'remote' | 'none'

export interface OutputInputs {
  /** The playback socket is open and this page has registered on it. */
  connected: boolean
  /** The server closed the socket because another tab took this device. */
  socketReplaced: boolean
  /** This page's device id while registered, else null. */
  deviceId: number | null
  /** The device id of the last registration, kept while the socket is down. */
  lastDeviceId: number | null
  /** The server's active device, as last heard. */
  activeDeviceId: number | null | undefined
}

export function deriveOutputState({
  connected,
  socketReplaced,
  deviceId,
  lastDeviceId,
  activeDeviceId,
}: OutputInputs): OutputState {
  if (connected && deviceId != null && deviceId === activeDeviceId) return 'active'
  if (socketReplaced) return 'replaced'
  if (!connected) return lastDeviceId != null && lastDeviceId === activeDeviceId ? 'dropout' : 'offline'
  return activeDeviceId != null ? 'remote' : 'none'
}

/** The socket is down and no other tab took the device over. */
export function socketLost(state: OutputState): boolean {
  return state === 'dropout' || state === 'offline'
}

/** Another device than this page's (by its last registration) is the
 * server's active one - not merely "this page is not": that is also true for
 * a moment around every reconnect. */
export function anotherDeviceIsActive(activeDeviceId: number | null | undefined, lastDeviceId: number | null): boolean {
  return activeDeviceId != null && activeDeviceId !== lastDeviceId
}

// What each consumer does while this page is not `active`. They differ on
// purpose in what else they ask for; they do not differ in what a dropout is.
// All four take any lost socket (`offline` as well as `dropout`) for one: a
// page that was not the output has nothing playing to keep. (For the keep-alive that is not quite true, see wantsKeepAlive.)

/** The element sync leaves its elements alone instead of pausing them: the
 * socket is lost and playback is wanted - hidden or visible (returning to
 * the foreground replaces the socket, which reads as a drop for a moment). */
export function keepsElementsThroughSocketLoss(
  state: OutputState,
  { hasCurrent, wantPlaying }: { hasCurrent: boolean; wantPlaying: boolean },
): boolean {
  return socketLost(state) && hasCurrent && wantPlaying
}

/** The media session keeps its notification instead of clearing it: only
 * while hidden, where losing it costs the page its background-audio
 * exemption; a visible page sets it up again with the next snapshot. */
export function keepsMediaSessionThroughSocketLoss(
  state: OutputState,
  { hasCurrent, serverPlaying, hidden }: { hasCurrent: boolean; serverPlaying: boolean; hidden: boolean },
): boolean {
  return socketLost(state) && hasCurrent && serverPlaying && hidden
}

/** The silent keep-alive player runs: while this page is the active device
 * and playing, and through a lost socket while hidden (as the media session).
 * Because `offline` counts, a hidden remote-control page whose socket drops
 * while another device plays starts it too. */
export function wantsKeepAlive(
  state: OutputState,
  { hasCurrent, serverPlaying, hidden }: { hasCurrent: boolean; serverPlaying: boolean; hidden: boolean },
): boolean {
  return hasCurrent && serverPlaying && (state === 'active' || (socketLost(state) && hidden))
}

/** A backgrounded video's audio-only element is kept (not torn down): on
 * the active device and through a lost socket. Only the active device starts
 * or changes it. */
export function keepsBackgroundVideoAudio(state: OutputState): boolean {
  return state === 'active' || socketLost(state)
}
