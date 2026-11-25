import { Fragment, useEffect, useRef } from 'react'
import { api } from '../api'
import { usePlayerActions } from '../PlayerContext'
import { mediaRef, withArtVersion } from '../artEntity'
import { formatDuration } from '../formatDuration'
import { useLongPressMenu } from '../useLongPressMenu'
import type { MediaItem } from '../types'
import { VideoIcon } from './Icons'
import { MoreButton } from './MoreMenu'
import { VideoMenuPanel } from './VideoMenuPanel'
import { LibraryLinks } from './LibraryLinks'
import { useArtScrollRoot } from '../artScrollRoot'
import { useVirtualGridRange } from '../useVirtualGridRange'
import { useToast } from '../ToastContext'
import { nativeCall } from '../native/bridge'
import { DownloadMark } from '../native/DownloadMark'
import { useDownloadedIds } from '../native/downloadStore'
import { NOT_DOWNLOADED_MESSAGE } from '../native/localPlayback'
import { useOffline } from '../native/offline'

interface Props {
  videos: MediaItem[]
  onLoadMore?: () => void
  hasMore?: boolean
  onOpenArtist?: (artist: string) => void
  onOpenAlbum?: (artist: string | null, album: string) => void
}

export function VideoList({ videos, onLoadMore, hasMore, onOpenArtist, onOpenAlbum }: Props) {
  const { playQueue } = usePlayerActions()
  const { showToast } = useToast()
  const offline = useOffline()
  const downloaded = useDownloadedIds()
  const menu = useLongPressMenu<number>()
  const sentinelRef = useRef<HTMLDivElement>(null)
  // Only the rows near the viewport are drawn (a folder can hold hundreds of
  // videos); the grid's own CSS decides how many fit in a row.
  const scrollRoot = useArtScrollRoot()
  const { start, end, topPad, bottomPad, setContainerNode } = useVirtualGridRange(videos.length, {
    scrollContainerRef: scrollRoot,
    tileSelector: '.video-card',
    tileSizeVar: '--video-tile-size',
    tileSizeDefault: '7.25rem',
  })

  useEffect(() => {
    if (!hasMore || !onLoadMore) return
    const el = sentinelRef.current
    if (!el) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) onLoadMore()
      },
      { rootMargin: '800px' },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [hasMore, onLoadMore])

  return (
    <>
      <div className="video-grid" ref={setContainerNode}>
        {topPad > 0 && <div className="browse-grid-spacer" style={{ height: topPad }} aria-hidden />}
        {videos.slice(start, end).map((video, offset) => {
          const index = start + offset
          return (
          // The menu panel is a sibling of the card, not a child: it is
          // position: fixed, so it takes no grid cell, and clicks/keys inside
          // it must not reach the card's play handlers.
          <Fragment key={video.id}>
          {/* Nothing interactive nests in another interactive element: the
              play button is an overlay filling the card (the thumbnail and
              text above it pass presses through), and the artist and album
              links and the ⋯ button sit above it as its siblings. */}
          {/* oxlint-disable-next-line jsx-a11y/no-static-element-interactions */}
          <div
            className={`video-card ${offline && !downloaded.has(video.id) ? 'video-card-unavailable' : ''}`}
            onContextMenu={(e) => menu.onContextMenu(e, video.id)}
            onPointerDown={(e) => menu.onPointerDown(e, video.id)}
            onPointerUp={menu.onPointerUp}
            onPointerLeave={menu.onPointerLeave}
            onPointerCancel={menu.onPointerCancel}
          >
            <button
              type="button"
              className="video-play-btn"
              aria-label={`Play ${video.title}`}
              onClick={() => {
                if (menu.consumeClick()) return
                // A video on this device plays from there, in the app's own
                // player, with or without a connection.
                if (downloaded.has(video.id)) {
                  void nativeCall('playVideo', { mediaId: video.id }).catch(() => showToast('Could not play this video.', 'error'))
                } else if (offline) {
                  showToast(NOT_DOWNLOADED_MESSAGE, 'error', 'not-downloaded')
                } else {
                  playQueue(videos, index)
                }
              }}
            />
            <div className="video-thumb-wrap">
              <img
                src={withArtVersion(api.thumbnailUrl(video.id), mediaRef(video))}
                alt=""
                className="video-thumb"
                loading="lazy"
                onError={(e) => {
                  e.currentTarget.style.display = 'none'
                  e.currentTarget.nextElementSibling?.classList.remove('hidden')
                }}
              />
              <div className="video-thumb-fallback hidden"><VideoIcon /></div>
              <MoreButton
                label={video.title}
                className="more-btn-overlay"
                open={menu.openFor === video.id}
                menuId={menu.menuId}
                onOpen={(a) => menu.open(video.id, a.x, a.y, a.trigger)}
              />
              <DownloadMark mediaId={video.id} subject={video.title} />
              {video.duration_seconds != null && (
                <span className="video-duration">{formatDuration(video.duration_seconds)}</span>
              )}
            </div>
            <div className="video-title">{video.title}</div>
            <div className="track-subtitle">
              {video.kind === 'music_video' ? (
                <LibraryLinks artist={video.artist} artists={video.artists} albumArtist={video.album_artist} album={video.album} onArtist={onOpenArtist} onAlbum={onOpenAlbum} />
              ) : (
                video.performer ?? 'Video'
              )}
            </div>
          </div>
          {menu.openFor === video.id && menu.anchor && (
            <VideoMenuPanel video={video} anchor={menu.anchor} menuId={menu.menuId} onClose={menu.close} />
          )}
          </Fragment>
          )
        })}
        {bottomPad > 0 && <div className="browse-grid-spacer" style={{ height: bottomPad }} aria-hidden />}
      </div>
      {hasMore && <div ref={sentinelRef} aria-hidden />}
    </>
  )
}
