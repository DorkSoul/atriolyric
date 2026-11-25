/** "m:ss", or "h:mm:ss" from one hour up, for track and video lengths and
 * playback times. Fractions are floored (a 3:25.9 song reads 3:25, as the
 * player clock does). `null`/`undefined` (length unknown) is an empty string
 * so callers can drop it; a number that is not finite - an unloaded media
 * element reports NaN - reads as 0:00. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null) return ''
  if (!Number.isFinite(seconds)) return '0:00'
  const total = Math.max(0, Math.floor(seconds))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const ss = String(s).padStart(2, '0')
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`
}
