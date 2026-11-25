import { useEffect, useRef, useState } from 'react'
import { useVirtualRange } from './useVirtualRange'

interface Options {
  scrollContainerRef: React.RefObject<HTMLElement | null> | null | undefined
  onEndReached?: () => void
  endReachedPx?: number
  /** A tile of this grid, measured for the row height (`.browse-card`). */
  tileSelector?: string
  /** The CSS variable holding the grid's minimum tile width, as its
   * `repeat(auto-fill, minmax(var(...), 1fr))` uses it, and its default. */
  tileSizeVar?: string
  tileSizeDefault?: string
}

// Used only until the first real tile can be measured (a render or two,
// at most) - just needs to be a plausible tile height so the very first
// paint doesn't wildly over- or under-render.
const INITIAL_ROW_HEIGHT_ESTIMATE = 160

/** The grid equivalent of useVirtualRange: renders only the rows near the
 * viewport instead of the whole grid. The CSS itself decides how many
 * tiles fit per row (`.browse-grid`'s `repeat(auto-fill, minmax(...))`),
 * which can change with the tile-size setting or the window width, so
 * this measures the actual rendered column count and tile height from the
 * DOM rather than assuming a fixed grid shape, then delegates the actual
 * windowing math to useVirtualRange by treating one grid row as one
 * "item" there. */
export function useVirtualGridRange(itemCount: number, options: Options) {
  const {
    scrollContainerRef,
    onEndReached,
    endReachedPx,
    tileSelector = '.browse-card',
    tileSizeVar = '--browse-tile-size',
    tileSizeDefault = '7rem',
  } = options
  // The node itself (not a ref object) - see the comment in
  // useVirtualRange.ts on why this needs to be real React state instead of
  // a plain useRef: a ref's identity survives an unmount/remount even
  // though the DOM node it points to doesn't, so an effect keyed on the
  // ref object would silently keep measuring a stale, detached container
  // after navigating away and back (or any other remount).
  const [containerNode, setContainerNode] = useState<HTMLElement | null>(null)
  const [columns, setColumns] = useState(1)
  const [rowHeight, setRowHeight] = useState(INITIAL_ROW_HEIGHT_ESTIMATE)
  const [rowGap, setRowGap] = useState(0)
  const measuredOnceRef = useRef(false)

  useEffect(() => {
    const container = containerNode
    if (!container) return

    function measure() {
      const node = container!
      const style = getComputedStyle(node)
      const rootStyle = getComputedStyle(document.documentElement)
      const rootFontSizePx = parseFloat(rootStyle.fontSize) || 16
      const toPx = (raw: string): number =>
        raw.trim().endsWith('rem') ? parseFloat(raw) * rootFontSizePx : parseFloat(raw) || 0

      const columnGapPx = toPx(style.columnGap || style.gap || '0')
      const tileMinPx = toPx(rootStyle.getPropertyValue(tileSizeVar) || tileSizeDefault) || 100
      // auto-fill lays tracks out in the content box - clientWidth also
      // counts the grid's own padding, which can be enough to squeeze in
      // one more column here than CSS actually renders (7 vs 6 on a 1200px
      // window), so every spacer would be sized for the wrong row count.
      const width = node.clientWidth - toPx(style.paddingLeft) - toPx(style.paddingRight)
      const cols = Math.max(1, Math.floor((width + columnGapPx) / (tileMinPx + columnGapPx)))
      setColumns(cols)

      const tile = node.querySelector(tileSelector) as HTMLElement | null
      if (tile) {
        const rowGapPx = toPx(style.rowGap || style.gap || '0')
        setRowHeight(tile.getBoundingClientRect().height + rowGapPx)
        setRowGap(rowGapPx)
        measuredOnceRef.current = true
      }
    }

    measure()
    // A tile only exists to measure after the first item-count-driven
    // render - once one shows up, re-measure so the estimate is replaced
    // with the real height instead of sticking with the initial guess.
    if (!measuredOnceRef.current) requestAnimationFrame(measure)

    const observer = new ResizeObserver(measure)
    observer.observe(container)
    return () => observer.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [containerNode, itemCount])

  const rowCount = Math.ceil(itemCount / columns)
  const {
    start: rowStart,
    end: rowEnd,
    topPad,
    bottomPad,
    scrollToIndex: scrollToRow,
  } = useVirtualRange(rowCount, {
    itemHeight: rowHeight,
    measureItems: false,
    containerNode,
    scrollContainerRef,
    onEndReached,
    endReachedPx,
  })

  return {
    start: rowStart * columns,
    end: Math.min(itemCount, rowEnd * columns),
    // Each spacer is a grid row of its own, so the grid puts a row gap
    // next to it that the rows it replaces already count in rowHeight -
    // take it back out so the spacer plus its gap equals those rows.
    topPad: topPad > 0 ? Math.max(0, topPad - rowGap) : 0,
    bottomPad: bottomPad > 0 ? Math.max(0, bottomPad - rowGap) : 0,
    // Attach this to the grid element's `ref` prop instead of creating a
    // separate useRef - see the comment above on why.
    setContainerNode,
    // Used by the A-Z jump bar - one grid row is one "item" to the
    // underlying useVirtualRange, so convert the tile index to a row index.
    scrollToIndex: (index: number, offsetPx = 0) => scrollToRow(Math.floor(index / columns), offsetPx),
  }
}
