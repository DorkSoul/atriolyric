// The server's art epoch (/api/version, app.art_epoch on the server): a
// number that changes when a rescan may have changed the art of tracks the
// app already knows. A track's art address is the same before and after, and
// is kept for a day by the browser (and by the Android app's own copies), so
// the epoch goes on the address: a new epoch is a new address, and the new
// picture shows on the next look. Remembered between visits so the
// addresses, and the copies kept under them, stay the same with no
// connection.

const STORAGE_KEY = 'atriolyric.artEpoch'

const listeners = new Set<() => void>()
let epoch = read()

function read(): number {
  try {
    const saved = Number(localStorage.getItem(STORAGE_KEY))
    return Number.isSafeInteger(saved) && saved > 0 ? saved : 0
  } catch {
    return 0
  }
}

/** 0 until the server has reported one. */
export function artEpoch(): number {
  return epoch
}

export function setArtEpoch(value: unknown): void {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value <= 0 || value === epoch) return
  epoch = value
  try {
    localStorage.setItem(STORAGE_KEY, String(value))
  } catch {
    // Private mode or a full store: this visit still uses the new epoch.
  }
  listeners.forEach((listener) => listener())
}

export function onArtEpochChange(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

/** A track art or video thumbnail address under the current epoch. */
export function withArtEpoch(url: string): string {
  return epoch ? `${url}${url.includes('?') ? '&' : '?'}e=${epoch}` : url
}
