import { Suspense, useActionState, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { TopBarBack } from '../headerControlsPortal'
import { errorMessage } from '../errorMessage'
import { KEPT_TAB_STALE_MS, useAsync } from '../useAsync'
import { useToast } from '../ToastContext'
import { usePlayerActions } from '../PlayerContext'
import { useCurrentTrackId, usePlayerSelector } from '../playerSelectors'
import type { MediaItem, Playlist, PlaySource } from '../types'
import { Art } from './Art'
import { ErrorState, LoadingState } from './ErrorState'
import { CollectionDetailHeader } from './CollectionDetailHeader'
import { MoreButton, MoreMenu } from './MoreMenu'
import { useConfirm } from '../dialogs'
import { PlaylistMenuPanel } from './PlaylistMenuPanel'
import { CollectionDownloadMark } from '../native/DownloadMark'
import { effectiveArt, playlistRef, useArtRevision } from '../artEntity'
import { useLongPressMenu } from '../useLongPressMenu'
import { FloppyIcon, HeartIcon, MusicNoteIcon, PlayIcon, ShuffleIcon, SparkleIcon } from './Icons'
import { DownloadIcon, PlusIcon, TrashIcon } from './BrowseIcons'
import { SearchChip } from './SearchChip'
import { formatDuration } from '../formatDuration'
import { MixCollage } from './MixCollage'
import { PlaylistReorderList } from './PlaylistReorderList'
import { SubmitButton } from './SubmitButton'
import { TrackList } from './TrackList'
import { HelpTip } from './HelpTip'
import { lazyView } from '../lazyView'

// Opened on demand, so its code loads when first used.
const SpecifyMixDialog = lazyView(() => import('./SpecifyMixDialog').then((m) => m.SpecifyMixDialog))

interface Props {
  /** Search text to start with (Home's "View full search"). */
  initialSearch?: string
  playlists: Playlist[]
  onChanged: () => void
  /** Which playlist (if any) is open, lifted up into App's URL-hash nav
   * state - not local component state - so opening one is a real history
   * entry. Otherwise the browser/hardware back button has nothing to pop
   * for it and falls through to whatever page was open before the
   * playlists tab, instead of just closing the playlist. */
  openId: number | null
  onOpenChange: (id: number | null) => void
  /** The open playlist's back link (named by `backLabel`). */
  onBack: () => void
  /** Label for the single history-aware detail breadcrumb. */
  backLabel: string
  onOpenArtist?: (artist: string) => void
  onOpenAlbum?: (artist: string | null, album: string) => void
  /** The scrolling ancestor: an open playlist's rows are windowed against
   * it, so a playlist of thousands renders only the rows near the viewport. */
  scrollContainerRef?: React.RefObject<HTMLElement | null>
}

/** "40 tracks · 2 h 31 min" for a playlist page's subtitle. */
function playlistSummary(items: MediaItem[]): string {
  const count = `${items.length} ${items.length === 1 ? 'track' : 'tracks'}`
  const seconds = items.reduce((sum, item) => sum + (item.duration_seconds ?? 0), 0)
  if (seconds <= 0) return count
  const minutes = Math.round(seconds / 60)
  const length = minutes >= 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min` : minutes > 0 ? `${minutes} min` : formatDuration(seconds)
  return `${count} · ${length}`
}

function customPlaylistArt(pl: Playlist) {
  return effectiveArt(playlistRef(pl)).custom
}

export function PlaylistsView({
  playlists,
  onChanged,
  openId,
  onOpenChange,
  onBack,
  backLabel,
  onOpenArtist,
  onOpenAlbum,
  initialSearch = '',
  scrollContainerRef,
}: Props) {
  const { showToast } = useToast()
  const confirm = useConfirm()
  useArtRevision()
  const menu = useLongPressMenu<number>()
  const [newName, setNewName] = useState('')
  // Home's "View full search" opens this tab with a query; there is no
  // search box here, so it shows as a removable chip.
  const [filter, setFilter] = useState(initialSearch)
  const [saving, setSaving] = useState(false)
  const [addingMore, setAddingMore] = useState(false)
  const [specifyMixOpen, setSpecifyMixOpen] = useState(false)
  // Keyed by id so opening another playlist never inherits an open form.
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editName, setEditName] = useState('')
  const [editDescription, setEditDescription] = useState('')
  // Maps a mix's id to the copy saved from it, so only that mix shows as saved
  // and clicking again removes exactly that copy.
  // Rearranging: the open playlist's id, so another playlist never opens in it.
  const [reorderingId, setReorderingId] = useState<number | null>(null)
  // Saves of the new order run one after another, each sending the order as
  // it stands; `confirmedRef` is the last order the server accepted, put back
  // if one is refused (and `epochRef` drops the saves queued behind it).
  const reorderChain = useRef<Promise<void>>(Promise.resolve())
  const confirmedRef = useRef<MediaItem[] | null>(null)
  const epochRef = useRef(0)
  const [savedCopies, setSavedCopies] = useState<Record<number, number>>({})
  const currentTrackId = useCurrentTrackId()
  const queueContextCount = usePlayerSelector((player) => player.queueContextCount)
  const isPlaying = usePlayerSelector((player) => player.isPlaying)
  const isShuffled = usePlayerSelector((player) => player.isShuffled)
  const { playQueue, setShuffle, togglePlayPause } = usePlayerActions()

  // The open playlist, keyed on its id: opening another one never shows the
  // previous one's tracks while it loads.
  const detailState = useAsync(() => api.getPlaylist(openId as number), [openId], {
    enabled: openId != null,
    // A kept-mounted tab: shown again soon after, it keeps what it has (an
    // edit anywhere - clearBrowseCache - makes it ask again).
    staleMs: KEPT_TAB_STALE_MS,
  })
  const detail = openId != null ? (detailState.data ?? null) : null
  const reloadOpenPlaylist = detailState.reload
  // A refresh after an edit keeps the rows on screen; its failure still says so.
  const refreshFailed = detailState.status === 'error' && detailState.data !== undefined
  useEffect(() => {
    if (refreshFailed) showToast('Could not refresh that playlist.', 'error')
  }, [refreshFailed, showToast])

  // The name input is controlled: a failed create keeps what was typed.
  const [, create] = useActionState(async () => {
    if (!newName.trim()) return
    try {
      await api.createPlaylist(newName.trim())
      setNewName('')
      onChanged()
    } catch {
      showToast('Could not create the playlist.', 'error')
    }
  }, undefined)

  function moveTrack(playlistId: number, from: number, to: number) {
    const current = detailState.data
    if (!current || current.id !== playlistId) return
    if (!confirmedRef.current) confirmedRef.current = current.items
    const items = [...current.items]
    const [moved] = items.splice(from, 1)
    items.splice(to, 0, moved)
    // Shown at once; the save follows.
    detailState.setData({ ...current, items })
    const epoch = epochRef.current
    const ids = items.map((item) => item.playlist_item_id as number)
    reorderChain.current = reorderChain.current.then(async () => {
      if (epoch !== epochRef.current) return
      try {
        const saved = await api.reorderPlaylist(playlistId, ids)
        if (epoch !== epochRef.current) return
        confirmedRef.current = saved.items
      } catch (e) {
        if (epoch !== epochRef.current) return
        epochRef.current += 1
        const confirmed = confirmedRef.current
        if (confirmed) detailState.setData((previous) => ({ ...(previous ?? current), items: confirmed }))
        showToast(errorMessage(e, 'Could not save the new order.'), 'error')
      }
    })
  }

  function stopReordering() {
    // Whatever is still saving finishes; the next arrangement starts clean.
    setReorderingId(null)
    reorderChain.current = reorderChain.current.then(() => {
      confirmedRef.current = null
    })
  }

  async function remove(id: number, name: string) {
    if (!(await confirm({
      title: 'Delete playlist',
      body: `Delete playlist "${name}"? This cannot be undone.`,
      confirmLabel: 'Delete',
      danger: true,
    }))) return
    try {
      await api.deletePlaylist(id)
      if (openId === id) onOpenChange(null)
      onChanged()
    } catch {
      showToast('Could not delete the playlist.', 'error')
    }
  }

  async function saveAsPlaylist() {
    if (!openPlaylist || saving) return
    const sourceId = openPlaylist.id
    const copyId = savedCopies[sourceId]
    setSaving(true)
    try {
      if (copyId != null) {
        await api.deletePlaylist(copyId)
        setSavedCopies(({ [sourceId]: _removed, ...rest }) => rest)
        onChanged()
        showToast('Removed from playlists', 'success')
      } else {
        const copy = await api.savePlaylistCopy(sourceId, openPlaylist.name)
        setSavedCopies((prev) => ({ ...prev, [sourceId]: copy.id }))
        onChanged()
        showToast('Added as a playlist', 'success')
      }
    } catch {
      showToast(copyId != null ? 'Could not remove that playlist.' : 'Could not save a copy of this playlist.', 'error')
    } finally {
      setSaving(false)
    }
  }

  function startEditing() {
    if (!detail) return
    setEditName(detail.name)
    setEditDescription(detail.description ?? '')
    setEditingId(detail.id)
  }

  async function saveEdits() {
    if (!detail || !editName.trim()) return
    setSaving(true)
    try {
      detailState.setData(await api.updatePlaylist(detail.id, { name: editName.trim(), description: editDescription }))
      setEditingId(null)
      onChanged()
    } catch {
      showToast('Could not save the playlist.', 'error')
    } finally {
      setSaving(false)
    }
  }

  async function addMoreTracks() {
    if (!openPlaylist || addingMore) return
    setAddingMore(true)
    try {
      detailState.setData(await api.addMoreTracks(openPlaylist.id))
      onChanged()
      showToast('Added more tracks to the playlist.', 'success')
    } catch {
      showToast('Could not add more tracks to this playlist.', 'error')
    } finally {
      setAddingMore(false)
    }
  }

  const openPlaylist = playlists.find((p) => p.id === openId) ?? null
  // Opening a playlist is also used by Home's mix cards. The playlist list
  // may still be refreshing when the detail request completes, so don't
  // make the detail view depend on that separate list request. This fallback
  // keeps the detail view usable immediately; the list data is used when it
  // becomes available for artwork and generated-playlist actions.
  const detailPlaylist =
    openId != null && detail?.id === openId
      ? (openPlaylist ?? {
          id: detail.id,
          name: detail.name,
          description: detail.description,
          created_at: detail.created_at,
          item_count: detail.items.length,
          is_auto_generated: false,
          auto_kind: null,
          preview_media_item_ids: [],
          custom_art: detail.custom_art,
          art_version: detail.art_version,
        })
      : null

  if (detailPlaylist && detail) {
    const editing = editingId === detail.id
    const isAutoManaged = !!detailPlaylist.auto_kind && detailPlaylist.auto_kind !== 'favorites'
    const isSaved = savedCopies[detail.id] != null
    const reordering = reorderingId === detail.id
    const playSource: PlaySource = {
      kind: 'playlist',
      label: detail.name,
      payload: { playlist_id: detail.id, ...(detailPlaylist.auto_kind ? { auto_kind: detailPlaylist.auto_kind } : {}) },
    }
    // "Active" = the queue is playing this playlist's own context.
    const mixActive =
      currentTrackId != null && detail.items.some((item) => item.id === currentTrackId) && queueContextCount === detail.items.length
    const playFilled = mixActive && isPlaying && !isShuffled
    const shuffleFilled = mixActive && isPlaying && isShuffled

    const mixItems = detail.items
    function playMix(shuffle: boolean) {
      if (mixItems.length === 0) return
      if (mixActive && isShuffled === shuffle) {
        togglePlayPause()
        return
      }
      api.recordPlaySource(playSource).catch(() => {})
      if (shuffle) {
        setShuffle(true)
        playQueue(mixItems, Math.floor(Math.random() * mixItems.length), playSource)
      } else {
        setShuffle(false)
        playQueue(mixItems, 0, playSource)
      }
    }

    return (
      <div className="playlist-page">
        {/* Not a new history entry: App goes back to the page this playlist
            was opened from (the Playlists list, a mix on Home) when that is
            the entry before it, and otherwise - a link, a reload, a new tab -
            shows the Playlists list in this entry's place. */}
        <CollectionDetailHeader
          backLabel={backLabel}
          onBack={onBack}
          title={detail.name}
          eyebrow={
detailPlaylist.auto_kind && detailPlaylist.auto_kind !== 'favorites' ? 'Generated mix' : 'Playlist'
          }
          subtitle={playlistSummary(detail.items)}
          description={detail.description}
          collageMediaItemIds={detailPlaylist.preview_media_item_ids}
          artRef={playlistRef(detailPlaylist)}
          fallback={detailPlaylist.auto_kind === 'favorites' ? <HeartIcon filled /> : detailPlaylist.auto_kind ? <SparkleIcon /> : <MusicNoteIcon />}
          actions={
            <>
              {detail.items.length > 0 && (
                <>
                  <button
                    type="button"
                    className={`btn btn-primary hero-play-btn${playFilled ? ' active' : ''}`}
                    onClick={() => playMix(false)}
                    aria-pressed={playFilled}
                    aria-label="Play"
                    title="Play"
                  >
                    <PlayIcon aria-hidden="true" />
                    <span>Play</span>
                  </button>
                  <button
                    type="button"
                    className={`btn btn-ghost hero-shuffle-btn${shuffleFilled ? ' active' : ''}`}
                    onClick={() => playMix(true)}
                    aria-pressed={shuffleFilled}
                    aria-label="Shuffle play"
                    title="Shuffle play"
                  >
                    <ShuffleIcon aria-hidden="true" />
                    <span>Shuffle</span>
                  </button>
                </>
              )}
              {isAutoManaged && (
                <button
                  type="button"
                  className={`btn btn-ghost btn-sm playlist-tool-btn${isSaved ? ' active' : ''}`}
                  onClick={saveAsPlaylist}
                  disabled={saving}
                  aria-label={isSaved ? 'Remove saved playlist' : 'Save as a playlist'}
                  aria-pressed={isSaved}
                  title={isSaved ? 'Remove saved playlist' : 'Save as a playlist'}
                >
                  <FloppyIcon filled={isSaved} aria-hidden="true" />
                  <span>{isSaved ? 'Saved as a playlist' : 'Save as a playlist'}</span>
                </button>
              )}
              {!detailPlaylist.auto_kind && !editing && (
                <button type="button" className="btn btn-ghost btn-sm playlist-tool-btn" onClick={startEditing}>
                  Edit
                </button>
              )}
              {!detailPlaylist.auto_kind && !editing && detail.items.length > 1 && (
                <button
                  type="button"
                  className="btn btn-ghost btn-sm playlist-tool-btn"
                  aria-pressed={reordering}
                  onClick={() => (reordering ? stopReordering() : setReorderingId(detail.id))}
                >
                  {reordering ? 'Done' : 'Reorder'}
                </button>
              )}
              <MoreMenu label={detail.name}>
                {(anchor, close, menuId) => (
                  <PlaylistMenuPanel
                    playlist={detailPlaylist}
                    anchor={anchor}
                    menuId={menuId}
                    onClose={close}
                    onDelete={
                      detailPlaylist.auto_kind === 'favorites'
                        ? undefined
                        : () => void remove(detail.id, detail.name)
                    }
                  />
                )}
              </MoreMenu>
            </>
          }
        />
        {editing && (
          <form
            className="playlist-edit-form"
            onSubmit={(e) => {
              e.preventDefault()
              void saveEdits()
            }}
          >
            <label>
              Name
              <input value={editName} maxLength={200} onChange={(e) => setEditName(e.target.value)} required />
            </label>
            <label>
              Description
              <textarea
                value={editDescription}
                maxLength={1000}
                rows={3}
                onChange={(e) => setEditDescription(e.target.value)}
              />
            </label>
            <div className="playlist-edit-actions">
              <button type="submit" className="btn btn-primary btn-sm" disabled={saving || !editName.trim()}>
                {saving ? 'Saving…' : 'Save'}
              </button>
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => setEditingId(null)}>
                Cancel
              </button>
            </div>
          </form>
        )}
        <div className="playlist-tools">
          {isAutoManaged && (
            <p className="discovery-status playlist-auto-note">
              Auto-managed - overwritten on refresh. Save a copy to keep it.
            </p>
          )}
          <span className="page-toolbar-gap" />
          {isAutoManaged && (
            <button
              type="button"
              className="btn btn-ghost btn-sm playlist-tool-btn playlist-add-more"
              onClick={addMoreTracks}
              disabled={addingMore}
            >
              <PlusIcon aria-hidden="true" />
              <span>{addingMore ? 'Finding tracks…' : 'Add more tracks'}</span>
            </button>
          )}
          <a
            className="text-link playlist-export-link"
            href={`/api/playlists/${detail.id}/export.m3u`}
            download={`${detail.name}.m3u`}
          >
            <DownloadIcon aria-hidden="true" />
            Export .m3u
          </a>
        </div>
        {detail.items.length === 0 ? (
          <p className="empty">No tracks yet — add some from the library.</p>
        ) : reordering ? (
          <>
            <p className="discovery-status">Drag a track, or use its arrows, to change the order.</p>
            <PlaylistReorderList items={detail.items} onMove={(from, to) => moveTrack(detail.id, from, to)} />
          </>
        ) : (
          <TrackList
            key={detail.id}
            tracks={detail.items}
            playlists={playlists}
            onChanged={reloadOpenPlaylist}
            onPlaylistsChanged={onChanged}
            onOpenArtist={onOpenArtist}
            onOpenAlbum={onOpenAlbum}
            playSource={playSource}
            scrollContainerRef={scrollContainerRef}
            membershipTracksFavorites={detailPlaylist.auto_kind === 'favorites'}
            // Favourites has its own remove path (unfavorite, via the
            // heart) - a separate manual removal here would desync the
            // playlist from the Favorite row it's supposed to mirror.
            playlistId={detailPlaylist.auto_kind === 'favorites' ? undefined : detail.id}
          />
        )}
      </div>
    )
  }

  // Keep the playlist grid out of the first render after opening a playlist.
  // In particular, this prevents a Home mix click from visibly navigating to
  // the generic Playlists page before its own detail view arrives.
  if (openId != null) {
    return (
      <div>
        {/* A way back, so a playlist that will not load is not a dead end. */}
        <TopBarBack label={backLabel} onBack={onBack} />
        {detailState.status === 'error' ? (
          <ErrorState message={errorMessage(detailState.error, 'Could not load that playlist.')} onRetry={detailState.retry} />
        ) : (
          <LoadingState label="Loading playlist…" />
        )}
      </div>
    )
  }

  // "Permanent" = the user made it (Create, or a saved copy) or it's the
  // Favourites playlist (auto-managed, but never wholesale overwritten -
  // it only ever grows/shrinks one track at a time via favoriting).
  // "Generated" = Discovery, genre Mixes, and single-song "Song Mix:..."
  // playlists - all get regenerated/overwritten from Home, not something
  // to rely on staying put.
  const needle = filter.trim().toLowerCase()
  const shownPlaylists = needle ? playlists.filter((p) => p.name.toLowerCase().includes(needle)) : playlists
  const permanentPlaylists = shownPlaylists.filter((p) => !p.auto_kind || p.auto_kind === 'favorites')
  const generatedPlaylists = shownPlaylists.filter((p) => p.auto_kind && p.auto_kind !== 'favorites')

  function playlistGrid(list: Playlist[]) {
    return (
      <div className="browse-grid">
        {list.map((pl) => (
          <div key={pl.id} className="browse-card browse-card-playlist">
            <button
              className="browse-card-main"
              onClick={() => { if (!menu.consumeClick()) onOpenChange(pl.id) }}
              onContextMenu={(e) => menu.onContextMenu(e, pl.id)}
              onPointerDown={(e) => menu.onPointerDown(e, pl.id)}
              onPointerUp={menu.onPointerUp}
              onPointerLeave={menu.onPointerLeave}
              onPointerCancel={menu.onPointerCancel}
            >
              {customPlaylistArt(pl) ? (
                <Art
                  mediaItemId={null}
                  artRef={playlistRef(pl)}
                  className="art-tile"
                  fallback={pl.auto_kind === 'favorites' ? <HeartIcon filled /> : pl.auto_kind ? <SparkleIcon /> : <MusicNoteIcon />}
                />
              ) : pl.preview_media_item_ids.length > 0 ? (
                <div className="art-tile playlist-collage-wrap">
                  <MixCollage mediaItemIds={pl.preview_media_item_ids} />
                </div>
              ) : (
                <Art
                  mediaItemId={null}
                  className="art-tile"
                  fallback={pl.auto_kind === 'favorites' ? <HeartIcon filled /> : pl.auto_kind ? <SparkleIcon /> : <MusicNoteIcon />}
                />
              )}
              <div className="browse-title">{pl.name} <CollectionDownloadMark kind="playlist" itemKey={pl.id} subject={pl.name} /></div>
              <div className="browse-subtitle">
                {pl.item_count} {pl.item_count === 1 ? 'track' : 'tracks'}
              </div>
            </button>
            <MoreButton label={pl.name} open={menu.openFor === pl.id} menuId={menu.menuId} onOpen={(a) => menu.open(pl.id, a.x, a.y, a.trigger)} />
            {menu.openFor === pl.id && menu.anchor && (
              <PlaylistMenuPanel
                playlist={pl}
                anchor={menu.anchor}
                menuId={menu.menuId}
                onClose={menu.close}
                onDelete={pl.auto_kind === 'favorites' ? undefined : () => void remove(pl.id, pl.name)}
              />
            )}
            {pl.auto_kind !== 'favorites' && (
              <button
                className="browse-card-delete"
                onClick={() => remove(pl.id, pl.name)}
                aria-label={`Delete ${pl.name}`}
              >
                <TrashIcon aria-hidden="true" />
              </button>
            )}
          </div>
        ))}
      </div>
    )
  }

  return (
    <div className="playlists-view">
      {filter.trim() && (
        <div className="page-toolbar" role="group" aria-label="Applied search">
          <SearchChip query={filter.trim()} onClear={() => setFilter('')} />
        </div>
      )}
      <div className="sec-head playlists-heading">
        <h2>Your playlists</h2>
        <HelpTip
          label="About playlists"
          text="Create playlists, open one to play or edit it, and export it as an M3U file. Generated mixes and discovery lists appear separately below; save a copy of one to keep it."
          className="section-help-tip"
          buttonClassName="home-rail-help-button"
          bubbleClassName="section-help-bubble"
          align="start"
        />
      </div>
      <form className="playlist-create" action={create}>
        <input
          aria-label="New playlist name"
          placeholder="New playlist name"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          autoComplete="off"
        />
        <SubmitButton className="btn btn-primary" pendingLabel="Creating…">Create</SubmitButton>
      </form>

      {playlists.length === 0 ? (
        <p className="empty">No playlists yet.</p>
      ) : needle && shownPlaylists.length === 0 ? (
        <p className="empty">No playlists match “{filter.trim()}”.</p>
      ) : permanentPlaylists.length === 0 ? (
        <p className="empty">
          No playlists yet - create one above, or favourite a song to start one.
        </p>
      ) : (
        playlistGrid(permanentPlaylists)
      )}

      {/* The heading - and with it the Specify mix entry point - renders even
          when there is nothing generated yet, so creating a mix from this tab
          stays possible exactly when the list below is empty. */}
      <div className="sec-head tab-section-heading playlists-heading">
        <h2>Generated</h2>
        <HelpTip
          label="About generated playlists"
          text="Discovery, genre mixes, and song mixes are refreshed from Home and can be overwritten. Use Save as a playlist inside one to keep a permanent copy."
          className="section-help-tip"
          buttonClassName="home-rail-help-button"
          bubbleClassName="section-help-bubble"
          align="start"
        />
        <span className="page-toolbar-gap" />
        <button type="button" className="text-link specify-mix-trigger" onClick={() => setSpecifyMixOpen(true)}>
          <PlusIcon aria-hidden="true" />
          Specify mix
        </button>
      </div>
      {generatedPlaylists.length > 0 ? (
        <>
          {playlistGrid(generatedPlaylists)}
        </>
      ) : (
        <p className="empty">
          No generated playlists yet — use Specify mix to add a genre or tag mix.
        </p>
      )}

      {specifyMixOpen && (
        <Suspense fallback={null}>
        <SpecifyMixDialog
          hint="Choose a genre or library tag to add a temporary mix to the Generated list below."
          // Unlike Home there is nothing to navigate to straight after the
          // POST, so rather than keeping the fresh mix list here this just
          // refreshes App's playlist list - which is what makes the new mix
          // card show up in the Generated grid above.
          onCreated={() => {
            onChanged()
            showToast('Added a temporary mix to Generated.', 'success')
          }}
          onClose={() => setSpecifyMixOpen(false)}
        />
        </Suspense>
      )}
    </div>
  )
}
