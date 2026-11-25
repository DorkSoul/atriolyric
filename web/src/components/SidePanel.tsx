import { Suspense, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react'
import { useSettings } from '../SettingsContext'
import { applySidePanelWidth } from '../settings'
import { ArtScrollRootProvider } from '../artScrollRoot'
import { lazyView } from '../lazyView'
import type { Playlist } from '../types'
import { useIsWide } from '../useMediaQuery'
import { ErrorBoundary } from './ErrorBoundary'

// The panel's contents load after the shell and the page have painted: the
// panel is a column of its own, sized by the stylesheet, so nothing moves
// when they arrive.
const SidePanelContent = lazyView(() => import('./SidePanelContent').then((m) => m.SidePanelContent))

export interface SidePanelProps {
  playlists: Playlist[]
  onPlaylistsChanged?: () => void
  onOpenArtist?: (artist: string) => void
  onOpenAlbum?: (artist: string | null, album: string) => void
}

/**
 * The desktop side panel (1200px and wider, in place of the player bar): the
 * playing song with its controls, then Up next. Mounted once in the shell, so
 * the panel - and with it its scroll position - survives tab changes. The
 * whole panel is one scroll area.
 */
export function SidePanel(props: SidePanelProps) {
  const isWide = useIsWide()
  const scrollRef = useRef<HTMLElement>(null)
  if (!isWide) return null
  return (
    <aside className="side-panel" ref={scrollRef} aria-label="Now playing and queue">
      <SidePanelResizer panelRef={scrollRef} />
      {/* Queue covers load as they near the panel's own edges, not the page's. */}
      <ArtScrollRootProvider value={scrollRef}>
        <ErrorBoundary variant="view">
          <Suspense fallback={null}>
            <SidePanelContent {...props} scrollRef={scrollRef} />
          </Suspense>
        </ErrorBoundary>
      </ArtScrollRootProvider>
    </aside>
  )
}

const KEY_STEP_PX = 16

/** The panel's allowed widths in px: the stylesheet owns both ends (its
 * min-width is the default width, its max-width follows the window). */
function widthBounds(panel: HTMLElement): { min: number; max: number } {
  const style = getComputedStyle(panel)
  const min = parseFloat(style.minWidth) || 320
  const max = Math.max(min, parseFloat(style.maxWidth) || min)
  return { min, max }
}

/**
 * The strip on the panel's left edge that resizes it: drag it (mouse, touch
 * or pen), or focus it and use the arrow keys; a double-click goes back to
 * the default width. Dragging writes the CSS variable directly so the
 * layout follows the pointer without a React render per move, and the
 * width is saved once, on release.
 */
function SidePanelResizer({ panelRef }: { panelRef: React.RefObject<HTMLElement | null> }) {
  const { settings, updateSettings } = useSettings()
  const [bounds, setBounds] = useState({ min: 320, max: 320, now: 320 })
  const dragging = useRef(false)

  // Mirror the real width into the separator's aria values.
  useLayoutEffect(() => {
    const panel = panelRef.current
    if (!panel) return
    function measure() {
      if (!panel) return
      const { min, max } = widthBounds(panel)
      setBounds({ min, max, now: Math.round(panel.getBoundingClientRect().width) })
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(panel)
    return () => observer.disconnect()
  }, [panelRef])

  function clampWidth(width: number): number {
    const panel = panelRef.current
    if (!panel) return width
    const { min, max } = widthBounds(panel)
    return Math.round(Math.min(max, Math.max(min, width)))
  }

  function widthAt(clientX: number): number {
    const panel = panelRef.current
    if (!panel) return 0
    return clampWidth(panel.getBoundingClientRect().right - clientX)
  }

  function onPointerDown(e: PointerEvent<HTMLDivElement>) {
    if (e.pointerType === 'mouse' && e.button !== 0) return
    e.preventDefault()
    dragging.current = true
    e.currentTarget.setPointerCapture(e.pointerId)
    document.documentElement.classList.add('side-panel-resizing')
  }

  function onPointerMove(e: PointerEvent<HTMLDivElement>) {
    if (!dragging.current) return
    applySidePanelWidth(widthAt(e.clientX))
  }

  function endDrag(e: PointerEvent<HTMLDivElement>, save: boolean) {
    if (!dragging.current) return
    dragging.current = false
    document.documentElement.classList.remove('side-panel-resizing')
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId)
    if (save) updateSettings({ sidePanelWidth: widthAt(e.clientX) })
    else applySidePanelWidth(settings.sidePanelWidth)
  }

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    // The panel is on the right, so moving its edge left widens it.
    const step = e.key === 'ArrowLeft' ? KEY_STEP_PX : e.key === 'ArrowRight' ? -KEY_STEP_PX : 0
    if (!step) return
    e.preventDefault()
    updateSettings({ sidePanelWidth: clampWidth(bounds.now + step) })
  }

  return (
    <div
      className="side-panel-resizer"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize now playing panel"
      aria-valuenow={bounds.now}
      aria-valuemin={Math.round(bounds.min)}
      aria-valuemax={Math.round(bounds.max)}
      tabIndex={0}
      title="Drag to resize, double-click to reset"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={(e) => endDrag(e, true)}
      onPointerCancel={(e) => endDrag(e, false)}
      onDoubleClick={() => updateSettings({ sidePanelWidth: null })}
      onKeyDown={onKeyDown}
    />
  )
}
