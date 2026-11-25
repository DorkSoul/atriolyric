import { useSyncExternalStore } from 'react'

// "Show missing" on owned artist and album pages: a per-device choice,
// off by default, shared by every page that has the toggle.
const KEY = 'atriolyric.showMissing'
const listeners = new Set<() => void>()

function read(): boolean {
  try {
    return localStorage.getItem(KEY) === '1'
  } catch {
    return false
  }
}

let value = read()

export function setShowMissing(next: boolean) {
  value = next
  try {
    localStorage.setItem(KEY, next ? '1' : '0')
  } catch {
    // Not remembered across reloads, but still applies now.
  }
  listeners.forEach((listener) => listener())
}

export function useShowMissing(): [boolean, (next: boolean) => void] {
  const current = useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    () => value,
  )
  return [current, setShowMissing]
}
