import { createContext, useContext } from 'react'

/** What changes on every position tick. Kept out of the main player
 * context so a tick re-renders only what shows the position (the seek bars
 * and the lyrics), not every view that asks the player for something. */
export interface PlayerTime {
  currentTime: number
  duration: number
}

export interface PlayerTimeStore {
  get: () => PlayerTime
  subscribe: (listener: () => void) => () => void
  setCurrentTime: (seconds: number) => void
  setDuration: (seconds: number) => void
}

export function createPlayerTimeStore(): PlayerTimeStore {
  let value: PlayerTime = { currentTime: 0, duration: 0 }
  const listeners = new Set<() => void>()
  function update(next: PlayerTime) {
    value = next
    for (const listener of listeners) listener()
  }
  return {
    get: () => value,
    subscribe: (listener) => {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    // Same bail-out as a state setter: an unchanged value notifies nobody.
    setCurrentTime: (seconds) => {
      if (!Object.is(seconds, value.currentTime)) update({ ...value, currentTime: seconds })
    },
    setDuration: (seconds) => {
      if (!Object.is(seconds, value.duration)) update({ ...value, duration: seconds })
    },
  }
}

/** The provider's time store (see usePlayerTime in PlayerContext). */
export const PlayerTimeContext = createContext<PlayerTimeStore | null>(null)

/** Reads the position and duration when called, for an event handler that
 * needs them (a "back 30 seconds" button): the caller is not re-rendered by
 * a tick. */
export function usePlayerTimeGetter(): () => PlayerTime {
  const store = useContext(PlayerTimeContext)
  if (!store) throw new Error('usePlayerTimeGetter must be used within PlayerProvider')
  return store.get
}
