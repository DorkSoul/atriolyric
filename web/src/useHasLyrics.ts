import { useEffect, useState } from 'react'
import { api } from './api'
import { askUntilSettled } from './usePendingAsync'

// Per-track answer, cached for the session so skipping back and forth (or
// reopening Now Playing) does not refetch. Only a settled answer is cached:
// while the server is still looking (`pending`) the answer is unknown.
const known = new Map<number, boolean>()
// One request per track at a time, shared by every control asking (Now
// Playing and the side panel ask for the same track together).
const inFlight = new Map<number, ReturnType<typeof api.getLyrics>>()

function lookup(trackId: number) {
  let promise = inFlight.get(trackId)
  if (!promise) {
    promise = api.getLyrics(trackId).finally(() => inFlight.delete(trackId))
    inFlight.set(trackId, promise)
  }
  return promise
}

function hasText(result: { plain: string | null; synced: string | null }): boolean {
  return Boolean((result.plain ?? result.synced ?? '').trim())
}

/** Records an answer a lyrics view got itself, so the Lyrics control agrees
 * with what the view shows without asking again. */
export function noteLyrics(trackId: number, result: { pending?: boolean; plain: string | null; synced: string | null }) {
  if (!result.pending) known.set(trackId, hasText(result))
}

/** Whether a track has lyrics: `undefined` while still being checked
 * (including while the server is still looking them up - asked again every
 * few seconds while this track is the one showing). */
export function useHasLyrics(trackId: number | null | undefined, enabled = true): boolean | undefined {
  const [, bump] = useState(0)
  useEffect(() => {
    if (trackId == null || !enabled || known.has(trackId)) return
    const controller = new AbortController()
    askUntilSettled(() => lookup(trackId), controller.signal).then(
      (result) => {
        if (result.pending) return
        known.set(trackId, hasText(result))
        bump((n) => n + 1)
      },
      () => {
        // A failed look-up shows no Lyrics control for this track this
        // session; an abort (the track changed) leaves it unknown.
        if (controller.signal.aborted) return
        known.set(trackId, false)
        bump((n) => n + 1)
      },
    )
    return () => controller.abort()
  }, [trackId, enabled])
  if (trackId == null || !enabled) return undefined
  return known.get(trackId)
}
