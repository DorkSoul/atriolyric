import { memo, useEffect, useRef, type RefObject } from 'react'
import { usePlayerActions } from '../PlayerContext'
import { usePlayerSelector } from '../playerSelectors'
import { useRenderProbe } from '../useRenderProbe'
import type { MediaItem } from '../types'
import { Art } from './Art'
import { useConfirm } from '../dialogs'
import { CloseIcon, DragHandleIcon } from './Icons'
import { LibraryLinks } from './LibraryLinks'
import { useDragReorder } from '../useDragReorder'

/** "Show 3 previous" / "Hide 3 previous": already-played songs stay in the
 * queue (so Previous keeps working) but are tucked away until asked for.
 * Renders nothing when nothing has been played yet. */
export function QueuePreviousToggle({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  const currentIndex = usePlayerSelector((player) => player.currentIndex)
  const queueIsEmpty = usePlayerSelector((player) => player.queue.length === 0)
  const previousCount = Math.max(0, currentIndex)
  if (previousCount === 0 || queueIsEmpty) return null
  return (
    <button type="button" className="queue-previous-toggle" onClick={onToggle} aria-expanded={open}>
      {open ? `Hide ${previousCount} previous` : `Show ${previousCount} previous`}
    </button>
  )
}

/**
 * The play queue: the current song (pinned: it cannot be dragged or have
 * anything moved across it), what comes next, and - when `showPrevious` -
 * what has already played. Rows reorder by dragging their handle (pointer
 * events, so touch works too) or by the arrow keys on the focused handle;
 * each can be removed after a confirmation.
 *
 * Rendered by Now Playing and the desktop side panel. `scrollRef` is the
 * scroll area the list sits in: a row dragged near its top or bottom edge
 * scrolls it.
 *
 * Memoised, and subscribed to the queue's own fields only: with up to a few
 * hundred rows it must not re-render because its parent did (each step of a
 * volume drag in Now Playing) or because some other player state moved. Its
 * props must therefore keep their identity between the parent's renders.
 */
export const QueueList = memo(function QueueList({
  onOpenArtist,
  showPrevious,
  scrollRef,
  scrollToCurrent = false,
}: {
  onOpenArtist?: (artist: string) => void
  showPrevious: boolean
  scrollRef?: RefObject<HTMLElement | null>
  /** Bring the current song into view when the list first shows. */
  scrollToCurrent?: boolean
}) {
  const queue = usePlayerSelector((player) => player.queue)
  const queueItemIds = usePlayerSelector((player) => player.queueItemIds)
  const currentIndex = usePlayerSelector((player) => player.currentIndex)
  const queueContextCount = usePlayerSelector((player) => player.queueContextCount)
  const { jumpToQueueIndex, removeFromQueue, reorderQueueItem, moveQueueItem } = usePlayerActions()
  useRenderProbe('queue')
  const confirm = useConfirm()
  const listRef = useRef<HTMLUListElement>(null)
  // A keyboard move is answered by the server's next queue snapshot;
  // this remembers which control to put focus back on (React may have moved or
  // recreated the row's DOM by then) and what to announce.
  const pendingMove = useRef<{ queueItemId: number; from: number; control: 'handle' } | null>(null)
  const announcerRef = useRef<HTMLOutputElement>(null)
  const announcements = useRef(0)
  const previousCount = Math.max(0, currentIndex)
  const firstVisible = showPrevious ? 0 : previousCount

  function moveBy(queueItemId: number | undefined, index: number, direction: -1 | 1, control: 'handle') {
    if (queueItemId == null) return
    pendingMove.current = { queueItemId, from: index, control }
    moveQueueItem(queueItemId, direction)
  }

  // The playing song is pinned: nothing is moved across it (as a drag cannot
  // drop on it), and the ends of the queue have nowhere to go.
  const canMoveUp = (index: number) => index > 0 && index - 1 !== currentIndex
  const canMoveDown = (index: number) => index < queue.length - 1 && index + 1 !== currentIndex

  // A drag stays on its side of the pinned playing song: before it among the
  // played songs, or after it among what comes next.
  const { handleProps, rowProps } = useDragReorder({
    listRef,
    count: queue.length,
    bounds: (from) => (from < currentIndex ? [firstVisible, currentIndex - 1] : [currentIndex + 1, queue.length - 1]),
    onDrop: (from, to) => {
      const queueItemId = queueItemIds[from]
      if (queueItemId != null) reorderQueueItem(queueItemId, to)
    },
    scrollRef,
    orderKey: queueItemIds,
  })

  // After the move arrives: focus stays on the control that was used, which now
  // sits on the moved row, and the new position is announced politely.
  useEffect(() => {
    const pending = pendingMove.current
    if (!pending) return
    // One snapshot answers one move: whether or not it moved the item (a
    // refused move leaves the order alone), the wait is over.
    pendingMove.current = null
    const index = queueItemIds.indexOf(pending.queueItemId)
    if (index < 0 || index === pending.from) return
    listRef.current
      ?.querySelector<HTMLElement>(`[data-queue-item-id="${pending.queueItemId}"] [data-queue-control="${pending.control}"]`)
      ?.focus({ preventScroll: true })
    const shown = queue.length - firstVisible
    const position = index - firstVisible + 1
    // The same text twice in a row is not announced again: alternate a no-break space.
    announcements.current += 1
    if (announcerRef.current) {
      announcerRef.current.textContent = `Moved to position ${position} of ${shown}${announcements.current % 2 ? '' : ' '}`
    }
  }, [queueItemIds, queue.length, firstVisible])

  // Only when the list first shows, not on every later change of the prop.
  const scrollToCurrentOnMount = useRef(scrollToCurrent)
  useEffect(() => {
    if (!scrollToCurrentOnMount.current) return
    const row = listRef.current?.querySelector('.queue-row-current') as HTMLElement | null
    row?.scrollIntoView?.({ block: 'nearest' })
  }, [])

  return (
    <div className="nowplaying-queue">
      {queueContextCount > queue.length && (
        <p className="queue-context-note">
          Showing next {queue.length} of {queueContextCount}
        </p>
      )}
      {queue.length === 0 ? (
        <p className="empty">Queue is empty.</p>
      ) : (
        <>
          <output ref={announcerRef} className="visually-hidden" />
          <ul className="queue-list" ref={listRef}>
            {queue.slice(firstVisible).map((item: MediaItem, offset) => {
              const index = firstVisible + offset
              const queueItemId = queueItemIds[index]
              const isCurrent = index === currentIndex
              const drag = rowProps(index)
              return (
                <li
                  key={queueItemId ?? index}
                  data-index={index}
                  data-reorder-index={drag['data-reorder-index']}
                  data-queue-item-id={queueItemId}
                  style={drag.style}
                  className={`queue-row ${isCurrent ? 'queue-row-current' : ''} ${index < currentIndex ? 'queue-row-played' : ''} ${drag.className}`}
                >
                  {isCurrent ? (
                    <span className="queue-drag-handle queue-drag-handle-pinned" aria-hidden="true" />
                  ) : (
                    <button
                      type="button"
                      className="queue-drag-handle"
                      data-queue-control="handle"
                      {...handleProps(index)}
                      onKeyDown={(e) => {
                        const direction = e.key === 'ArrowUp' ? -1 : e.key === 'ArrowDown' ? 1 : 0
                        if (!direction) return
                        e.preventDefault()
                        if (direction < 0 ? canMoveUp(index) : canMoveDown(index)) moveBy(queueItemId, index, direction, 'handle')
                      }}
                      aria-label={`Reorder ${item.title}`}
                      title="Drag to reorder, or use the Up and Down arrow keys"
                    >
                      <DragHandleIcon />
                    </button>
                  )}
                  <Art mediaItemId={item.id} className="art-xs" thumbnail={item.kind !== 'track'} />
                  {/* The row jumps to the song; its artist line is a link of
                      its own beside the button (links can't nest in one). */}
                  <div className="queue-row-info">
                    <button
                      className="queue-row-jump"
                      onClick={() => queueItemId != null && jumpToQueueIndex(index)}
                    >
                      <span className="track-title">{item.title}</span>
                    </button>
                    <span className="track-subtitle">
                      <LibraryLinks artist={item.artist} artists={item.artists} onArtist={onOpenArtist} />
                    </span>
                  </div>
                  <div className="queue-row-actions">
                    <button
                      onClick={async () => {
                        if (queueItemId == null) return
                        const confirmed = await confirm({
                          title: 'Remove from queue',
                          body: `Remove "${item.title}" from the queue?`,
                          confirmLabel: 'Remove',
                          danger: true,
                        })
                        if (confirmed) removeFromQueue(queueItemId)
                      }}
                      aria-label={`Remove ${item.title} from queue`}
                    >
                      <CloseIcon />
                    </button>
                  </div>
                </li>
              )
            })}
          </ul>
        </>
      )}
    </div>
  )
})
