import { useState, type ReactNode } from 'react'
import { api } from '../api'
import { formatCompact } from '../infoText'
import { usePlayerActions } from '../PlayerContext'
import { useShowMissing } from '../showMissing'
import type { ArtistTopTrack, MediaItem, PlaySource } from '../types'
import { wantApi } from '../wantList'
import { WantButton } from './Catalog'
import { usePendingAsync } from '../usePendingAsync'

const COLLAPSED_COUNT = 5

// Artists that came back with no top tracks (no MusicBrainz ID, or nothing
// on ListenBrainz) in this session: their page shows no placeholder next
// time, so nothing is reserved only to collapse again.
const knownEmpty = new Set<string>()

/** One ranked row. Owned rows play; missing ones are greyed out, with
 * `action` left for per-row buttons (e.g. adding it to a want list). */
export function TopTrackRow({
  track,
  onPlay,
  action,
}: {
  track: ArtistTopTrack
  onPlay?: () => void
  action?: ReactNode
}) {
  const missing = track.media_item == null
  const content = (
    <>
      <span className="top-track-rank">{track.rank}</span>
      <span className="top-track-title">{track.media_item?.title ?? track.title}</span>
      {track.playcount != null && (
        <span className="top-track-plays" title={`${track.playcount.toLocaleString()} plays on ListenBrainz`}>
          {formatCompact(track.playcount)}
        </span>
      )}
    </>
  )
  return (
    <li className={missing ? 'top-track-row is-missing' : 'top-track-row'}>
      {missing || !onPlay ? (
        <div className="top-track-main" aria-disabled={missing ? 'true' : undefined} title={missing ? 'Not in your library' : undefined}>
          {content}
        </div>
      ) : (
        <button type="button" className="top-track-main" onClick={onPlay} aria-label={`Play ${track.media_item?.title ?? track.title}`}>
          {content}
        </button>
      )}
      {action}
    </li>
  )
}

/** The artist page's "Top tracks": ListenBrainz's most listened songs by the
 * artist, the owned ones playable. Songs not in the library are hidden
 * unless "Show missing" is on (remembered per device). Keyed by artist
 * by its parent, so a new artist starts collapsed. */
export function TopTracks({ artist }: { artist: string }) {
  const { playQueue } = usePlayerActions()
  // ListenBrainz may still be fetching them (`pending`, no tracks yet): asked
  // again until it answers, the space held meanwhile.
  const state = usePendingAsync(() => api.artistTopTracks(artist, 20), [artist])
  const result = state.data
  const tracks: ArtistTopTrack[] | null =
    state.status === 'error' ? [] : result === undefined ? null : result.available ? result.tracks : []
  const waiting = tracks === null || (state.fetchingMore && tracks.length === 0)
  if (!waiting && tracks.length === 0 && state.status !== 'error') knownEmpty.add(artist)
  if (!waiting && tracks.length > 0) knownEmpty.delete(artist)
  const [expanded, setExpanded] = useState(false)
  // The artist page's one "Show missing" switch, in the toolbar above (it
  // also shows the albums this artist is missing).
  const [showMissing] = useShowMissing()
  const [wanted, setWanted] = useState<Set<string>>(new Set())

  if (waiting && knownEmpty.has(artist)) return null
  if (waiting) {
    // The collapsed list's height, held so the albums below do not jump down
    // when the tracks arrive. Not a region yet: there may be nothing to show.
    return (
      <div className="top-tracks top-tracks-placeholder" aria-busy="true">
        <div className="top-tracks-header">
          <h3 className="artist-section-heading">Top tracks</h3>
        </div>
        <p className="fetching-more top-tracks-placeholder-note" role="status">
          Loading top tracks…
        </p>
      </div>
    )
  }
  if (!tracks || tracks.length === 0) return null

  const shown = showMissing ? tracks : tracks.filter((track) => track.media_item != null)
  const visible = expanded ? shown : shown.slice(0, COLLAPSED_COUNT)
  // Playing a row queues every owned song in the list, in rank order.
  const owned = shown.flatMap((track) => (track.media_item ? [track.media_item] : []))

  function play(item: MediaItem) {
    const source: PlaySource = { kind: 'artist', label: artist, payload: { artist } }
    playQueue(owned, owned.indexOf(item), source)
    api.recordPlaySource(source).catch(() => {})
  }

  return (
    <section className="top-tracks" aria-label="Top tracks">
      <div className="top-tracks-header">
        <h3 className="artist-section-heading">Top tracks</h3>
      </div>
      {shown.length === 0 ? (
        <p className="top-tracks-empty">None of this artist's top tracks are in your library.</p>
      ) : (
        <ol className="top-tracks-list">
          {visible.map((track) => {
            const item = track.media_item
            return (
              <TopTrackRow
                key={`${track.rank}:${track.title}`}
                track={track}
                onPlay={item ? () => play(item) : undefined}
                action={
                  item ? undefined : (
                    <WantButton
                      compact
                      wanted={wanted.has(track.title)}
                      onAdd={() =>
                        wantApi
                          .add({ kind: 'track', artist, title: track.title, recording_mbid: track.mbid })
                          .then(() => setWanted((current) => new Set(current).add(track.title)))
                      }
                    />
                  )
                }
              />
            )
          })}
        </ol>
      )}
      {shown.length > COLLAPSED_COUNT && (
        <button type="button" className="top-tracks-more" onClick={() => setExpanded((value) => !value)}>
          {expanded ? 'View less' : 'View more'}
        </button>
      )}
    </section>
  )
}
