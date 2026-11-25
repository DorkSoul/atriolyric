export interface SyncedLine {
  time: number
  text: string
}

export interface ParsedLyrics {
  /** Time-sorted lines, empty unless the text carries timestamps. */
  synced: SyncedLine[]
  /** Clean text without any timestamp or metadata tags. */
  plain: string | null
}

// [mm:ss], [mm:ss.xx], [mm:ss:xx]; also [hh:mm:ss] is not LRC, so not handled.
const TIME_TAG = /\[(\d+):(\d{1,2})(?:[.:](\d+))?\]/g
const HAS_TIME_TAG = /\[\d+:\d{1,2}(?:[.:]\d+)?\]/
const META_TAG = /^\s*\[([a-zA-Z#]+):([^\]]*)\]\s*$/

/**
 * Parses lyrics text. Any text containing LRC timestamps is treated as
 * synced - whichever field it came from - with the tags stripped, metadata
 * tags ([ar:], [ti:], [offset:]) dropped and the offset applied.
 */
export function parseLyrics(...candidates: (string | null | undefined)[]): ParsedLyrics {
  const timed = candidates.find((c) => c && HAS_TIME_TAG.test(c))
  if (!timed) {
    const plain = candidates.find((c) => c && c.trim())
    return { synced: [], plain: plain ? plain.replace(/\r\n?/g, '\n').trim() : null }
  }
  let offset = 0
  const lines: SyncedLine[] = []
  for (const row of timed.split(/\r?\n/)) {
    const meta = META_TAG.exec(row)
    if (meta) {
      if (meta[1].toLowerCase() === 'offset') offset = (Number(meta[2]) || 0) / 1000
      continue
    }
    const stamps = [...row.matchAll(TIME_TAG)]
    if (stamps.length === 0) {
      // Untimed text inside timed lyrics belongs with the line before it.
      const text = row.trim()
      if (text && lines.length > 0) lines.push({ time: lines[lines.length - 1].time, text })
      continue
    }
    const text = row.replace(TIME_TAG, '').trim()
    for (const s of stamps) {
      const frac = s[3] ? Number(`0.${s[3]}`) : 0
      lines.push({ time: Math.max(0, Number(s[1]) * 60 + Number(s[2]) + frac - offset), text })
    }
  }
  lines.sort((a, b) => a.time - b.time)
  const plain = lines.map((l) => l.text).join('\n').trim()
  return { synced: lines, plain: plain || null }
}

/** Index of the line being sung at `time` (-1 before the first line). */
export function activeLyricIndex(lines: SyncedLine[], time: number, lead = 0.25): number {
  let active = -1
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].time <= time + lead) active = i
    else break
  }
  return active
}
