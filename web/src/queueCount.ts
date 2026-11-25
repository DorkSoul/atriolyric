import { usePlayer } from './PlayerContext'

/** "6 of 42": the songs after the current one, and the whole context's size
 * when the queue is a window onto a bigger one. */
export function useQueueCountLabel(): string {
  const { queue, currentIndex, queueContextCount } = usePlayer()
  const upcoming = Math.max(0, queue.length - Math.max(0, currentIndex) - 1)
  return queueContextCount > queue.length ? `${upcoming} of ${queueContextCount}` : `${upcoming}`
}
