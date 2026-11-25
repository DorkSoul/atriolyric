import { useMemo, useState } from 'react'
import { pickableCredits } from '../artistCredits'
import { navToHash } from '../nav'
import type { ArtistCredit } from '../types'
import { ArtistPickerModal } from './ArtistPickerModal'

interface Props {
  /** The artist display string, as tagged. */
  artist?: string | null
  /** Individual credits behind `artist`; more than one opens a picker. */
  artists?: ArtistCredit[]
  album?: string | null
  /** Who owns the album when that isn't `artist` (the album-artist tag). */
  albumArtist?: string | null
  onArtist?: (artist: string) => void
  onAlbum?: (artist: string | null, album: string) => void
  disableArtist?: boolean
  disableAlbum?: boolean
  /** Only the artist or only the album (a table's separate columns). */
  part?: 'artist' | 'album'
}

function stopLinkClick(e: React.MouseEvent, action: () => void) {
  e.preventDefault()
  e.stopPropagation()
  action()
}

export function LibraryLinks({
  artist,
  artists,
  album,
  albumArtist,
  onArtist,
  onAlbum,
  disableArtist,
  disableAlbum,
  part,
}: Props) {
  const [picking, setPicking] = useState(false)
  const credits = useMemo(() => pickableCredits(artists), [artists])
  const albumOwner = albumArtist ?? artist ?? null

  let artistLink: React.ReactNode = artist ?? 'Unknown artist'
  if (artist && onArtist && !disableArtist) {
    if (credits.length > 1) {
      artistLink = (
        <>
          {/* An <a>, not a <button>: a button is an atomic inline-block (its
              36px min-height would stretch the subtitle line, and text-overflow
              cannot cut it with "…"). Enter works natively on a link. */}
          <a
            href={`#/artists/${encodeURIComponent(credits[0].name)}`}
            className="library-link-button"
            aria-haspopup="dialog"
            onClick={(e) => stopLinkClick(e, () => setPicking(true))}
          >
            {artist}
          </a>
          {picking && (
            // Portaled, but React still bubbles its events to the row this
            // sits in, which would play the track. Not a control itself.
            // oxlint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions
            <span
              onClick={(e) => e.stopPropagation()}
              onKeyDown={(e) => e.stopPropagation()}
              onPointerDown={(e) => e.stopPropagation()}
              onPointerUp={(e) => e.stopPropagation()}
            >
              <ArtistPickerModal credits={credits} onPick={onArtist} onClose={() => setPicking(false)} />
            </span>
          )}
        </>
      )
    } else {
      const name = credits.length === 1 ? credits[0].name : artist
      artistLink = (
        <a href={`#/artists/${encodeURIComponent(name)}`} onClick={(e) => stopLinkClick(e, () => onArtist(name))}>
          {artist}
        </a>
      )
    }
  }
  const albumLink = album ? <AlbumLink album={album} albumOwner={albumOwner} onAlbum={disableAlbum ? undefined : onAlbum} /> : album

  if (part === 'artist') return <span className="library-links">{artistLink}</span>
  if (part === 'album') return <span className="library-links">{albumLink}</span>
  return (
    <span className="library-links">
      {artistLink}
      {albumLink && <><span className="library-links-separator"> · </span>{albumLink}</>}
    </span>
  )
}

/** An album name that opens the album (a link), or plain text without
 * `onAlbum`. `albumOwner` is the album artist (else the track artist). On
 * its own for a separate album line, as Now Playing and the side panel show. */
export function AlbumLink({
  album,
  albumOwner,
  onAlbum,
}: {
  album: string
  albumOwner: string | null
  onAlbum?: (artist: string | null, album: string) => void
}) {
  if (!onAlbum) return <>{album}</>
  return (
    <a
      href={navToHash({ tab: 'albums', browseAlbum: { artist: albumOwner, album } })}
      onClick={(e) => stopLinkClick(e, () => onAlbum(albumOwner, album))}
    >
      {album}
    </a>
  )
}
