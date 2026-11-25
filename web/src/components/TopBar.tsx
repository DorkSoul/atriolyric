import type { ReactNode, Ref } from 'react'

interface Props {
  /** The page's one h1. */
  title: string
  /** Kept for screen readers only: on Home (the search takes its place) and
   * on a detail page (the back link does). */
  titleHidden: boolean
  /** Shown after the title, e.g. "1,284". */
  count?: ReactNode
  headingRef: Ref<HTMLHeadingElement>
  /** Mount node for TopBarTitlePortal (Home search, back links, title tips). */
  titleSlotRef: Ref<HTMLDivElement>
  /** Mount node for HeaderPortal (the active tab's sort/view controls). */
  controlsRef: Ref<HTMLDivElement>
  /** Right-hand side: the user name and the settings menu. */
  actions: ReactNode
}

/** The bar above the page, one fixed height on every tab so nothing below it
 * moves when switching. Left: the title (or what a view portals in place of
 * it). Then the active view's controls (on a phone, a row of their own under
 * the bar), then `actions`. */
export function TopBar({ title, titleHidden, count, headingRef, titleSlotRef, controlsRef, actions }: Props) {
  return (
    <header className="app-header top-bar">
      <div className="top-bar-title">
        <h1 ref={headingRef} className={titleHidden ? 'top-bar-heading visually-hidden' : 'top-bar-heading'}>
          {title}
        </h1>
        {count != null && !titleHidden && <span className="top-bar-count">{count}</span>}
        <div className="top-bar-slot" ref={titleSlotRef} />
      </div>
      {/* Portal target for the active view's sort / view-mode / search icons
          (headerControlsPortal.tsx). Hidden by :empty on other tabs. */}
      <div className="browse-header-controls" ref={controlsRef} />
      <div className="top-bar-actions">{actions}</div>
    </header>
  )
}
