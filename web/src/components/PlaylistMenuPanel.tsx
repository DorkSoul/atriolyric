import type { MenuAnchor } from '../menuTrigger'
import { playlistRef } from '../artEntity'
import { api } from '../api'
import { usePlayerActions } from '../PlayerContext'
import { useToast } from '../ToastContext'
import type { Playlist } from '../types'
import { trackCountLabel } from '../plural'
import { EntityMenuItems } from './EntityMenuItems'
import { Menu, MenuItem } from './Menu'

/** Menu for a playlist/mix card or header. */
export function PlaylistMenuPanel({
  playlist,
  anchor,
  menuId,
  onClose,
  onDelete,
}: {
  playlist: Pick<Playlist, 'id' | 'name' | 'auto_kind' | 'custom_art' | 'art_version'>
  anchor: MenuAnchor
  /** The menu's id, for the trigger's aria-controls. */
  menuId?: string
  onClose: () => void
  /** Omit where deleting makes no sense (e.g. Favourites). */
  onDelete?: () => void
}) {
  const { playNextMany, addManyToQueueEnd } = usePlayerActions()
  const { showToast } = useToast()

  async function queue(mode: 'next' | 'end') {
    try {
      const detail = await api.getPlaylist(playlist.id)
      if (mode === 'next') playNextMany(detail.items)
      else addManyToQueueEnd(detail.items)
      showToast(`${mode === 'next' ? 'Queued next' : 'Added'} ${trackCountLabel(detail.items.length)} from "${playlist.name}".`, 'success')
    } catch {
      showToast(`Couldn't load "${playlist.name}".`, 'error')
    }
  }

  return (
    <Menu id={menuId} className="track-menu-panel collection-menu-panel" label={`Actions for ${playlist.name}`} anchor={anchor} onClose={onClose}>
      <MenuItem onSelect={() => { void queue('next'); onClose() }}>Add next in queue</MenuItem>
      <MenuItem onSelect={() => { void queue('end'); onClose() }}>Add last in queue</MenuItem>
      <EntityMenuItems
        art={playlistRef(playlist)}
        download={{ kind: 'playlist', key: playlist.id }}
        downloadRefusal={
          playlist.auto_kind && playlist.auto_kind !== 'favorites'
            ? 'A mix changes when it refreshes. Save it as a playlist, then download the playlist.'
            : undefined
        }
        onClose={onClose}
      />
      {onDelete && <MenuItem onSelect={() => { onClose(); onDelete() }}>Delete playlist</MenuItem>}
    </Menu>
  )
}
