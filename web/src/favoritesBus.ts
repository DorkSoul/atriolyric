import type { PreferenceState } from './types'

/** Liking/disliking a track happens from several independent,
 * simultaneously-mounted places (a track list row, the full-screen Now
 * Playing heart) that each hold their own local copy of the song's state -
 * there's no shared store. This tiny pub/sub lets whichever one made the
 * change broadcast it, so every other place showing that same track updates
 * instantly instead of only on next fetch/refresh. */
type Listener = (mediaItemId: number, state: PreferenceState) => void

const listeners = new Set<Listener>()

export function subscribePreferenceChange(fn: Listener): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function publishPreferenceChange(mediaItemId: number, state: PreferenceState): void {
  for (const fn of listeners) fn(mediaItemId, state)
}
