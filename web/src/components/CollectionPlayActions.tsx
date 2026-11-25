import { api } from '../api'
import { usePlayerActions } from '../PlayerContext'
import { useToast } from '../ToastContext'
import type { PlaySource } from '../types'
import { PlayIcon, ShuffleIcon } from './Icons'

export type CollectionFilter = { artist?: string; artist_is_null?: boolean; album?: string }

/** Starts every track matching `filter` (an artist or album), in order or
 * shuffled. */
export function useCollectionPlayback() {
  const { playLibraryContext, setShuffle } = usePlayerActions()
  const { showToast } = useToast()

  return async function playCollection(
    filter: CollectionFilter,
    playSource: PlaySource | null,
    shuffle: boolean,
    knownStartId?: number,
  ) {
    try {
      let startId = knownStartId
      if (shuffle || startId == null) {
        const page = await api.listTracks({ ...filter, limit: shuffle ? 200 : 1 })
        if (page.length === 0) return
        startId = shuffle ? page[Math.floor(Math.random() * page.length)].id : page[0].id
      }
      setShuffle(shuffle)
      playLibraryContext(filter, startId, undefined, playSource ?? undefined)
      if (playSource) api.recordPlaySource(playSource).catch(() => {})
    } catch {
      showToast("Couldn't start playback.", 'error')
    }
  }
}

/** Play and Shuffle buttons for an artist/album detail hero. */
export function CollectionPlayActions({
  filter,
  playSource,
}: {
  filter: CollectionFilter
  playSource: PlaySource
}) {
  const playCollection = useCollectionPlayback()
  return (
    <>
      <button
        type="button"
        className="btn btn-primary hero-play-btn"
        onClick={() => void playCollection(filter, playSource, false)}
        aria-label="Play"
        title="Play"
      >
        <PlayIcon aria-hidden="true" />
        <span>Play</span>
      </button>
      <button
        type="button"
        className="btn btn-ghost hero-shuffle-btn"
        onClick={() => void playCollection(filter, playSource, true)}
        aria-label="Shuffle play"
        title="Shuffle play"
      >
        <ShuffleIcon aria-hidden="true" />
        <span>Shuffle</span>
      </button>
    </>
  )
}
