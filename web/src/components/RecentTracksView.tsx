import { api } from '../api'
import { errorMessage } from '../errorMessage'
import type { Playlist } from '../types'
import { KEPT_TAB_STALE_MS, useAsync } from '../useAsync'
import { useCurrentTrackId } from '../playerSelectors'
import { ErrorState, LoadingState } from './ErrorState'
import { TrackList } from './TrackList'

interface Props {
  kind: 'recent-played'
  playlists: Playlist[]
  scrollContainerRef: React.RefObject<HTMLElement | null>
  onPlaylistsChanged: () => void
  onOpenArtist: (artist: string) => void
  onOpenAlbum: (artist: string | null, album: string) => void
}

/** "Recently played" (opened from History on Home): always small
 * (server-capped), so fetched in one shot rather than paged like Songs. Shown
 * as the Songs table on a desktop-width screen. */
export function RecentTracksView({ kind, playlists, scrollContainerRef, onPlaylistsChanged, onOpenArtist, onOpenAlbum }: Props) {
  // A kept-mounted tab: shown again soon after, it keeps the list it has -
  // unless another song has started since, which belongs at the top of it.
  const playingId = useCurrentTrackId()
  const recent = useAsync(() => api.recentlyPlayed(), [kind], { staleMs: KEPT_TAB_STALE_MS, staleKey: playingId })

  if (recent.data === undefined) {
    if (recent.status === 'error') {
      return <ErrorState message={errorMessage(recent.error, 'Could not load these tracks.')} onRetry={recent.retry} />
    }
    return <LoadingState />
  }
  if (recent.data.length === 0) return <p className="empty">Nothing here yet.</p>
  return (
    <div className="songs-view recent-view">
      <p className="page-note">Every song you played lately, newest first.</p>
      <TrackList
      layout="table"
      tracks={recent.data}
      playlists={playlists}
      onChanged={recent.reload}
      scrollContainerRef={scrollContainerRef}
      onPlaylistsChanged={onPlaylistsChanged}
      onOpenArtist={onOpenArtist}
      onOpenAlbum={onOpenAlbum}
      />
    </div>
  )
}
