// Downloading upcoming tracks into memory.

import { isNativeApp } from '../native/bridge'
import type { TrackDownloadSample } from './adaptiveBitrate'

// The next track is downloaded into memory ahead of time so that advancing to
// it never needs the network. A hidden/locked page can lose network access
// (Doze, a frozen tab) at exactly the moment the previous track ends, and a
// fresh stream request made then may never complete - the element sits at
// readyState 0 and the OS drops the media notification that keeps the page
// alive. Anything larger than this cap, or without a usable body, falls back
// to letting the element stream from the network.
export const MAX_TRACK_PREFETCH_BYTES = 96 * 1024 * 1024
// How many queue items after the current one are held in memory: the next
// one (downloaded first, for the hand-off) and the ones after it, fetched one
// at a time once the next is in. With the data saver on, only the next one.
export const PREFETCH_AHEAD = 4
// The most memory all held copies together (the one just finished, the
// current one and the ones ahead) may take before no further track ahead is
// fetched. The next track is always fetched (up to MAX_TRACK_PREFETCH_BYTES);
// a FLAC original is tens of MB, a transcode a few. An object so
// the browser tests can lower it through the app's own module instance.
export const PREFETCH_BUDGET = { bytes: 150 * 1024 * 1024 }
// Blob URLs are held for the track just finished (so going back never
// re-downloads it), the current one and the ones ahead (see above).
export function streamTrackId(streamUrl: string): number | null {
  const id = Number(/\/api\/stream\/(\d+)/.exec(streamUrl)?.[1])
  return Number.isFinite(id) ? id : null
}

/** How many queue items ahead to hold: PREFETCH_AHEAD, or just the next one
 * when the browser says the user wants to save data. */
export function prefetchAheadCount(): number {
  const connection = (navigator as Navigator & { connection?: { saveData?: boolean } }).connection
  return connection?.saveData ? 1 : PREFETCH_AHEAD
}

/** Where the playing item is in `queue`, which is what "next" and "ahead"
 * count from. The server's index while it names that item. A track end
 * moves `current` on at once (see advanceLocalTrackAfterEnd) while the
 * server's state still names the track that ended until it answers (or for
 * as long as the socket is down): `current` is then the item after it.
 * Counting from the server's index there would take the track now playing
 * for the next one and load it into the standby a second time. */
export function playingQueueIndex(
  queue: readonly { id: number }[],
  current: { id: number } | null,
  serverIndex: number | null | undefined,
): number {
  const index = serverIndex ?? 0
  if (!current || queue[index]?.id === current.id) return index
  if (queue[index + 1]?.id === current.id) return index + 1
  const found = queue.findIndex((item) => item.id === current.id)
  return found >= 0 ? found : index
}

// The size of each copy this module made, by blob URL, for the budget.
const blobBytes = new Map<string, number>()

/** Bytes held by these blob URLs (copies made elsewhere count as 0). */
export function heldBlobBytes(blobUrls: Iterable<string>): number {
  let total = 0
  for (const url of blobUrls) total += blobBytes.get(url) ?? 0
  return total
}

/** Forgets the sizes of copies that are no longer held. */
export function forgetBlobSizesExcept(held: Iterable<string>): void {
  const keep = new Set(held)
  for (const url of blobBytes.keys()) if (!keep.has(url)) blobBytes.delete(url)
}

// Told whenever a copy is added to the player's cache, so the download of the
// tracks further ahead can carry on once the next one is in.
const blobListeners = new Set<() => void>()

export function onTrackBlobRemembered(listener: () => void): () => void {
  blobListeners.add(listener)
  return () => {
    blobListeners.delete(listener)
  }
}

export function notifyTrackBlobRemembered(): void {
  for (const listener of [...blobListeners]) listener()
}

/** The source's bitrate from the stream's `Server-Timing` header
 * (`source-bitrate;desc="320.000"`), if it is there. */
function sourceBitrateKbps(headers: Headers): number | undefined {
  const kbps = Number(/(?:^|,)\s*source-bitrate\s*;\s*desc="?([\d.]+)/.exec(headers.get('server-timing') ?? '')?.[1])
  return Number.isFinite(kbps) && kbps > 0 ? kbps : undefined
}

/** `onDownloaded` is told about a copy that was made: how large it is and how
 * long it took from the request on - the one place the app sees a track's
 * bytes arrive, which the adaptive quality uses to step up again. */
export async function prefetchTrackBlob(
  url: string,
  signal: AbortSignal,
  maxBytes = MAX_TRACK_PREFETCH_BYTES,
  onDownloaded?: (sample: TrackDownloadSample) => void,
): Promise<string | null> {
  // The Android app's native players cannot play a copy held by the page, and
  // do not need one: they keep their network access with the screen off.
  if (isNativeApp()) return null
  try {
    const startedAt = performance.now()
    const response = await fetch(url, { signal, credentials: 'same-origin' })
    if (!response.ok || !response.body) return null
    const declared = Number(response.headers.get('content-length'))
    if (Number.isFinite(declared) && declared > maxBytes) {
      void response.body.cancel()
      return null
    }
    const reader = response.body.getReader()
    const chunks: BlobPart[] = []
    let received = 0
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      received += value.byteLength
      if (received > maxBytes) {
        void reader.cancel()
        return null
      }
      chunks.push(new Uint8Array(value))
    }
    if (received === 0) return null
    const blobUrl = URL.createObjectURL(new Blob(chunks, { type: response.headers.get('content-type') ?? 'audio/mpeg' }))
    blobBytes.set(blobUrl, received)
    onDownloaded?.({
      bytes: received,
      elapsedMs: performance.now() - startedAt,
      sourceKbps: sourceBitrateKbps(response.headers),
    })
    return blobUrl
  } catch {
    return null
  }
}
