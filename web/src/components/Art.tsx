import { useEffect, useRef, useState, type ReactNode } from 'react'
import { MusicNoteIcon } from './Icons'
import { loadArt, type ArtLoadHandle } from '../artLoadQueue'
import { api } from '../api'
import { scrollRootFor, useArtScrollRoot } from '../artScrollRoot'
import { observeIntersection } from '../sharedIntersection'
import { customArtUrl, effectiveArt, useArtRevision, withArtVersion, type ArtRef } from '../artEntity'

interface Props {
  mediaItemId: number | null
  alt?: string
  className?: string
  /** An icon (from Icons.tsx) shown when there is no picture. */
  fallback?: ReactNode
  /** Videos don't have embedded cover art - use their generated frame
   * thumbnail instead of the (audio-only) art endpoint. */
  thumbnail?: boolean
  /** The un-resized original, for a context that actually displays the
   * image large (Now Playing) - every other context wants the default
   * resized copy, which is what keeps a grid of a hundred tiles from each
   * pulling a multi-megabyte embedded picture. */
  full?: boolean
  /** Lower priority for paged song rows so text can paint first. */
  defer?: boolean
  /** Identifies the item for custom artwork: a custom album/artist/playlist
   * image replaces the representative track's art, and the item's art
   * version cache-busts the image URL. */
  artRef?: ArtRef
}

// How far outside its scroll container's visible area a tile still counts
// as "worth loading" (or "not yet worth unloading") - one generous margin
// used both ways, so scrolling a screen or so past a loaded tile doesn't
// immediately drop it, but a library with thousands of tiles never has more
// than a viewport's worth (plus this margin) actually decoded in memory at
// once.
const VIEWPORT_MARGIN = '1000px 0px'

// Safety net for releaseObjectUrl: a removed <img> that never reports its
// load settling still gets its object URL released eventually.
const RELEASE_FALLBACK_MS = 30_000

/** Revokes a tile's object URL without pulling it out from under an <img>
 * still loading it - including one React already removed, which keeps
 * loading, and which WebKit reports as a failed load if its URL is revoked
 * first. A loaded <img> keeps its image, so it can be revoked at once. */
function releaseObjectUrl(url: string, img: HTMLImageElement | null) {
  if (!img || img.getAttribute('src') !== url || img.complete) {
    URL.revokeObjectURL(url)
    return
  }
  const release = () => {
    window.clearTimeout(fallback)
    img.removeEventListener('load', release)
    img.removeEventListener('error', release)
    URL.revokeObjectURL(url)
  }
  const fallback = window.setTimeout(release, RELEASE_FALLBACK_MS)
  img.addEventListener('load', release)
  img.addEventListener('error', release)
}

export function Art({
  mediaItemId,
  alt = '',
  className = '',
  fallback = <MusicNoteIcon />,
  thumbnail = false,
  full = false,
  defer = false,
  artRef,
}: Props) {
  // Re-evaluate whenever any art is uploaded or reset in this session.
  useArtRevision()
  let targetUrl: string | null
  if (artRef && artRef.type !== 'media_item' && effectiveArt(artRef).custom) {
    targetUrl = customArtUrl(artRef, full)
  } else if (mediaItemId == null) {
    targetUrl = null
  } else {
    const base = thumbnail
      ? api.thumbnailUrl(mediaItemId)
      : full
        ? api.artFullUrl(mediaItemId)
        : api.artUrl(mediaItemId)
    targetUrl = artRef?.type === 'media_item' ? withArtVersion(base, artRef) : base
  }

  const wrapRef = useRef<HTMLDivElement>(null)
  const scrollRootRef = useArtScrollRoot()
  const [nearViewport, setNearViewport] = useState(false)
  const [visible, setVisible] = useState(false)
  const visibleRef = useRef(false)

  // A library with thousands of tiles must never try to load all of them -
  // only ones actually near the viewport ever request an image at all. The
  // margin is measured against the scroll container (see artScrollRoot.ts).
  // A second observer, against the viewport with no margin, tells the loader
  // which of those are on screen right now, so they load first.
  // Both observers are shared by every tile under the same scroll root (see
  // sharedIntersection.ts).
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const stopNear = observeIntersection(el, scrollRootFor(scrollRootRef, el), VIEWPORT_MARGIN, (entry) =>
      setNearViewport(entry.isIntersecting),
    )
    const stopVisible = observeIntersection(el, null, '0px', (entry) => {
      visibleRef.current = entry.isIntersecting
      setVisible(entry.isIntersecting)
    })
    return () => {
      stopNear()
      stopVisible()
    }
  }, [scrollRootRef])

  // An object URL from the shared art loader, or null whenever this tile
  // isn't near the viewport (never requested yet, or scrolled far enough
  // past to unload again) or its turn hasn't come yet. The loader does the
  // fetching - so it can interrupt downloads nobody is waiting for any
  // more - which is also why there's no native `loading="lazy"` here: the
  // <img> only ever displays bytes that have already arrived.
  const [src, setSrc] = useState<string | null>(null)
  const [failedUrl, setFailedUrl] = useState<string | null>(null)
  const handleRef = useRef<ArtLoadHandle | null>(null)
  // The last <img> rendered - kept after React removes it, since it may
  // still be loading the object URL being released.
  const imgRef = useRef<HTMLImageElement | null>(null)

  useEffect(() => {
    if (!nearViewport || targetUrl == null) return

    let objectUrl: string | null = null
    const start = () => {
      handleRef.current = loadArt(targetUrl, visibleRef.current, (result) => {
        if ('failed' in result) {
          setFailedUrl(targetUrl)
          return
        }
        objectUrl = result.objectUrl
        setFailedUrl(null)
        setSrc(objectUrl)
      })
    }
    const timer = defer ? window.setTimeout(start, 50) : undefined
    if (!defer) start()

    return () => {
      if (timer != null) window.clearTimeout(timer)
      handleRef.current?.cancel()
      handleRef.current = null
      setSrc(null)
      if (objectUrl != null) releaseObjectUrl(objectUrl, imgRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nearViewport, targetUrl])

  useEffect(() => {
    handleRef.current?.setVisible(visible)
  }, [visible])

  // Wanted (near the viewport) but not here yet: queued behind other tiles or
  // still downloading. Tiles far off screen aren't loading, so show nothing.
  const loading = nearViewport && targetUrl != null && src == null && failedUrl !== targetUrl

  return (
    <div ref={wrapRef} className={`art-wrap ${className}`}>
      {src != null && (
        <img
          ref={(el) => {
            if (el) imgRef.current = el
          }}
          src={src}
          alt={alt}
          className="art-img"
          // Loaded, the <img> keeps its image - the blob can go now.
          onLoad={() => URL.revokeObjectURL(src)}
          onError={(e) => {
            e.currentTarget.style.display = 'none'
            e.currentTarget.nextElementSibling?.classList.remove('hidden')
          }}
        />
      )}
      {loading && <span className="art-spinner" aria-hidden="true" />}
      <div className={`art-fallback ${targetUrl != null && failedUrl !== targetUrl ? 'hidden' : ''}`}>{fallback}</div>
    </div>
  )
}
