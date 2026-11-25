import type { MediaItem } from './types'

/** Which info modal to show. Opened from "⋯" menus, which unmount as soon
 * as an entry is picked - so the modal lives in one host (InfoModalHost,
 * mounted once in App) instead of inside whichever menu asked for it. */
export type InfoTarget =
  | { kind: 'artist'; name: string }
  | { kind: 'album'; artist: string | null; album: string }
  | { kind: 'track'; track: MediaItem }

type Listener = (target: InfoTarget) => void

const listeners = new Set<Listener>()

export function openInfo(target: InfoTarget) {
  for (const listener of listeners) listener(target)
}

export function subscribeInfo(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}
