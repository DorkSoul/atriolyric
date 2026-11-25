import { useId, useState, type RefObject } from 'react'
import { preloadNowPlayingProps } from './nowPlayingLazy'
import { useAuth } from '../AuthContext'
import { mediaRef } from '../artEntity'
import type { MenuAnchor } from '../menuTrigger'
import { usePlayer } from '../PlayerContext'
import { trackPreference } from '../preference'
import type { MediaItem, Playlist } from '../types'
import { useHasLyrics } from '../useHasLyrics'
import { Art } from './Art'
import { RadioIcon } from './BrowseIcons'
import { MusicNoteIcon, PauseIcon, PlayIcon, RepeatIcon, ShuffleIcon, SkipNextIcon, SkipPrevIcon, SpeakerIcon, StopIcon } from './Icons'
import { AlbumLink, LibraryLinks } from './LibraryLinks'
import { LyricsPanel } from './LyricsPanel'
import { MoreButton } from './MoreMenu'
import { PreferenceHeart } from './PreferenceHeart'
import { PlayerTime, ProgressSeek } from './ProgressSeek'
import { QueueList, QueuePreviousToggle } from './QueueList'
import { useQueueCountLabel } from '../queueCount'
import type { SidePanelProps } from './SidePanel'
import { TagEditModal } from './TagEditModal'
import { TrackMenuPanel } from './TrackMenuPanel'

function VolumeControl() {
  const { volume, setVolume } = usePlayer()
  return (
    <label className="player-volume side-panel-volume" title="Volume">
      <SpeakerIcon />
      <input
        type="range"
        min={0}
        max={1}
        step={0.01}
        value={volume}
        onChange={(e) => setVolume(Number(e.target.value))}
        aria-label="Volume"
        aria-valuetext={`${Math.round(volume * 100)}%`}
      />
    </label>
  )
}

/** The song menu Now Playing's "more" button opens. Mounted only while open,
 * so the lyrics look-up behind "View lyrics" waits until someone asks. */
function SidePanelTrackMenu({
  track,
  playlists,
  anchor,
  menuId,
  onClose,
  onViewLyrics,
  onEditTags,
  onPlaylistsChanged,
}: {
  track: MediaItem
  playlists: Playlist[]
  anchor: MenuAnchor
  menuId: string
  onClose: () => void
  onViewLyrics: () => void
  onEditTags: () => void
  onPlaylistsChanged?: () => void
}) {
  const { user } = useAuth()
  const hasLyrics = useHasLyrics(track.id, track.kind === 'track')
  return (
    <TrackMenuPanel
      track={track}
      playlists={playlists}
      isAdmin={!!user?.is_admin}
      anchor={anchor}
      menuId={menuId}
      alsoInside={['.side-panel-now']}
      onClose={onClose}
      onViewLyrics={hasLyrics ? onViewLyrics : undefined}
      onEditTags={user?.is_admin ? onEditTags : undefined}
      onPlaylistsChanged={onPlaylistsChanged}
    />
  )
}

function UpNextHeader({ showPrevious, onTogglePrevious }: { showPrevious: boolean; onTogglePrevious: () => void }) {
  const { isShuffled, repeatMode, toggleShuffle, cycleRepeat } = usePlayer()
  const count = useQueueCountLabel()
  return (
    <div className="side-panel-upnext">
      <h2 className="side-panel-upnext-title">Up next</h2>
      <span className="side-panel-upnext-count">{count}</span>
      <QueuePreviousToggle open={showPrevious} onToggle={onTogglePrevious} />
      <button
        className={`side-panel-icon-btn ${isShuffled ? 'active' : ''}`}
        onClick={toggleShuffle}
        aria-label={isShuffled ? 'Shuffle on' : 'Shuffle off'}
        aria-pressed={isShuffled}
        title={isShuffled ? 'Shuffle on' : 'Shuffle off'}
      >
        <ShuffleIcon />
      </button>
      <button
        className={`side-panel-icon-btn ${repeatMode !== 'off' ? 'active' : ''}`}
        onClick={cycleRepeat}
        aria-label={repeatMode === 'off' ? 'Repeat off' : repeatMode === 'all' ? 'Repeat all' : 'Repeat one'}
        aria-pressed={repeatMode !== 'off'}
        title={repeatMode === 'off' ? 'Repeat off' : repeatMode === 'all' ? 'Repeat all' : 'Repeat one'}
      >
        <RepeatIcon mode={repeatMode} />
      </button>
    </div>
  )
}

