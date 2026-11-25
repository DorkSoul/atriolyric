import { useEffect, useState, useSyncExternalStore } from 'react'
import { onLabelsChange, pendingLabels } from '../networkActivity'
import { usePlayer } from '../PlayerContext'

// Only announce work that is actually slow; fast responses never flash a pill.
const SHOW_AFTER_MS = 700

let labelsSnapshot: string[] = []
function getLabels(): string[] {
  const next = pendingLabels()
  if (next.length === labelsSnapshot.length && next.every((label, i) => label === labelsSnapshot[i])) {
    return labelsSnapshot
  }
  labelsSnapshot = next
  return next
}

/** A small pill naming whatever the server is still taking its time over. */
export function LoadingStatus() {
  const labels = useSyncExternalStore(onLabelsChange, getLabels)
  const { mediaLoading, current, isPlaying, isActiveDevice } = usePlayer()
  const items = [...labels]
  if (mediaLoading && isPlaying && isActiveDevice && current) items.unshift(`"${current.title}"`)
  const message = items.length ? `Loading ${items.join(', ')}…` : null

  // Hold the message back until it has been wanted for SHOW_AFTER_MS.
  const [shown, setShown] = useState<string | null>(null)
  useEffect(() => {
    if (!message) {
      setShown(null)
      return
    }
    if (shown) {
      setShown(message)
      return
    }
    const timer = window.setTimeout(() => setShown(message), SHOW_AFTER_MS)
    return () => window.clearTimeout(timer)
  }, [message, shown])

  if (!shown) return null
  return (
    <div className="loading-status" role="status" aria-live="polite">
      <span className="loading-status-spinner" aria-hidden="true" />
      <span>{shown}</span>
    </div>
  )
}
