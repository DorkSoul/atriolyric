import { useEffect, useEffectEvent, useLayoutEffect, useState } from 'react'
import { scrollBehavior } from './motion'

interface Options {
  // A starting estimate only - the real height is measured from the first
  // rendered row (see measureItems) and replaces it as soon as one exists.
  itemHeight: number
  // Grids pass false: their "item" is a whole row of tiles plus the row
  // gap, which no single child element's height represents, so
  // useVirtualGridRange measures it itself and passes it in exactly.
  measureItems?: boolean
  // The actual DOM node (not a ref object) that wraps the rendered items -
  // takes the *value* rather than a React.RefObject deliberately: a ref
  // object's identity never changes even when the node it points to is
  // unmounted and replaced (e.g. navigating away and back, or a
  // loading-state swap), so an effect keyed on the ref object alone would
  // silently keep measuring a stale, detached node after a remount. Keying
  // on the node value itself means any real remount is a genuine
  // dependency change, so the effect always re-attaches correctly. Pass
  // this from a `useState<HTMLElement | null>` updated via a callback ref.
  containerNode: HTMLElement | null
  // null/undefined means "no known scroll ancestor" - used by small,
  // inherently-bounded lists (e.g. one album's tracks) that don't need
  // windowing at all, so they just render everything instead of getting
  // stuck at the first-30 window with nothing driving it forward.
  scrollContainerRef: React.RefObject<HTMLElement | null> | null | undefined
  overscan?: number
  onEndReached?: () => void
  endReachedPx?: number
}

/** Renders only the rows near the viewport (plus a small overscan buffer)
 * instead of the whole list, re-measuring on scroll of the given ancestor.
 * Keeps the DOM - and each row's own image request - bounded no matter how
 * far a list has been paged in, since rendering every row of a
 * multi-thousand-track library at once makes the Songs tab (and the
 * whole page, on mobile) lock up and stop responding to taps. */
export function useVirtualRange(itemCount: number, options: Options) {
  const {
    itemHeight: estimatedItemHeight,
    measureItems = true,
    containerNode,
    scrollContainerRef,
    overscan = 8,
    onEndReached,
    endReachedPx = 800,
  } = options
  const virtualize = scrollContainerRef != null
  const [range, setRange] = useState({ start: 0, end: virtualize ? Math.min(itemCount, 30) : itemCount })
  // Reads the latest `onEndReached` from inside the scroll listener without
  // making a new callback re-subscribe it.
  const reachEnd = useEffectEvent((scrollNode: HTMLElement) => {
    if (!onEndReached) return
    const distanceToBottom = scrollNode.scrollHeight - (scrollNode.scrollTop + scrollNode.clientHeight)
    if (distanceToBottom < endReachedPx) onEndReached()
  })

  // The spacers stand in for every row outside the window, so their height
  // has to match the real rows exactly: an estimate that's off by even 3px
  // grows the error by that much per row scrolled past, and the content
  // under the viewport visibly shifts each time the window moves. The
  // browser's scroll anchoring then "corrects" scrollTop for that shift,
  // which fires another scroll and another shift - the jumping up and down
  // on a fast scroll far down a long list. So measure the real row instead
  // of trusting padding/font-size arithmetic. The container's
  // ResizeObserver catches the first rows arriving and any appearance
  // change that resizes them.
  const [measuredItemHeight, setMeasuredItemHeight] = useState<number | null>(null)
  useLayoutEffect(() => {
    if (!measureItems || !containerNode) return
    const container = containerNode

    function measure() {
      const row = container.querySelector(':scope > :not([aria-hidden])')
      if (!row) return
      const height = row.getBoundingClientRect().height
      if (height <= 0) return
      setMeasuredItemHeight((prev) => (prev !== null && Math.abs(prev - height) < 0.01 ? prev : height))
    }

    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(container)
    return () => observer.disconnect()
  }, [measureItems, containerNode])
  const itemHeight = (measureItems && measuredItemHeight) || estimatedItemHeight

  useEffect(() => {
    if (!virtualize) return
    const scrollEl = scrollContainerRef.current
    const container = containerNode
    if (!scrollEl || !container) return

    function update() {
      const scrollNode = scrollEl!
      const containerNode = container!
      const containerRect = containerNode.getBoundingClientRect()
      const scrollRect = scrollNode.getBoundingClientRect()
      const containerTop = containerRect.top - scrollRect.top + scrollNode.scrollTop

      const relativeTop = Math.max(0, scrollNode.scrollTop - containerTop)
      // Clamped: with content after the list (the Files tab's files after
      // its folders), the viewport can be far below the list's end, and a
      // start past the last item would make the top spacer taller than the
      // whole list.
      const start = Math.min(itemCount, Math.max(0, Math.floor(relativeTop / itemHeight) - overscan))
      const visibleRows = Math.ceil(scrollNode.clientHeight / itemHeight) + overscan * 2
      const end = Math.min(itemCount, start + visibleRows)
      setRange((prev) => (prev.start === start && prev.end === end ? prev : { start, end }))

      reachEnd(scrollNode)
    }

    update()
    scrollEl.addEventListener('scroll', update, { passive: true })
    window.addEventListener('resize', update)
    return () => {
      scrollEl.removeEventListener('scroll', update)
      window.removeEventListener('resize', update)
    }
  }, [itemCount, itemHeight, overscan, endReachedPx, containerNode, scrollContainerRef, virtualize])

  // Used by the A-Z jump bar - scrolls so the item at `index` lands at the
  // top of the viewport (or `offsetPx` below it, under a sticky row).
  // Computed fresh from the live DOM rather than cached, same positioning
  // math `update()` uses above.
  function scrollToIndex(index: number, offsetPx = 0) {
    const scrollEl = scrollContainerRef?.current
    const container = containerNode
    if (!scrollEl || !container) return
    const containerRect = container.getBoundingClientRect()
    const scrollRect = scrollEl.getBoundingClientRect()
    const containerTop = containerRect.top - scrollRect.top + scrollEl.scrollTop
    scrollEl.scrollTo({ top: Math.max(0, containerTop + index * itemHeight - offsetPx), behavior: scrollBehavior() })
  }

  // Scrolls (instantly, and only as far as needed) so the item at `index`
  // is inside the viewport - for keeping a row in view after it moved, when
  // it may have left the rendered window. The window follows on the scroll.
  function scrollIndexIntoView(index: number) {
    const scrollEl = scrollContainerRef?.current
    const container = containerNode
    if (!scrollEl || !container) return
    const containerRect = container.getBoundingClientRect()
    const scrollRect = scrollEl.getBoundingClientRect()
    const top = containerRect.top - scrollRect.top + scrollEl.scrollTop + index * itemHeight
    const viewTop = scrollEl.scrollTop
    const viewBottom = viewTop + scrollEl.clientHeight
    if (top < viewTop) scrollEl.scrollTo({ top, behavior: 'instant' })
    else if (top + itemHeight > viewBottom) {
      scrollEl.scrollTo({ top: top + itemHeight - scrollEl.clientHeight, behavior: 'instant' })
    }
  }

  const visibleRange = virtualize ? range : { start: 0, end: itemCount }
  return {
    start: visibleRange.start,
    end: Math.max(visibleRange.start, visibleRange.end),
    topPad: visibleRange.start * itemHeight,
    bottomPad: Math.max(0, (itemCount - visibleRange.end) * itemHeight),
    scrollToIndex,
    scrollIndexIntoView,
  }
}