/** What the side panel shows (SidePanel.tsx loads it on demand). The
 * `.side-panel-player` block carries the same accessible names as the player
 * bar - "Open now playing", Previous / Play / Pause / Next, Seek, Volume and
 * the heart - so whichever of the two is on screen answers the same way. */
export function SidePanelContent({
  playlists,
  onPlaylistsChanged,
  onOpenArtist,
  onOpenAlbum,
  scrollRef,
}: SidePanelProps & { scrollRef: RefObject<HTMLElement | null> }) {
  const {
    current,
    isPlaying,
    openNowPlaying,
    nowPlayingOpen,
    togglePlayPause,
    skipNext,
    skipPrev,
    cycleCurrentPreference,
    radioStation,
    isRadioPlaying,
    radioNowPlaying,
    toggleRadioPlayPause,
    stopRadio,
  } = usePlayer()
  const [showPrevious, setShowPrevious] = useState(false)
  const [menuAnchor, setMenuAnchor] = useState<MenuAnchor | null>(null)
  const [lyricsOpen, setLyricsOpen] = useState(false)
  const [editingTags, setEditingTags] = useState(false)
  const menuId = useId()

  if (radioStation) {
    return (
      <div className="side-panel-player side-panel-player-radio" role="region" aria-label="Player">
        <button className="side-panel-art player-art-btn" onClick={openNowPlaying} aria-label="Open now playing" {...preloadNowPlayingProps}>
          <Art mediaItemId={null} className="art-side-panel" fallback={<RadioIcon />} />
        </button>
        <div className="side-panel-info player-track-info">
          <div className="track-title-text player-info-text">
            <div className="track-title side-panel-title">{radioStation.name}</div>
            <div className="track-subtitle">{radioNowPlaying ?? 'Live radio'}</div>
            <div className="side-panel-album">
              <span className="badge">Live radio</span>
            </div>
          </div>
        </div>
        <div className="side-panel-controls side-panel-transport side-panel-transport-radio player-controls">
          <span aria-hidden="true" />
          <button
            className="side-panel-play"
            onClick={toggleRadioPlayPause}
            aria-label={isRadioPlaying ? 'Pause' : 'Play'}
          >
            {isRadioPlaying ? <PauseIcon /> : <PlayIcon />}
          </button>
          <button className="radio-stop-btn side-panel-stop" onClick={stopRadio} aria-label="Stop">
            <StopIcon aria-hidden="true" /> Stop
          </button>
        </div>
        <div className="side-panel-volume-row">
          <VolumeControl />
        </div>
      </div>
    )
  }

  if (!current) {
    return (
      <div className="side-panel-player side-panel-player-empty" role="region" aria-label="Player">
        <div className="side-panel-empty-art" aria-hidden="true">
          <MusicNoteIcon />
        </div>
        <p className="side-panel-empty-text">Nothing playing — pick a track to get started.</p>
        <div className="side-panel-controls player-controls">
          <VolumeControl />
        </div>
      </div>
    )
  }

  const openMenuAt = (anchor: MenuAnchor) => setMenuAnchor(anchor)
  const upNext = (
    <section className="side-panel-queue" aria-label="Up next">
      <UpNextHeader showPrevious={showPrevious} onTogglePrevious={() => setShowPrevious((open) => !open)} />
      <QueueList onOpenArtist={onOpenArtist} showPrevious={showPrevious} scrollRef={scrollRef} />
    </section>
  )

  // The Now Playing page (a song; a video keeps its sheet) shows the song and
  // its controls in the middle, so the panel keeps only Up next meanwhile.
  if (nowPlayingOpen && current.kind === 'track') return upNext

  return (
    <>
      <div className="side-panel-player side-panel-now" role="region" aria-label="Player">
        {/* The artwork, with the volume upright to its right, as tall as it. */}
        <div className="side-panel-art-row">
        <button
          className="side-panel-art player-art-btn"
          onClick={openNowPlaying}
          {...preloadNowPlayingProps}
          onContextMenu={(e) => {
            e.preventDefault()
            openMenuAt({ x: e.clientX, y: e.clientY })
          }}
          aria-label="Open now playing"
        >
          <Art
            mediaItemId={current.id}
            artRef={mediaRef(current)}
            className="art-side-panel"
            thumbnail={current.kind !== 'track'}
          />
        </button>
        <div className="side-panel-volume-col">
          <VolumeControl />
        </div>
        </div>
        {/* Under the artwork: the song, seek bar and controls, full width. */}
        <div className="side-panel-main">
        <div className="side-panel-info player-track-info">
          <div className="track-title-text player-info-text">
            <div className="track-title side-panel-title" title={current.title}>
              {current.title}
            </div>
            <div className="track-subtitle">
              {current.kind === 'video' ? (
                current.performer ?? 'Video'
              ) : (
                <LibraryLinks artist={current.artist} artists={current.artists} onArtist={onOpenArtist} />
              )}
            </div>
            {current.kind !== 'video' && current.album && (
              <div className="side-panel-album">
                <AlbumLink album={current.album} albumOwner={current.album_artist ?? current.artist ?? null} onAlbum={onOpenAlbum} />
              </div>
            )}
          </div>
          <div className="side-panel-actions">
            <PreferenceHeart
              state={trackPreference(current)}
              onChange={cycleCurrentPreference}
              subject={current.title}
              className="player-pref-btn side-panel-icon-btn"
              activeClassName="active"
            />
            <MoreButton
              label={current.title}
              className="side-panel-more"
              open={menuAnchor != null}
              menuId={menuId}
              onOpen={openMenuAt}
            />
          </div>
        </div>
        <div className="side-panel-seek">
          <PlayerTime />
          <ProgressSeek className="player-progress-inline" />
          <PlayerTime total />
        </div>
        {/* Previous / play / next, with play in the middle of the panel. */}
        <div className="side-panel-controls side-panel-transport side-panel-transport-centred player-controls">
          <button onClick={skipPrev} aria-label="Previous">
            <SkipPrevIcon />
          </button>
          <button className="side-panel-play" onClick={togglePlayPause} aria-label={isPlaying ? 'Pause' : 'Play'}>
            {isPlaying ? <PauseIcon /> : <PlayIcon />}
          </button>
          <button onClick={skipNext} aria-label="Next">
            <SkipNextIcon />
          </button>
        </div>
        </div>
        {menuAnchor && (
          <SidePanelTrackMenu
            track={current}
            playlists={playlists}
            anchor={menuAnchor}
            menuId={menuId}
            onClose={() => setMenuAnchor(null)}
            onViewLyrics={() => setLyricsOpen(true)}
            onEditTags={() => setEditingTags(true)}
            onPlaylistsChanged={onPlaylistsChanged}
          />
        )}
      </div>

      {upNext}

      {lyricsOpen && <LyricsPanel track={current} onClose={() => setLyricsOpen(false)} onOpenArtist={onOpenArtist} />}
      {editingTags && (
        <TagEditModal items={[current]} onClose={() => setEditingTags(false)} onSaved={() => setEditingTags(false)} />
      )}
    </>
  )
}
