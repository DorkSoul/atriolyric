import { createPortal } from 'react-dom'
import { lazyView } from '../lazyView'
import { usePlayer } from '../PlayerContext'
import { useIsDesktop } from '../useIsDesktop'

/** Where Now Playing renders as a page (App.tsx, over <main>). */
export const NOW_PLAYING_PAGE_HOST_ID = 'nowplaying-page-host'

/** Now Playing is a page in the app's content area on a desktop or tablet
 * (sidebar and top bar stay), and a full-screen sheet on a phone. A video
 * keeps its full-bleed sheet everywhere: it is drawn over the whole window. */
export function nowPlayingAsPage(
  isDesktop: boolean,
  current: { kind: string } | null | undefined,
  radio: boolean,
): boolean {
  return isDesktop && (radio || current == null || current.kind === 'track')
}

/** True while Now Playing is open as a page (see nowPlayingAsPage). */
export function useNowPlayingAsPage(): boolean {
  const { nowPlayingOpen, current, radioStation } = usePlayer()
  const isDesktop = useIsDesktop()
  return nowPlayingOpen && nowPlayingAsPage(isDesktop, current, radioStation != null)
}

// Now Playing's code loads on demand. It is preloaded once something is
// playing (on idle, see PlayerBar) and when the pointer reaches a control that
// opens it, so opening it rarely waits; when it does, NowPlayingSkeleton
// holds the sheet's place.
export const NowPlayingView = lazyView(() => import('./NowPlayingView').then((m) => m.NowPlayingView))

export function preloadNowPlaying() {
  NowPlayingView.preload()
}

/** Props for a control that opens Now Playing: its code starts downloading
 * the moment the pointer is over it, or it gets keyboard focus. */
export const preloadNowPlayingProps = {
  onPointerEnter: preloadNowPlaying,
  onPointerDown: preloadNowPlaying,
  onFocus: preloadNowPlaying,
}

/** The empty sheet shown while Now Playing's code is still on its way. */
export function NowPlayingSkeleton() {
  const { current, radioStation } = usePlayer()
  const isDesktop = useIsDesktop()
  const host = document.getElementById(NOW_PLAYING_PAGE_HOST_ID)
  const content = (
    <div className="nowplaying-content" role="status" aria-label="Opening Now Playing" aria-busy="true">
      <p className="loading">Loading…</p>
    </div>
  )
  if (host && nowPlayingAsPage(isDesktop, current, radioStation != null)) {
    return createPortal(<div className="nowplaying-page nowplaying-skeleton">{content}</div>, host)
  }
  return createPortal(<div className="nowplaying-overlay nowplaying-skeleton">{content}</div>, document.body)
}
