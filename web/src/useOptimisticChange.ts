import { useOptimistic, useTransition } from 'react'

/** A value the user changes with one press (a heart, a favourite): `change`
 * shows the next value at once and only while `apply` runs. On success the
 * caller's `value` has caught up by the time it ends (`apply` updates it); on
 * failure the shown value goes back to `value` by itself, with nothing to roll
 * back. `busy` is true meanwhile, so a second press can be refused. `apply`
 * reports its own errors; a rejection is swallowed here. */
export function useOptimisticChange<T>(value: T, apply: (next: T) => Promise<unknown>) {
  const [shown, showNext] = useOptimistic(value)
  const [busy, startTransition] = useTransition()

  function change(next: T) {
    startTransition(async () => {
      showNext(next)
      try {
        await apply(next)
      } catch {
        // Reported by the caller.
      }
    })
  }

  return { shown, busy, change }
}
