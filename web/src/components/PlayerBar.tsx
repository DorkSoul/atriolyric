import { OverlayBoundary } from './OverlayBoundary'
import { Suspense, useEffect } from 'react'
import { onIdle } from '../idle'
import { usePlayer } from '../PlayerContext'
import type { Playlist, PlaySource } from '../types'
import { useIsDesktop } from '../useIsDesktop'
import { useIsWide } from '../useMediaQuery'
import { Art } from './Art'
import { DevicePicker } from './DevicePicker'
import { LibraryLinks } from './LibraryLinks'
import { MarqueeText } from './MarqueeText'
import { RadioIcon } from './BrowseIcons'
import { PauseIcon, PlayIcon, SkipNextIcon, SkipPrevIcon, SpeakerIcon } from './Icons'
import { trackPreference } from '../preference'
import { PreferenceHeart } from './PreferenceHeart'
import { ProgressSeek } from './ProgressSeek'
import { NowPlayingSkeleton, NowPlayingView, preloadNowPlaying, preloadNowPlayingProps } from './nowPlayingLazy'

/**
 * The mini player along the bottom (under 1200px wide; the side panel takes
 * its place above that, see SidePanel.tsx): a thin seek line across its top
 * edge, the art (opens Now Playing), title and artist, the heart, then
 * previous / play / next, and on desktop widths the volume and the playback
 * device. Also where Now Playing mounts, at every width.
 */
export function PlayerBar({
  playlists,
  onPlaylistsChanged,
  onOpenArtist,
  onOpenLabel,
  onOpenAlbum,
  onOpenPlaySource,
}: {
  playlists: Playlist[]
  onPlaylistsChanged?: () => void
  onOpenArtist?: (artist: string) => void
  onOpenLabel?: (label: { genre?: string; tag?: string }) => void
  onOpenAlbum?: (artist: string | null, album: string) => void
  onOpenPlaySource?: (source: PlaySource) => void
}) {
  const {
    current,
    isPlaying,
    volume,
    setVolume,
    devices,
    isActiveDevice,
    connected,
    reconnecting,
    nowPlayingOpen,
    openNowPlaying,
    closeNowPlaying,
    togglePlayPause,
    skipNext,
    skipPrev,
    cycleCurrentPreference,
    radioStation,
    isRadioPlaying,
    radioNowPlaying,
    toggleRadioPlayPause,
  } = usePlayer()
  const isDesktop = useIsDesktop()
  const isWide = useIsWide()
  const activeDevice = devices.find((d) => d.is_active && d.is_connected)

  // Something to open Now Playing for: fetch its code while the page is idle.
  const hasSomething = current != null || radioStation != null
  useEffect(() => {
    if (!hasSomething) return
    return onIdle(preloadNowPlaying)
  }, [hasSomething])

  const nowPlaying = nowPlayingOpen && (
    <OverlayBoundary what="Now Playing" onClose={closeNowPlaying}>
    <Suspense fallback={<NowPlayingSkeleton />}>
      <NowPlayingView
        onClose={closeNowPlaying}
        playlists={playlists}
        onPlaylistsChanged={onPlaylistsChanged}
        onOpenArtist={onOpenArtist}
        onOpenAlbum={onOpenAlbum}
        onOpenLabel={onOpenLabel}
        onOpenPlaySource={onOpenPlaySource}
      />
    </Suspense>
    </OverlayBoundary>
  )

  // The side panel carries the controls: no second set of them.
  if (isWide) return nowPlaying || null

  // A phone has the device picker in the top bar (TopBarDevicePicker) and the
  // volume on the hardware buttons and in Now Playing.
  const extras = isDesktop && (
    <div className="player-extras">
      <label className="player-volume" title="Volume">
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
      <DevicePicker />
    </div>
  )

  if (radioStation) {
    return (
      <>
        <div className="player-bar player-bar-radio" role="region" aria-label="Player">
          <div className="player-track-info">
            <button className="player-art-btn" onClick={openNowPlaying} aria-label="Open now playing" {...preloadNowPlayingProps}>
              <Art mediaItemId={null} className="art-sm" fallback={<RadioIcon />} />
            </button>

            <div className="track-title-text player-info-text">
              <div className="track-title">{radioStation.name}</div>
              <div className="track-subtitle">{radioNowPlaying ?? 'Live radio'}</div>
            </div>
          </div>

          <div className="player-controls">
            <button
              className="player-play-btn"
              onClick={toggleRadioPlayPause}
              aria-label={isRadioPlaying ? 'Pause' : 'Play'}
            >
              {isRadioPlaying ? <PauseIcon /> : <PlayIcon />}
            </button>
          </div>
          {extras}
        </div>
        {nowPlaying}
      </>
    )
  }

  if (!current) {
    return (
      <>
        <div className="player-bar player-bar-empty" role="region" aria-label="Player">
          <span className="player-empty-text">Nothing playing — pick a track to get started.</span>
          {extras}
        </div>
        {nowPlaying}
      </>
    )
  }

  return (
    <>
      <div className="player-bar" role="region" aria-label="Player">
        <ProgressSeek />

        <div className="player-track-info">
          <button className="player-art-btn" onClick={openNowPlaying} aria-label="Open now playing" {...preloadNowPlayingProps}>
            <Art mediaItemId={current.id} className="art-sm" thumbnail={current.kind !== 'track'} />
          </button>

          <div className="track-title-text player-info-text">
            <MarqueeText className="track-title marquee-left" text={current.title} always />
            <MarqueeText className="track-subtitle marquee-left" always>
              {current.kind === 'video' ? (
                current.performer ?? 'Video'
              ) : (
                <LibraryLinks
                  artist={current.artist}
                  artists={current.artists}
                  albumArtist={current.album_artist}
                  album={current.album}
                  onArtist={onOpenArtist}
                  onAlbum={onOpenAlbum}
                />
              )}
              {!isActiveDevice && connected && (
                <span className="player-remote-note"> · playing on {activeDevice?.name ?? 'another device'}</span>
              )}
              {reconnecting && <output className="player-remote-note"> · Reconnecting…</output>}
            </MarqueeText>
          </div>

          <PreferenceHeart
            state={trackPreference(current)}
            onChange={cycleCurrentPreference}
            subject={current.title}
            className="player-pref-btn"
            activeClassName="active"
          />
        </div>

        <div className="player-controls">
          <button onClick={skipPrev} aria-label="Previous">
            <SkipPrevIcon />
          </button>
          <button className="player-play-btn" onClick={togglePlayPause} aria-label={isPlaying ? 'Pause' : 'Play'}>
            {isPlaying ? <PauseIcon /> : <PlayIcon />}
          </button>
          <button onClick={skipNext} aria-label="Next">
            <SkipNextIcon />
          </button>
        </div>

        {extras}
      </div>
      {nowPlaying}
    </>
  )
}
