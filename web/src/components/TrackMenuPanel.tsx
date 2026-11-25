import { useState } from 'react'
import { api, ApiError } from '../api'
import { usePlayerActions } from '../PlayerContext'
import { useToast } from '../ToastContext'
import { mediaRef } from '../artEntity'
import { openInfo } from '../infoModalBus'
import type { MenuAnchor } from '../menuTrigger'
import type { OutsideClickInside } from '../useOutsideClick'
import type { MediaItem, Playlist } from '../types'
import { trackCountLabel } from '../plural'
import { EntityMenuItems } from './EntityMenuItems'
import { ArrowLeftIcon } from './Icons'
import { Menu, MenuItem, MenuNote } from './Menu'

interface Props {
  track: MediaItem
  playlists: Playlist[]
  isAdmin: boolean
  /** Viewport coordinates (from the triggering click/touch) to open the
   * menu at, so it appears where the user actually pressed rather than a
   * fixed corner of whatever row/element it came from. */
  anchor: MenuAnchor
  /** The menu's id, for the trigger's aria-controls. */
  menuId?: string
  /** Areas a press may land in without closing the menu. */
  alsoInside?: OutsideClickInside[]
  onClose: () => void
  onViewLyrics?: () => void
  /** Omit to hide the action entirely (e.g. non-admins, or a context with
   * nowhere sensible to send it). */
  onEditTags?: () => void
  onSelect?: () => void
  onPlaylistsChanged?: () => void
  /** Only meaningful within a playlist's own detail view. */
  onRemoveFromPlaylist?: () => void
}

/** The track "..." menu's contents - shared by every place a track can be
 * long-pressed/right-clicked (track list rows, the full-screen Now Playing
 * view) so they can't drift out of sync with each other. Owns the generic
 * actions (queue moves, generating a mix, adding to a playlist) itself;
 * actions that depend on the caller's own state (viewing lyrics, editing
 * tags, bulk-select, removing from a playlist) are delegated via optional
 * callbacks so this stays usable outside a list. */
export function TrackMenuPanel({
  track,
  playlists,
  isAdmin,
  anchor,
  menuId,
  alsoInside,
  onClose,
  onViewLyrics,
  onEditTags,
  onSelect,
  onPlaylistsChanged,
  onRemoveFromPlaylist,
}: Props) {
  const { playNext, addToQueueEnd } = usePlayerActions()
  const { showToast, showPlaylistToast } = useToast()
  const [showPlaylistPicker, setShowPlaylistPicker] = useState(false)
  // Set when stepping back out of the playlist picker, so focus lands on the
  // item that opened it rather than the top of the menu.
  const [cameBackFromPicker, setCameBackFromPicker] = useState(false)
  const playlistChoices = playlists.filter((pl) => !pl.auto_kind)

  async function generateFromTrack() {
    try {
      const result = await api.generatePlaylistFromTrack(track.id)
      onPlaylistsChanged?.()
      showPlaylistToast(`Created "${result.name}" (${trackCountLabel(result.track_count)}).`, result.playlist_id)
    } catch (err) {
      // The backend gives a specific reason (e.g. not enough similar
      // tracks found) via ApiError.message - surface that instead of a
      // generic one whenever we have it.
      const message =
        err instanceof ApiError ? err.message : 'Could not generate a mix from that song.'
      showToast(message, 'error')
    }
  }

  return (
    <Menu
      id={menuId}
      className="track-menu-panel"
      label={`Actions for ${track.title}`}
      anchor={anchor}
      alsoInside={alsoInside}
      onClose={onClose}
      onBack={showPlaylistPicker ? () => { setShowPlaylistPicker(false); setCameBackFromPicker(true) } : undefined}
      view={showPlaylistPicker ? 'playlists' : 'actions'}
    >
      {showPlaylistPicker ? (
        <>
          <MenuItem
            className="track-menu-back"
            onSelect={() => { setShowPlaylistPicker(false); setCameBackFromPicker(true) }}
          >
            <ArrowLeftIcon aria-hidden="true" /> Back
          </MenuItem>
          {playlistChoices.length === 0 ? (
            <MenuNote className="track-menu-empty">No playlists yet.</MenuNote>
          ) : (
            playlistChoices.map((pl) => (
              <MenuItem
                key={pl.id}
                onSelect={async () => {
                  onClose()
                  try {
                    await api.addToPlaylist(pl.id, [track.id])
                    onPlaylistsChanged?.()
                    showToast(`Added "${track.title}" to "${pl.name}".`, 'success')
                  } catch {
                    showToast(`Couldn't add "${track.title}" to "${pl.name}".`, 'error')
                  }
                }}
              >
                {pl.name}
              </MenuItem>
            ))
          )}
        </>
      ) : (
        <>
          <MenuItem onSelect={() => { playNext(track); onClose() }}>Add next in queue</MenuItem>
          <MenuItem onSelect={() => { addToQueueEnd(track); onClose() }}>Add last in queue</MenuItem>
          <MenuItem onSelect={() => { generateFromTrack(); onClose() }}>Generate mix from this song</MenuItem>
          {onViewLyrics && <MenuItem onSelect={() => { onViewLyrics(); onClose() }}>View lyrics</MenuItem>}
          {isAdmin && onEditTags && <MenuItem onSelect={() => { onEditTags(); onClose() }}>Edit tags</MenuItem>}
          {isAdmin && onSelect && <MenuItem onSelect={() => { onSelect(); onClose() }}>Select</MenuItem>}
          <MenuItem hasSubmenu defaultFocus={cameBackFromPicker} onSelect={() => setShowPlaylistPicker(true)}>
            Add to playlist
          </MenuItem>
          {track.kind !== 'video' && (
            <MenuItem onSelect={() => { openInfo({ kind: 'track', track }); onClose() }}>Info</MenuItem>
          )}
          {onRemoveFromPlaylist && (
            <MenuItem onSelect={() => { onRemoveFromPlaylist(); onClose() }}>Remove from playlist</MenuItem>
          )}
          <EntityMenuItems
            art={mediaRef(track)}
            artLabel={[track.title, track.artist].filter(Boolean).join(' · ')}
            download={{ kind: 'media', key: track.id }}
            onClose={onClose}
          />
        </>
      )}
    </Menu>
  )
}
