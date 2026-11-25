import { useEffect } from 'react'
import { ApiError } from '../api'
import { useShowMissing } from '../showMissing'
import { onWantListChange, wantApi, type CatalogArtist, type CatalogReleaseGroup } from '../wantList'
import { CatalogTile, CatalogTrackList } from './Catalog'
import { usePendingAsync } from '../usePendingAsync'
import './wantList.css'
import { Switch } from './Switch'

/** The "Show missing" switch on owned artist and album pages (remembered
 * per device, off by default). */
export function ShowMissingToggle() {
  const [showMissing, setShowMissing] = useShowMissing()
  return (
    <Switch className="show-missing-toggle" checked={showMissing} onChange={setShowMissing}>
      Show missing
    </Switch>
  )
}

export interface MissingReleases {
  catalog: CatalogArtist | null
  loading: boolean
  error: string | null
  /** Release groups the library doesn't (fully) have, by section key. */
  bySection: Map<string, CatalogReleaseGroup[]>
}

/** An owned artist's missing releases from MusicBrainz, fetched only while
 * "Show missing" is on. */
export function useMissingReleases(artist: string | null | undefined): MissingReleases {
  const [showMissing] = useShowMissing()
  const enabled = Boolean(artist) && showMissing
  // A first look answers at once with an empty placeholder (`pending`) while
  // the server reads MusicBrainz; asked again until it is complete.
  const state = usePendingAsync(() => wantApi.catalogArtist({ name: artist ?? '' }), [artist, showMissing], { enabled })
  const { reload } = state
  useEffect(() => onWantListChange(reload), [reload])
  const error = state.status === 'error'
    ? state.error instanceof ApiError ? state.error.message : "Couldn't load this artist's discography."
    : null
  // A placeholder lists nothing yet: no tiles until the real answer is in.
  const catalog = error || state.data?.pending ? null : (state.data ?? null)
  const loading = enabled && (state.status === 'loading' || state.fetchingMore)

  const bySection = new Map<string, CatalogReleaseGroup[]>()
  if (showMissing && catalog) {
    for (const group of catalog.release_groups) {
      if (group.status === 'owned') continue
      const key = group.section || 'Album'
      bySection.set(key, [...(bySection.get(key) ?? []), group])
    }
  }
  return { catalog: showMissing ? catalog : null, loading, error: showMissing ? error : null, bySection }
}

/** Greyed tiles for missing releases inside an artist page section. */
export function MissingTiles({
  groups,
  catalog,
  artist,
  onOpenCatalogAlbum,
}: {
  groups: CatalogReleaseGroup[]
  catalog: CatalogArtist
  artist: string
  onOpenCatalogAlbum: (mbid: string, artist: string) => void
}) {
  return (
    <>
      {groups.map((group) => (
        <CatalogTile
          key={group.mbid}
          group={group}
          artist={artist}
          artistMbid={catalog.artist_mbid}
          artistWanted={catalog.artist_wanted}
          onOpen={() => onOpenCatalogAlbum(group.mbid, artist)}
        />
      ))}
    </>
  )
}

/** Songs an owned album is missing, from its MusicBrainz edition.
 * `headerLoading`: the album's own details (which say whether it is matched)
 * are still on their way, so "not matched" would be premature. */
export function AlbumMissingTracks({
  releaseGroupMbid,
  artist,
  headerLoading = false,
}: {
  releaseGroupMbid: string | null | undefined
  artist: string | null
  headerLoading?: boolean
}) {
  const [showMissing] = useShowMissing()
  // The tracklist may come back as a placeholder (`pending`) the first time:
  // asked again until MusicBrainz has answered.
  const state = usePendingAsync(
    () => wantApi.catalogAlbum(releaseGroupMbid ?? '', artist),
    [showMissing, releaseGroupMbid, artist],
    { enabled: showMissing && Boolean(releaseGroupMbid) },
  )
  const { reload } = state
  useEffect(() => onWantListChange(reload), [reload])

  if (!showMissing) return null
  if (!releaseGroupMbid) {
    if (headerLoading) return <p className="missing-section-note">Loading the full tracklist…</p>
    return <p className="missing-section-note">This album hasn't been matched on MusicBrainz yet, so its missing songs aren't known.</p>
  }
  const album = state.data
  if (state.status === 'error' || album?.pending === true && !state.fetchingMore) {
    return <p className="missing-section-note">Couldn't load this album's full tracklist.</p>
  }
  if (!album || state.fetchingMore) return <p className="missing-section-note">Loading the full tracklist…</p>
  const missing = album.tracks.filter((track) => track.media_item_id == null).length
  if (missing === 0) return <p className="missing-section-note">Nothing missing - you have every song.</p>
  return (
    <section className="artist-album-section" aria-label="Missing songs">
      <h3 className="artist-section-heading">Missing songs</h3>
      <CatalogTrackList album={album} missingOnly />
    </section>
  )
}
