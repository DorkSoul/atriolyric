// Catalog pieces shared by the want list and the library's "Show missing"
// views (kept apart from WantListView so that view can load on demand).
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { formatDuration } from '../formatDuration'
import { useToast } from '../ToastContext'
import { catalogArtUrl, errorMessage, statusLabel, year } from '../catalogHelpers'
import { wantApi, type CatalogAlbum, type CatalogReleaseGroup, type WantInput } from '../wantList'
import './wantList.css'
import { CheckIcon, DiscIcon, MicIcon } from './Icons'
import { PlusIcon } from './BrowseIcons'

export function CatalogCover({
  mbid,
  kind = 'album',
  className = 'art-tile',
}: {
  mbid: string
  kind?: 'artist' | 'album'
  className?: string
}) {
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  // The server answers 204 (an error here) while it fetches the art in the
  // background: ask again after its 30 s cache window, three times at most.
  useEffect(() => {
    if (!failed || attempt >= 3) return
    const timer = window.setTimeout(() => {
      setAttempt((n) => n + 1)
      setFailed(false)
    }, 30_000)
    return () => window.clearTimeout(timer)
  }, [failed, attempt])
  // The same box as <Art>, so sizes and the fallback match library tiles.
  return (
    <div className={`art-wrap ${className}`}>
      {!failed && (
        <img className="art-img" src={attempt ? `${catalogArtUrl(kind, mbid)}${catalogArtUrl(kind, mbid).includes('?') ? '&' : '?'}retry=${attempt}` : catalogArtUrl(kind, mbid)} alt="" loading="lazy" onError={() => setFailed(true)} />
      )}
      {failed && <div className="art-fallback">{kind === 'artist' ? <MicIcon aria-hidden="true" /> : <DiscIcon aria-hidden="true" />}</div>}
    </div>
  )
}

/** "Add to want list" / "Wanted" (tap again to remove). */
export function WantButton({
  wanted,
  onAdd,
  onRemove,
  label = 'Want',
  compact = false,
}: {
  wanted: boolean
  onAdd: () => Promise<unknown>
  onRemove?: () => Promise<unknown>
  label?: string
  compact?: boolean
}) {
  const [busy, setBusy] = useState(false)
  const { showToast } = useToast()
  async function run(action: () => Promise<unknown>, done: string) {
    setBusy(true)
    try {
      await action()
      showToast(done, 'success', 'want-list')
    } catch (err) {
      showToast(errorMessage(err, 'Could not update your want list.'), 'error')
    } finally {
      setBusy(false)
    }
  }
  if (wanted) {
    return (
      <button
        type="button"
        className={`want-btn is-wanted${compact ? ' want-btn-compact' : ''}`}
        disabled={busy || !onRemove}
        aria-pressed="true"
        onClick={(e) => {
          e.stopPropagation()
          if (onRemove) void run(onRemove, 'Removed from your want list')
        }}
        title={onRemove ? 'Remove from want list' : 'On your want list'}
        aria-label={compact ? (onRemove ? 'Remove from want list' : 'On your want list') : undefined}
      >
        <CheckIcon aria-hidden="true" />
        {!compact && 'Wanted'}
      </button>
    )
  }
  return (
    <button
      type="button"
      className={`want-btn${compact ? ' want-btn-compact' : ''}`}
      disabled={busy}
      aria-pressed="false"
      onClick={(e) => {
        e.stopPropagation()
        void run(onAdd, 'Added to your want list')
      }}
      title="Add to want list"
      aria-label={compact ? 'Add to want list' : undefined}
    >
      <PlusIcon aria-hidden="true" />
      {!compact && label}
    </button>
  )
}

export function Badge({ children }: { children: ReactNode }) {
  return <span className="badge want-badge">{children}</span>
}

/** One release group in a catalog grid: greyed unless owned, with its
 * owned count and a want toggle. Shared with owned artists' "Show missing". */
export function CatalogTile({
  group,
  artist,
  artistMbid,
  artistWanted,
  onOpen,
}: {
  group: CatalogReleaseGroup
  artist: string
  artistMbid: string | null
  artistWanted: boolean
  onOpen: () => void
}) {
  const status = statusLabel(group)
  const albumInput: WantInput = {
    kind: 'album',
    artist,
    title: group.title,
    artist_mbid: artistMbid,
    release_group_mbid: group.mbid,
    release_type: group.primary_type,
  }
  return (
    <div className={`more-card-wrap catalog-tile${group.status === 'owned' ? '' : ' is-missing'}`}>
      <button type="button" className="browse-card browse-card-album" onClick={onOpen}>
        <CatalogCover mbid={group.mbid} />
        <div className="browse-title">{group.title}</div>
        <div className="browse-subtitle">{[year(group.first_release_date), status].filter(Boolean).join(' · ')}</div>
      </button>
      {group.status !== 'owned' && (
        <div className="catalog-tile-action">
          {artistWanted && !group.want_entry_id ? (
            // Wanted through the whole artist: "not this one" excludes it.
            <WantButton
              compact
              wanted={!group.excluded}
              onAdd={() => wantApi.add(albumInput)}
              onRemove={() => wantApi.add({ ...albumInput, excluded: true })}
            />
          ) : (
            <WantButton
              compact
              wanted={group.wanted}
              onAdd={() => wantApi.add(albumInput)}
              onRemove={group.want_entry_id != null ? () => wantApi.remove(group.want_entry_id!) : undefined}
            />
          )}
        </div>
      )}
    </div>
  )
}

/** A release's tracklist: owned songs plain, missing ones greyed with a
 * want toggle. Shared with owned albums' "Show missing". */
export function CatalogTrackList({ album, missingOnly = false }: { album: CatalogAlbum; missingOnly?: boolean }) {
  const multiDisc = useMemo(() => new Set(album.tracks.map((t) => t.disc)).size > 1, [album.tracks])
  const rows = missingOnly ? album.tracks.filter((t) => t.media_item_id == null) : album.tracks
  if (rows.length === 0) return null
  return (
    <ol className="catalog-tracks" aria-label={missingOnly ? 'Missing songs' : 'Tracklist'}>
      {rows.map((track) => (
        <li key={`${track.disc}-${track.position}`} className={`catalog-track${track.media_item_id == null ? ' is-missing' : ''}`}>
          <span className="catalog-track-number">{multiDisc ? `${track.disc}-${track.position}` : track.position}</span>
          <span className="catalog-track-title">
            {track.title}
            {track.artist && track.artist !== album.artist && <span className="want-result-sub"> · {track.artist}</span>}
          </span>
          <span className="catalog-track-length">{formatDuration(track.length_seconds)}</span>
          {track.media_item_id != null ? (
            <Badge>In library</Badge>
          ) : (
            <WantButton
              compact
              wanted={track.wanted}
              onAdd={() =>
                wantApi.add({
                  kind: 'track',
                  artist: track.artist || album.artist,
                  title: track.title,
                  album: album.title,
                  artist_mbid: album.artist_mbid,
                  recording_mbid: track.recording_mbid,
                  release_group_mbid: album.mbid,
                })
              }
              onRemove={track.want_entry_id != null ? () => wantApi.remove(track.want_entry_id!) : undefined}
            />
          )}
        </li>
      ))}
    </ol>
  )
}
