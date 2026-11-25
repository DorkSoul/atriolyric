import { Suspense, useSyncExternalStore } from 'react'
import type { ArtRef } from './artEntity'
import { lazyView } from './lazyView'
import { OverlayBoundary } from './components/OverlayBoundary'

// Opened on demand (with its want-list styles): its code loads when first used.
const ArtPickerModal = lazyView(() => import('./components/ArtPickerModal').then((m) => m.ArtPickerModal))

// The picker outlives the "⋯" menu that opens it (menus unmount on close),
// so it is hosted once at the app root and opened through this store.
let open: { art: ArtRef; title: string } | null = null
const listeners = new Set<() => void>()

function emit() {
  listeners.forEach((listener) => listener())
}

export function openArtPicker(art: ArtRef, title: string) {
  open = { art, title }
  emit()
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

function closeArtPicker() {
  open = null
  emit()
}

export function ArtPickerHost() {
  const current = useSyncExternalStore(subscribe, () => open)
  if (!current) return null
  return (
    <OverlayBoundary what="the artwork picker" onClose={closeArtPicker}>
    <Suspense fallback={null}>
    <ArtPickerModal
      key={`${current.art.type}:${current.art.key}`}
      art={current.art}
      title={current.title}
      onClose={closeArtPicker}
    />
    </Suspense>
    </OverlayBoundary>
  )
}
