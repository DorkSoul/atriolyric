import { Suspense, useState } from 'react'
import { lazyView } from '../lazyView'
import type { Pick, Target } from './LabelsModal'
import { closeModalThen } from '../modalHistory'

const LabelsModal = lazyView(() => import('./LabelsModal').then((m) => m.LabelsModal))

/** A small "Genres/tags" button that opens a popup listing them for a song,
 * or pooled across an album's or artist's tracks. */
export function LabelsButton({
  title,
  target,
  onPick,
}: {
  title: string
  target: Target
  /** Called with the clicked genre or tag; the popup closes first. */
  onPick?: (label: Pick) => void
}) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" className="labels-btn" onClick={() => setOpen(true)}>
        Genres/tags
      </button>
      {open && (
        <Suspense fallback={null}>
        <LabelsModal
          title={title}
          target={target}
          onClose={() => setOpen(false)}
          onPick={
            onPick &&
            // The popup's history entry goes first: the page the pick opens
            // then follows this one, with no entry left for the popup.
            ((label) => closeModalThen(() => setOpen(false), () => onPick(label)))
          }
        />
        </Suspense>
      )}
    </>
  )
}
