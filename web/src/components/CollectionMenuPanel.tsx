import { useState } from 'react'
import { api, ApiError } from '../api'
import { usePlayerActions } from '../PlayerContext'
import { useToast } from '../ToastContext'
import { openInfo, type InfoTarget } from '../infoModalBus'
import type { ArtRef } from '../artEntity'
import type { MenuAnchor } from '../menuTrigger'
import type { Playlist } from '../types'
import { trackCountLabel } from '../plural'
import { usePrompt } from '../dialogs'
import { EntityMenuItems } from './EntityMenuItems'
import { ArrowLeftIcon } from './Icons'
import { Menu, MenuItem, MenuNote } from './Menu'

interface Props {
  label: string
  filter: { artist?: string; artist_is_null?: boolean; album?: string }
  playlists: Playlist[]
  anchor: MenuAnchor
  /** The menu's id, for the trigger's aria-controls. */
  menuId?: string
  onClose: () => void
  onPlaylistsChanged?: () => void
  /** The album/artist's custom-art identity (its art_key); without it the
   * art and download entries are left out. */
  artRef?: ArtRef
}

/** Actions shared by artist/album cards and the Home page's collection cards. */
export function CollectionMenuPanel({
  label,
  filter,
  playlists,
  anchor,
  menuId,
  onClose,
  onPlaylistsChanged,
  artRef,
}: Props) {
  const { playNextMany, addManyToQueueEnd } = usePlayerActions()
  const { showToast, showPlaylistToast } = useToast()
  const [showPlaylistPicker, setShowPlaylistPicker] = useState(false)
  const prompt = usePrompt()
  // Set when stepping back out of the playlist picker, so focus lands on the
  // item that opened it rather than the top of the menu.
  const [cameBackFromPicker, setCameBackFromPicker] = useState(false)
  const playlistChoices = playlists.filter((playlist) => !playlist.auto_kind)

  const infoTarget: InfoTarget | null = filter.album
    ? { kind: 'album', artist: filter.artist ?? null, album: filter.album }
    : filter.artist
      ? { kind: 'artist', name: filter.artist }
      : null

  async function loadTracks() {
    const result = []
    for (let offset = 0; ; offset += 500) {
      const page = await api.listTracks({ ...filter, limit: 500, offset })
      result.push(...page)
      if (page.length < 500) return result
    }
  }

  async function queueCollection(mode: 'next' | 'end') {
    try {
      const tracks = await loadTracks()
      if (mode === 'next') playNextMany(tracks)
      else addManyToQueueEnd(tracks)
      showToast(`${mode === 'next' ? 'Queued next' : 'Added'} ${trackCountLabel(tracks.length)} from "${label}".`, 'success')
    } catch {
      showToast(`Couldn't load tracks from "${label}".`, 'error')
    }
  }

  async function createPlaylist() {
    const name = await prompt({
      title: 'Create playlist',
      label: 'Create a playlist named:',
      defaultValue: label,
      confirmLabel: 'Create',
    })
    if (!name?.trim()) return
    try {
      const tracks = await loadTracks()
      const playlist = await api.createPlaylist(name.trim())
      for (let i = 0; i < tracks.length; i += 500) {
        await api.addToPlaylist(playlist.id, tracks.slice(i, i + 500).map((track) => track.id))
      }
      onPlaylistsChanged?.()
      showPlaylistToast(`Created "${playlist.name}" with ${trackCountLabel(tracks.length)}.`, playlist.id)
    } catch {
      showToast(`Couldn't create a playlist from "${label}".`, 'error')
    }
  }

  async function generateSimilar() {
    try {
      const result = await api.generatePlaylistFromCollection({
        label,
        artist: filter.artist,
        album: filter.album,
      })
      onPlaylistsChanged?.()
      showPlaylistToast(`Created "${result.name}" (${trackCountLabel(result.track_count)}).`, result.playlist_id)
    } catch (err) {
      showToast(
        err instanceof ApiError ? err.message : `Could not generate a mix from "${label}".`,
        'error',
      )
    }
  }

  async function addToPlaylist(playlist: Playlist) {
    onClose()
    try {
      const tracks = await loadTracks()
      for (let i = 0; i < tracks.length; i += 500) {
        await api.addToPlaylist(playlist.id, tracks.slice(i, i + 500).map((track) => track.id))
      }
      onPlaylistsChanged?.()
      showToast(`Added ${trackCountLabel(tracks.length)} to "${playlist.name}".`, 'success')
    } catch {
      showToast(`Couldn't add "${label}" to "${playlist.name}".`, 'error')
    }
  }

  const stepBack = () => { setShowPlaylistPicker(false); setCameBackFromPicker(true) }

  return (
    <Menu
      id={menuId}
      className="track-menu-panel collection-menu-panel"
      label={`Actions for ${label}`}
      anchor={anchor}
      onClose={onClose}
      onBack={showPlaylistPicker ? stepBack : undefined}
      view={showPlaylistPicker ? 'playlists' : 'actions'}
    >
      {showPlaylistPicker ? (
        <>
          <MenuItem className="track-menu-back" onSelect={stepBack}><ArrowLeftIcon aria-hidden="true" /> Back</MenuItem>
          {playlistChoices.length === 0 ? (
            <MenuNote className="track-menu-empty">No playlists yet.</MenuNote>
          ) : (
            playlistChoices.map((playlist) => (
              <MenuItem key={playlist.id} onSelect={() => addToPlaylist(playlist)}>{playlist.name}</MenuItem>
            ))
          )}
        </>
      ) : (
        <>
          <MenuItem onSelect={() => { void queueCollection('next'); onClose() }}>Add next in queue</MenuItem>
          <MenuItem onSelect={() => { void queueCollection('end'); onClose() }}>Add last in queue</MenuItem>
          <MenuItem onSelect={() => { void createPlaylist(); onClose() }}>Create playlist from {label}</MenuItem>
          {(filter.artist || filter.album) && (
            <MenuItem onSelect={() => { void generateSimilar(); onClose() }}>Generate mix of similar songs</MenuItem>
          )}
          <MenuItem hasSubmenu defaultFocus={cameBackFromPicker} onSelect={() => setShowPlaylistPicker(true)}>
            Add all to playlist
          </MenuItem>
          {infoTarget && <MenuItem onSelect={() => { openInfo(infoTarget); onClose() }}>Info</MenuItem>}
          <EntityMenuItems
            art={artRef}
            artLabel={label}
            download={artRef && (artRef.type === 'album' || artRef.type === 'artist') ? { kind: artRef.type, key: artRef.key } : undefined}
            onClose={onClose}
          />
        </>
      )}
    </Menu>
  )
}
