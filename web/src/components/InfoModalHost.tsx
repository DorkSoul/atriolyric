import { Suspense, useEffect, useState } from 'react'
import { subscribeInfo, type InfoTarget } from '../infoModalBus'
import { lazyView } from '../lazyView'
import { OverlayBoundary } from './OverlayBoundary'

// The info dialogs load their code the first time one is opened.
const AlbumInfoModal = lazyView(() => import('./AlbumInfoModal').then((m) => m.AlbumInfoModal))
const ArtistInfoModal = lazyView(() => import('./ArtistInfoModal').then((m) => m.ArtistInfoModal))
const TrackInfoModal = lazyView(() => import('./TrackInfoModal').then((m) => m.TrackInfoModal))

/** Shows whichever info modal a menu asked for (see infoModalBus). */
export function InfoModalHost({ onOpenArtist }: { onOpenArtist: (name: string) => void }) {
  const [target, setTarget] = useState<InfoTarget | null>(null)
  useEffect(() => subscribeInfo(setTarget), [])
  if (!target) return null
  const close = () => setTarget(null)
  return (
    <OverlayBoundary what="that information" onClose={close}>
      <Suspense fallback={null}>{infoModal(target, close, onOpenArtist)}</Suspense>
    </OverlayBoundary>
  )
}

function infoModal(target: InfoTarget, close: () => void, onOpenArtist: (name: string) => void) {
  switch (target.kind) {
    case 'artist':
      // Keyed so following a member link to another artist starts fresh.
      return <ArtistInfoModal key={target.name} name={target.name} onClose={close} onOpenArtist={onOpenArtist} />
    case 'album':
      return <AlbumInfoModal artist={target.artist} album={target.album} onClose={close} />
    case 'track':
      return <TrackInfoModal track={target.track} onClose={close} />
  }
}
