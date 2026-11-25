import type { PlaybackDevice } from './types'

/** The rows of the "Playing on" pickers: every connected device, and always
 * this one when it is not the active device - including while the active
 * device is offline or this device has not been flagged connected yet. Without
 * it the only way to take playback back here would be missing exactly when
 * the other device has gone away. */
export function pickerDevices(devices: PlaybackDevice[], deviceId: number | null): PlaybackDevice[] {
  const connected = devices.filter((d) => d.is_connected)
  if (deviceId == null || connected.some((d) => d.id === deviceId)) return connected
  const known = devices.find((d) => d.id === deviceId)
  const self: PlaybackDevice = known
    ? { ...known, is_connected: true }
    : { id: deviceId, name: 'This device', device_type: 'web', is_active: false, is_connected: true, last_seen_at: '' }
  return [...connected, self]
}
