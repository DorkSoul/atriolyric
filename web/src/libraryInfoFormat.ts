// Number and date wording for the Library info screen and its top-bar count.

/** The server sends UTC times without a zone suffix: read them as UTC. */
export function parseServerDate(value: string): Date {
  return new Date(/(Z|[+-]\d\d:?\d\d)$/.test(value) ? value : `${value}Z`)
}

/** "412 GB", "1.3 TB", "820 MB" - base 1024, one decimal under 10. */
export function formatBytes(bytes: number): string {
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let value = Math.max(0, bytes)
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  const text = unit === 0 || value >= 10 ? Math.round(value).toLocaleString() : value.toFixed(1)
  return `${text} ${units[unit]}`
}

/** A long span of play time: "51 d 4 h", "4 h 12 min", "12 min". */
export function formatPlayTime(seconds: number): string {
  const minutes = Math.round(Math.max(0, seconds) / 60)
  const days = Math.floor(minutes / 1440)
  const hours = Math.floor((minutes % 1440) / 60)
  const mins = minutes % 60
  if (days > 0) return `${days.toLocaleString()} d ${hours} h`
  if (hours > 0) return `${hours} h ${mins} min`
  return `${mins} min`
}

/** "9 Nov", or "9 Nov 2025" when not this year. */
export function formatAddedDate(iso: string): string {
  const date = parseServerDate(iso)
  const sameYear = date.getFullYear() === new Date().getFullYear()
  return date.toLocaleDateString(undefined, { day: 'numeric', month: 'short', ...(sameYear ? {} : { year: 'numeric' }) })
}

/** "9 Nov, 14:32" (with the year when not this year). */
export function formatLastScan(iso: string): string {
  const date = parseServerDate(iso)
  const sameYear = date.getFullYear() === new Date().getFullYear()
  return date.toLocaleString(undefined, {
    day: 'numeric',
    month: 'short',
    ...(sameYear ? {} : { year: 'numeric' }),
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** "13,270 · 71%" for a part of a whole. */
export function formatShare(part: number, whole: number): string {
  const count = part.toLocaleString()
  return whole > 0 ? `${count} · ${Math.round((part / whole) * 100)}%` : count
}
