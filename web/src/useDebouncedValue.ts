import { useEffect, useState } from 'react'

/** Search boxes wait this long after the last keystroke before asking the server. */
export const SEARCH_DEBOUNCE_MS = 250

/** `value`, but only once it has stopped changing for `ms`. An empty value
 * applies at once: clearing a box (or a genre chip resetting it) should not
 * leave the old results up for a quarter of a second. */
export function useDebouncedValue<T extends string>(value: T, ms: number = SEARCH_DEBOUNCE_MS): T {
  const [settled, setSettled] = useState(value)
  // Derived while rendering rather than in an effect, so the cleared state is
  // already what this render uses.
  if (value === '' && settled !== '') setSettled(value)
  useEffect(() => {
    if (value === settled) return
    const timer = window.setTimeout(() => setSettled(value), ms)
    return () => window.clearTimeout(timer)
  }, [value, settled, ms])
  return settled
}
