import { useCallback, useEffect, useRef } from 'react'

/** How long a finger must stay down before it counts as a long press. */
export const LONG_PRESS_MS = 500

/** One long-press implementation for every row/card that opens a menu on a
 * held touch. Wire `start` to pointerdown and `cancel` to pointerup/leave/
 * cancel; the click that follows a fired long press must be swallowed with
 * `consumeClick()` or it would also activate whatever was pressed. The press
 * point is captured at pointerdown (React recycles nothing, but the timer
 * outlives the event). */
export function useLongPress<T = void>(onLongPress: (item: T, point: { x: number; y: number }) => void) {
  const timer = useRef<number | null>(null)
  const fired = useRef(false)
  const handler = useRef(onLongPress)
  useEffect(() => {
    handler.current = onLongPress
  })

  const cancel = useCallback(() => {
    if (timer.current != null) window.clearTimeout(timer.current)
    timer.current = null
  }, [])

  const start = useCallback((event: React.PointerEvent, item: T) => {
    fired.current = false
    const point = { x: event.clientX, y: event.clientY }
    timer.current = window.setTimeout(() => {
      timer.current = null
      fired.current = true
      handler.current(item, point)
    }, LONG_PRESS_MS)
  }, [])

  const consumeClick = useCallback(() => {
    if (!fired.current) return false
    fired.current = false
    return true
  }, [])

  useEffect(() => cancel, [cancel])

  return { start, cancel, consumeClick }
}
