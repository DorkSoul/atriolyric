import { useEffect, useRef, useState } from 'react'
import { useArtScrollRoot } from '../artScrollRoot'
import type { MediaItem } from '../types'
import { useVirtualRange } from '../useVirtualRange'
import { useDragReorder } from '../useDragReorder'
import { DragHandleIcon } from './Icons'

// First-paint estimate only: useVirtualRange measures the real row.
const ROW_HEIGHT_ESTIMATE = 49

/** The rows of a playlist being rearranged by a drag handle - dragged with a
 * mouse or a finger, or moved with the arrow keys once focused. It only
 * reports `onMove(from, to)`; the owner saves the order. Focus stays on the
 * handle after its row has moved, and a row that left the rendered window by
 * moving is scrolled back into view first. Only the rows
 * near the viewport are rendered: a 5,000-track playlist would
 * otherwise put 15,000 buttons in the page. */
export function PlaylistReorderList({
  items,
  onMove,
}: {
  items: MediaItem[]
  onMove: (from: number, to: number) => void
}) {
  const [announcement, setAnnouncement] = useState('')
  const focusAfter = useRef<{
    id: number | null | undefined
    control: 'handle'
    scrolled: boolean
  } | null>(null)
  // A node in state, not a ref: see useVirtualRange's containerNode.
  const [listNode, setListNode] = useState<HTMLOListElement | null>(null)
  const scrollRoot = useArtScrollRoot()
  const { start, end, topPad, bottomPad, scrollIndexIntoView } = useVirtualRange(items.length, {
    itemHeight: ROW_HEIGHT_ESTIMATE,
    containerNode: listNode,
    scrollContainerRef: scrollRoot,
  })

  // A node in state for useVirtualRange; a ref mirror for the drag hook.
  const listRef = useRef<HTMLOListElement | null>(null)
  const { handleProps, rowProps } = useDragReorder({
    listRef,
    count: items.length,
    onDrop: (from, to) => move(from, to, 'handle'),
    scrollRef: scrollRoot,
    orderKey: items,
  })

  // After a move: focus the same control in the moved row. A row outside
  // the rendered window is scrolled into view first; the window follows on
  // the scroll and this runs again with the row rendered.
  useEffect(() => {
    const target = focusAfter.current
    if (!target || !listNode) return
    const control = listNode.querySelector<HTMLElement>(
      `[data-reorder-id="${target.id}"] [data-reorder-control="${target.control}"]`,
    )
    if (control) {
      focusAfter.current = null
      control.focus()
      return
    }
    if (target.scrolled) return
    target.scrolled = true
    const index = items.findIndex((item) => item.playlist_item_id === target.id)
    if (index >= 0) scrollIndexIntoView(index)
  }, [items, start, end, listNode, scrollIndexIntoView])

  function move(from: number, to: number, control: 'handle') {
    if (to < 0 || to >= items.length || to === from) return
    const moved = items[from]
    focusAfter.current = { id: moved.playlist_item_id, control, scrolled: false }
    // Said aloud: the row moving is not otherwise announced to a screen reader.
    setAnnouncement(`Moved ${moved.title} to position ${to + 1} of ${items.length}.`)
    onMove(from, to)
  }

  return (
    <>
      <ol className="playlist-reorder-list" ref={(node) => {
          listRef.current = node
          setListNode(node)
        }} aria-label="Tracks, in playlist order">
        {topPad > 0 && <li className="playlist-reorder-spacer" style={{ height: topPad }} aria-hidden />}
        {items.slice(start, end).map((item, offset) => {
          const index = start + offset
          const drag = rowProps(index)
          return (
          <li
            key={item.playlist_item_id ?? item.id}
            data-reorder-id={item.playlist_item_id}
            data-reorder-index={drag['data-reorder-index']}
            // Only a window of the rows is in the page: say where this one is.
            aria-posinset={index + 1}
            aria-setsize={items.length}
            className={`queue-row playlist-reorder-row ${drag.className}`}
            style={drag.style}
          >
            <button
              type="button"
              className="queue-drag-handle"
              data-reorder-control="handle"
              aria-label={`Reorder ${item.title}`}
              title="Drag to reorder, or use the Up and Down arrow keys"
              {...handleProps(index)}
              onKeyDown={(e) => {
                const direction = e.key === 'ArrowUp' ? -1 : e.key === 'ArrowDown' ? 1 : 0
                if (!direction) return
                e.preventDefault()
                move(index, index + direction, 'handle')
              }}
            >
              <DragHandleIcon />
            </button>
            <span className="queue-row-info">
              <span className="track-title">{item.title}</span>
              {/* Always two lines, so every row has the height the window assumes. */}
              {item.artist
                ? <span className="track-subtitle">{item.artist}</span>
                : <span className="track-subtitle" aria-hidden="true">{'\u00a0'}</span>}
            </span>
          </li>
          )
        })}
        {bottomPad > 0 && <li className="playlist-reorder-spacer" style={{ height: bottomPad }} aria-hidden />}
      </ol>
      <div className="visually-hidden" role="status" aria-live="polite">
        {announcement}
      </div>
    </>
  )
}
