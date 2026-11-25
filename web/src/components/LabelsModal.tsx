// The genres/tags popup LabelsButton opens; its own file so its code loads
// only when it is first opened.
import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Labels } from '../types'
import { Modal } from './Modal'

export type Target = { media_item_id: number } | { artist: string | null; album?: string | null }

export type Pick = { genre?: string; tag?: string }

function LabelGroup({
  heading,
  items,
  onPick,
}: {
  heading: string
  items: Labels['genres']
  onPick?: (name: string) => void
}) {
  if (items.length === 0) return null
  return (
    <section className="labels-group">
      <h3>{heading}</h3>
      <ul className="labels-chips">
        {items.map((item) => (
          <li key={item.name}>
            <button
              type="button"
              className="labels-chip"
              disabled={!onPick}
              onClick={() => onPick?.(item.name)}
              title={onPick ? `Show songs tagged ${item.name}` : undefined}
            >
              {item.name}
              {item.track_count > 1 && <span className="labels-chip-count">{item.track_count}</span>}
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}

export function LabelsModal({
  title,
  target,
  onClose,
  onPick,
}: {
  title: string
  target: Target
  onClose: () => void
  onPick?: (label: Pick) => void
}) {
  const [labels, setLabels] = useState<Labels | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    api
      .getLabels(target)
      .then((value) => {
        if (!cancelled) setLabels(value)
      })
      .catch(() => {
        if (!cancelled) setFailed(true)
      })
    return () => {
      cancelled = true
    }
    // The target is rebuilt each render; the modal only ever opens for one.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const empty = labels && labels.genres.length === 0 && labels.tags.length === 0
  return (
    <Modal title={title} onClose={onClose} history>
      <div className="labels-body">
        {failed ? (
          <p className="empty">Couldn't load genres and tags.</p>
        ) : !labels ? (
          <p className="loading">Loading…</p>
        ) : empty ? (
          <p className="empty">No genres or tags yet.</p>
        ) : (
          <>
            <LabelGroup heading="Genres" items={labels.genres} onPick={onPick && ((genre) => onPick({ genre }))} />
            <LabelGroup heading="Tags" items={labels.tags} onPick={onPick && ((tag) => onPick({ tag }))} />
          </>
        )}
      </div>
    </Modal>
  )
}
