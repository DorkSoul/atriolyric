import type { Tab } from '../nav'
import { MoreHorizontalIcon, TabIcon } from './Icons'

/** How many of the user's tabs (after Home) sit on the phone's bottom bar;
 * the rest are under More. */
export const PHONE_BAR_TABS = 3

interface Props {
  /** Home, then the first PHONE_BAR_TABS of the user's visible tabs. */
  tabs: Tab[]
  current: Tab
  labels: Record<Tab, string>
  onSelect: (tab: Tab) => void
  /** True while the open tab is one of those under More. */
  currentInMore: boolean
  moreOpen: boolean
  onOpenMore: () => void
}

/** The phone navigation (under 640px): a bottom bar below the player with
 * Home, the user's first three tabs and More. */
export function PhoneTabBar({ tabs, current, labels, onSelect, currentInMore, moreOpen, onOpenMore }: Props) {
  return (
    <nav className="bottom-tabs" aria-label="Quick tab navigation">
      {tabs.map((t) => (
        <button
          key={t}
          type="button"
          className={t === current ? 'bottom-tab active' : 'bottom-tab'}
          aria-current={t === current ? 'page' : undefined}
          onClick={() => onSelect(t)}
        >
          <TabIcon tab={t} className="bottom-tab-icon" />
          <span className="bottom-tab-label">{labels[t]}</span>
        </button>
      ))}
      <button
        type="button"
        className={currentInMore ? 'bottom-tab active' : 'bottom-tab'}
        aria-haspopup="dialog"
        aria-expanded={moreOpen}
        onClick={onOpenMore}
      >
        <MoreHorizontalIcon className="bottom-tab-icon" />
        <span className="bottom-tab-label">More</span>
      </button>
    </nav>
  )
}
