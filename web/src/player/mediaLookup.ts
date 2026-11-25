// Turning queue ids into media items, a bounded number per request.

// `GET /api/library/media?ids=` accepts 20,000 characters of ids and reads the
// first 2,000 of them, while a queue can hold 5,000 explicit rows: asked for
// in one request, a long queue would be refused (422) on every snapshot, and
// the device would have no queue, no next track to prefetch and nothing to
// advance to.
// 500 ids stay far inside both limits whatever the length of an id.
export const MEDIA_LOOKUP_CHUNK_IDS = 500
// The first request starts this many rows before the playing one (Previous
// needs them) and runs on from there: what plays next is never held up by the
// rest of a long queue.
export const MEDIA_LOOKUP_ROWS_BEHIND = 50

/** The ids among `ids` that are not `known` yet, each once, in requests of at
 * most MEDIA_LOOKUP_CHUNK_IDS: first the rows around `aroundIndex` and on to
 * the end of the list, then the rows before them. */
export function mediaLookupChunks(
  ids: readonly number[],
  known: (id: number) => boolean,
  aroundIndex = 0,
): number[][] {
  const start = Math.max(0, Math.min(ids.length, Math.trunc(aroundIndex) - MEDIA_LOOKUP_ROWS_BEHIND))
  const seen = new Set<number>()
  const missing: number[] = []
  for (const id of [...ids.slice(start), ...ids.slice(0, start)]) {
    if (seen.has(id) || known(id)) continue
    seen.add(id)
    missing.push(id)
  }
  const chunks: number[][] = []
  for (let from = 0; from < missing.length; from += MEDIA_LOOKUP_CHUNK_IDS) {
    chunks.push(missing.slice(from, from + MEDIA_LOOKUP_CHUNK_IDS))
  }
  return chunks
}

/** True when both lists hold the same items in the same order. */
export function sameItems<T>(a: readonly T[], b: readonly T[]): boolean {
  return a.length === b.length && a.every((item, index) => item === b[index])
}
