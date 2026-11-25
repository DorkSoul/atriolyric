import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type RefObject } from 'react'

// Pixels from a scroll edge inside which a dragged row auto-scrolls it.
const DRAG_SCROLL_EDGE = 56
const DRAG_SCROLL_MAX_STEP = 16
// After a drop the rows hold their dropped-into place until the new order
// arrives (a server round trip for the queue); this caps that wait.
const SETTLE_TIMEOUT_MS = 1000

interface Options {
  /** The element whose rows carry `data-reorder-index` (from `rowProps`). */
  listRef: RefObject<HTMLElement | null>
  count: number
  /** The indices `from` may land on; defaults to the whole list. */
  bounds?: (from: number) => [number, number]
  onDrop: (from: number, to: number) => void
  /** An outer scroll area to auto-scroll when the row is held at its edge. */
  scrollRef?: RefObject<HTMLElement | null> | null
  /** Changes when the list's order does: ends the post-drop hold. */
  orderKey?: unknown
}

interface DragState {
  from: number
  over: number
  dy: number
  rowHeight: number
  settling: boolean
}

function edgeStep(position: number, start: number, end: number): number {
  if (position < start + DRAG_SCROLL_EDGE) {
    return -Math.ceil(DRAG_SCROLL_MAX_STEP * Math.min(1, (start + DRAG_SCROLL_EDGE - position) / DRAG_SCROLL_EDGE))
  }
  if (position > end - DRAG_SCROLL_EDGE) {
    return Math.ceil(DRAG_SCROLL_MAX_STEP * Math.min(1, (position - (end - DRAG_SCROLL_EDGE)) / DRAG_SCROLL_EDGE))
  }
  return 0
}

/**
 * Drag-to-reorder by a row's handle, the same with a mouse and a finger:
 * Pointer Events (native HTML5 drag-and-drop never fires from touch on
 * mobile Safari/Chrome). The dragged row turns see-through and follows the
 * pointer while the rows it passes slide aside, so the gap it would drop
 * into opens in real time.
 *
 * The landing index comes from the pointer's distance travelled against row
 * midpoints snapshotted at the start - not from hit-testing, since the rows
 * under the pointer are themselves moving. Rows outside a virtualised
 * window are extrapolated at the dragged row's height. Move/up listeners go
 * on the window, so a handle the virtualiser unmounts mid-drag (auto-scroll)
 * does not strand the drag.
 */
