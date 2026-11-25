import { createContext, useContext, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useNowPlayingOpen } from './PlayerContext'
import { useViewActive } from './viewActive'
import { ArrowLeftIcon } from './components/Icons'

// Two slots in the top bar (components/TopBar.tsx) that the active tab's view
// fills from inside its own tree, so the state behind what it puts there
// stays owned by the view instead of being lifted into App.tsx:
//
// - the controls slot (`div.browse-header-controls`): sort, view-mode and
//   search-popover buttons. On the right of the top bar on desktop, a toolbar
//   row under it on a phone. Empty (and hidden by `:empty`) on other tabs.
// - the title slot (`div.top-bar-slot`), beside the top bar's title: Home's
//   library search, a detail page's back link (see TopBarBack), a help tip
//   that belongs to the page title.
//
// App.tsx owns both mount nodes. A hidden (kept-mounted) tab renders nothing
// into either, so only the active tab's controls show.
const HeaderControlsPortalContext = createContext<HTMLElement | null>(null)
const TopBarTitlePortalContext = createContext<HTMLElement | null>(null)

export const HeaderControlsPortalProvider = HeaderControlsPortalContext
export const TopBarTitlePortalProvider = TopBarTitlePortalContext
// A third slot, at the start of the header's user actions (left of the
// playback device picker): Home's Customise button.
const HeaderActionsPortalContext = createContext<HTMLElement | null>(null)
export const HeaderActionsPortalProvider = HeaderActionsPortalContext

/** Whether the page under it may fill the header: it must be the active tab,
 * and Now Playing must not be open over it - Now Playing shows in the main
 * area on top of the page it was opened from, which stays the active tab
 * underneath (so Back returns to it), and its header controls (Home's
 * Customise, a list's sort) do not belong on Now Playing. */
function useOwnsHeader(): boolean {
  const active = useViewActive()
  const nowPlayingOpen = useNowPlayingOpen()
  return active && !nowPlayingOpen
}

/** Renders children into the header actions slot, before the device picker. */
export function HeaderActionsPortal({ children }: { children: ReactNode }) {
  const node = useContext(HeaderActionsPortalContext)
  const active = useOwnsHeader()
  if (!node || !active) return null
  return createPortal(children, node)
}

/** Renders children into the top bar's controls slot - a no-op until that
 * slot's DOM node exists (the first render or two), and empty whenever no
 * view has anything to put there. */
export function HeaderPortal({ children }: { children: ReactNode }) {
  const node = useContext(HeaderControlsPortalContext)
  // A hidden tab's controls must not sit in the header next to the active one's.
  const active = useOwnsHeader()
  if (!node || !active) return null
  return createPortal(children, node)
}

/** Renders children into the top bar's title slot (after the page title). */
export function TopBarTitlePortal({ children }: { children: ReactNode }) {
  const node = useContext(TopBarTitlePortalContext)
  const active = useOwnsHeader()
  if (!node || !active) return null
  return createPortal(children, node)
}

/** A detail page's way back ("← Albums"), shown at the left of the top bar
 * where the page title would be. The view keeps deciding what Back does. */
export function TopBarBack({ label, onBack, children }: { label: string; onBack: () => void; children?: ReactNode }) {
  return (
    <TopBarTitlePortal>
      <div className="browse-detail-header">
        <button type="button" className="browse-back" onClick={onBack}>
          <ArrowLeftIcon aria-hidden="true" />
          <span>{label}</span>
        </button>
        {children}
      </div>
    </TopBarTitlePortal>
  )
}
