// Narrow subscriptions to the player's state.
//
// `usePlayer()` hands a component the whole player value, so it re-renders
// whenever any of it changes: each step of a volume drag, a device heartbeat,
// `mediaLoading`, `reconnecting`. A list that only marks the playing song (or
// a view that needs one or two fields) reads them through `usePlayerSelector`
// instead and is re-rendered by those fields alone; commands come from
// `usePlayerActions()`, which never re-renders its caller.

import { createContext, useContext, useSyncExternalStore } from 'react'
import type { PlayerContextValue } from './PlayerContext'

/** The player value as a store. PlayerProvider publishes each new value to
 * it before paint. */
export interface PlayerValueStore {
  get: () => PlayerContextValue
  set: (value: PlayerContextValue) => void
  subscribe: (listener: () => void) => () => void
}

export function createPlayerValueStore(initial: PlayerContextValue): PlayerValueStore {
  let value = initial
  const listeners = new Set<() => void>()
  return {
    get: () => value,
    set: (next) => {
      if (next === value) return
      value = next
      for (const listener of [...listeners]) listener()
    },
    subscribe: (listener) => {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
  }
}

export const PlayerValueStoreContext = createContext<PlayerValueStore | null>(null)

/** One part of the player's state: re-renders the caller only when what
 * `selector` returns changes (compared with Object.is, so it must return a
 * field or a primitive made from fields, never a new object). */
export function usePlayerSelector<T>(selector: (value: PlayerContextValue) => T): T {
  const store = useContext(PlayerValueStoreContext)
  if (!store) throw new Error('usePlayerSelector must be used within PlayerProvider')
  return useSyncExternalStore(store.subscribe, () => selector(store.get()))
}

const selectCurrentTrackId = (value: PlayerContextValue) => value.current?.id ?? null

/** The id of the current item (null when there is none), for a list that
 * marks the playing song: re-renders the caller only when another item starts. */
export function useCurrentTrackId(): number | null {
  return usePlayerSelector(selectCurrentTrackId)
}
