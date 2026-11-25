import type { MenuAnchor } from '../menuTrigger'
import { mediaRef } from '../artEntity'
import { usePlayerActions } from '../PlayerContext'
import type { MediaItem } from '../types'
import { EntityMenuItems } from './EntityMenuItems'
import { Menu, MenuItem } from './Menu'

/** Menu for a video row/card: queue moves plus the shared art/download entries. */
export function VideoMenuPanel({
  video,
  anchor,
  menuId,
  onClose,
}: {
  video: MediaItem
  anchor: MenuAnchor
  /** The menu's id, for the trigger's aria-controls. */
  menuId?: string
  onClose: () => void
}) {
  const { playNext, addToQueueEnd } = usePlayerActions()
  return (
    <Menu id={menuId} className="track-menu-panel" label={`Actions for ${video.title}`} anchor={anchor} onClose={onClose}>
      <MenuItem onSelect={() => { playNext(video); onClose() }}>Add next in queue</MenuItem>
      <MenuItem onSelect={() => { addToQueueEnd(video); onClose() }}>Add last in queue</MenuItem>
      <EntityMenuItems art={mediaRef(video)} download={{ kind: 'media', key: video.id }} onClose={onClose} />
    </Menu>
  )
}
