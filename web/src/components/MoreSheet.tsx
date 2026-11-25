import type { Tab } from '../nav'
import { TabIcon } from './Icons'
import { closeModalThen } from '../modalHistory'
import { Modal } from './Modal'

interface Props {
  /** The user's visible tabs that are not on the bottom bar (no Admin). */
  tabs: Tab[]
  current: Tab
  labels: Record<Tab, string>
  showAdmin: boolean
  onSelect: (tab: Tab) => void
  onClose: () => void
}

/** The phone's More sheet: every tab not on the bottom bar, then Settings and
 * Admin. A Modal with its own history entry, so focus is trapped and Escape
 * or the phone's Back button close it like any other overlay. */
export function MoreSheet({ tabs, current, labels, showAdmin, onSelect, onClose }: Props) {
  // Picking an entry first drops the sheet's own history entry (Back to the
  // page it was opened on), then navigates - so the history holds the new
  // page right after the old one, with no leftover entry for the sheet.
  function choose(tab: Tab) {
    closeModalThen(onClose, () => onSelect(tab))
  }

  function item(tab: Tab) {
    return (
      <button
        key={tab}
        type="button"
        className={tab === current ? 'more-sheet-item active' : 'more-sheet-item'}
        aria-current={tab === current ? 'page' : undefined}
        onClick={() => choose(tab)}
      >
        <TabIcon tab={tab} className="more-sheet-icon" />
        <span>{labels[tab]}</span>
      </button>
    )
  }

  return (
    <Modal title="More" onClose={onClose} className="more-sheet" history>
      <div className="more-sheet-grid">
        {tabs.map(item)}
        {item('library-info')}
        {/* No Settings here: the header's gear opens it on every screen. */}
        {showAdmin && item('admin')}
      </div>
      <p className="more-sheet-hint">
        The first three tabs in Settings › Navigation sit on the bar; the rest are here.
      </p>
    </Modal>
  )
}