export function useDragReorder({ listRef, count, bounds, onDrop, scrollRef, orderKey }: Options) {
  const [drag, setDrag] = useState<DragState | null>(null)
  const dragRef = useRef<DragState | null>(null)
  const latest = useRef({ count, bounds, onDrop, scrollRef })
  // Read by the window listeners, which outlive the render that added them.
  useLayoutEffect(() => {
    latest.current = { count, bounds, onDrop, scrollRef }
  })
  const session = useRef<{
    startContentY: number
    startCenter: number
    pointerY: number
    min: number
    max: number
    frame: number | null
    cleanup: () => void
  } | null>(null)
  const settleTimer = useRef<number | null>(null)
  // Row midpoints from the last drag's start, kept through the post-drop hold.
  const midsRef = useRef<Map<number, number>>(new Map())

  function update(next: DragState | null) {
    dragRef.current = next
    setDrag(next)
  }

  // Pointer Y in the list's own content coordinates, so scrolling the list
  // or its outer scroll area counts as movement through the rows.
  function contentY(clientY: number) {
    const list = listRef.current
    if (!list) return clientY
    return clientY - list.getBoundingClientRect().top + list.scrollTop
  }

  function midOf(index: number, h: number) {
    const mids = midsRef.current
    const known = mids.get(index)
    if (known != null) return known
    // Outside the rendered window: extrapolate from the nearest known row.
    let nearest: number | null = null
    for (const k of mids.keys()) if (nearest == null || Math.abs(k - index) < Math.abs(nearest - index)) nearest = k
    return nearest == null ? index * h : mids.get(nearest)! + (index - nearest) * h
  }

  function recompute() {
    const s = session.current
    const d = dragRef.current
    if (!s || !d || d.settling) return
    const dy = contentY(s.pointerY) - s.startContentY
    const center = s.startCenter + dy
    let over = d.from
    while (over < s.max && midOf(over + 1, d.rowHeight) < center) over += 1
    while (over > s.min && midOf(over - 1, d.rowHeight) > center) over -= 1
    if (over !== d.over || dy !== d.dy) update({ ...d, over, dy })
  }

  function tick() {
    const s = session.current
    if (!s) return
    const list = listRef.current
    // The list itself scrolls only where a stylesheet caps its height.
    if (list && list.scrollHeight > list.clientHeight + 1) {
      const rect = list.getBoundingClientRect()
      const step = edgeStep(s.pointerY, rect.top, rect.bottom)
      if (step) list.scrollBy(0, step)
    }
    const scroller = latest.current.scrollRef?.current
    if (scroller) {
      const rect = scroller.getBoundingClientRect()
      const step = edgeStep(s.pointerY, Math.max(0, rect.top), Math.min(window.innerHeight, rect.bottom))
      if (step) scroller.scrollBy(0, step)
    }
    recompute()
    s.frame = window.requestAnimationFrame(tick)
  }

  function end(commit: boolean) {
    const s = session.current
    const d = dragRef.current
    if (!s) return
    s.cleanup()
    if (s.frame != null) window.cancelAnimationFrame(s.frame)
    session.current = null
    if (!d || !commit || d.over === d.from) {
      update(null)
      return
    }
    // Hold the rows where they were dropped (no transition) until the new
    // order renders, so nothing slides back and then jumps forward.
    const landed = midOf(d.over, d.rowHeight) - midOf(d.from, d.rowHeight)
    update({ ...d, dy: landed, settling: true })
    if (settleTimer.current != null) window.clearTimeout(settleTimer.current)
    settleTimer.current = window.setTimeout(() => update(null), SETTLE_TIMEOUT_MS)
    latest.current.onDrop(d.from, d.over)
  }

  // The new order is in: the rows are already where it puts them. A layout
  // effect, so the new order never paints with the old transforms on it.
  useLayoutEffect(() => {
    if (!dragRef.current?.settling) return
    if (settleTimer.current != null) window.clearTimeout(settleTimer.current)
    update(null)
  }, [orderKey])

  useEffect(
    () => () => {
      session.current?.cleanup()
      if (session.current?.frame != null) window.cancelAnimationFrame(session.current.frame)
      if (settleTimer.current != null) window.clearTimeout(settleTimer.current)
    },
    [],
  )

  function start(e: React.PointerEvent, index: number) {
    if (e.button !== 0 || session.current) return
    const list = listRef.current
    if (!list) return
    // No text selection, and no focus-scroll jump from the press.
    e.preventDefault()
    const listTop = list.getBoundingClientRect().top - list.scrollTop
    const mids = new Map<number, number>()
    let rowHeight = 0
    list.querySelectorAll<HTMLElement>('[data-reorder-index]').forEach((row) => {
      const i = Number(row.dataset.reorderIndex)
      const rect = row.getBoundingClientRect()
      mids.set(i, rect.top - listTop + rect.height / 2)
      if (i === index) rowHeight = rect.height
    })
    if (!mids.has(index)) return
    midsRef.current = mids
    const [min, max] = latest.current.bounds?.(index) ?? [0, latest.current.count - 1]
    const onMove = (ev: PointerEvent) => {
      if (!session.current) return
      session.current.pointerY = ev.clientY
      recompute()
    }
    const onUp = () => end(true)
    const onCancel = () => end(false)
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === 'Escape') end(false)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onCancel)
    window.addEventListener('keydown', onKey)
    if (settleTimer.current != null) window.clearTimeout(settleTimer.current)
    session.current = {
      startContentY: contentY(e.clientY),
      startCenter: mids.get(index)!,
      pointerY: e.clientY,
      min,
      max,
      frame: null,
      cleanup: () => {
        window.removeEventListener('pointermove', onMove)
        window.removeEventListener('pointerup', onUp)
        window.removeEventListener('pointercancel', onCancel)
        window.removeEventListener('keydown', onKey)
      },
    }
    update({ from: index, over: index, dy: 0, rowHeight, settling: false })
    session.current.frame = window.requestAnimationFrame(tick)
  }

  /** Spread on the drag handle (keyboard handling stays the caller's). */
  function handleProps(index: number) {
    return { onPointerDown: (e: React.PointerEvent) => start(e, index) }
  }

  /** Spread on each row: marks it for measuring and moves it while dragging. */
  function rowProps(index: number): { 'data-reorder-index': number; className: string; style?: CSSProperties } {
    const base = { 'data-reorder-index': index }
    if (!drag) return { ...base, className: '' }
    const { from, over, dy, rowHeight, settling } = drag
    if (index === from) {
      return {
        ...base,
        className: `reorder-dragging${settling ? ' reorder-settling' : ''}`,
        style: { transform: `translateY(${dy}px)` },
      }
    }
    // Each passed row slides into its neighbour's slot - measured, so gaps
    // and headings between rows are kept.
    let shift = 0
    if (from < over && index > from && index <= over) shift = midOf(index - 1, rowHeight) - midOf(index, rowHeight)
    else if (from > over && index < from && index >= over) shift = midOf(index + 1, rowHeight) - midOf(index, rowHeight)
    return {
      ...base,
      className: settling ? 'reorder-shifting reorder-settling' : 'reorder-shifting',
      style: shift ? { transform: `translateY(${shift}px)` } : undefined,
    }
  }

  return { dragging: drag != null && !drag.settling, fromIndex: drag?.from ?? null, overIndex: drag?.over ?? null, handleProps, rowProps }
}
