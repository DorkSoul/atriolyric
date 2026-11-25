import { useId, type ReactNode } from 'react'
import type { Tab } from '../nav'
import { ChevronLeftIcon, ChevronRightIcon, TabIcon } from './Icons'
import { useSettings } from '../SettingsContext'
import { useMediaQuery } from '../useMediaQuery'

/** An extra sidebar entry below the divider (above Admin), for screens that
 * are not tabs of the reorderable list - e.g. "Library info". */
export interface SidebarExtraItem {
  id: string
  label: string
  icon: ReactNode
  current: boolean
  onSelect: () => void
}

interface Props {
  /** Home, then the user's ordered visible tabs (no Admin). */
  tabs: Tab[]
  current: Tab
  labels: Record<Tab, string>
  onSelect: (tab: Tab) => void
  showAdmin: boolean
  /** Count shown at the end of a tab's row (hidden when the sidebar is icons
   * only). The button's name stays its label ("Songs"); the count is its
   * description, so a screen reader still reads it. */
  counts?: Partial<Record<Tab, ReactNode>>
  /** Entries after the divider, before Admin. */
  extraItems?: SidebarExtraItem[]
}

/** The desktop navigation (640px and wider): brand, Home, the user's tabs in
 * their order, a divider, then Admin. Under 1000px wide - or wider, when the
 * user collapses it with the toggle at its foot - it shows icons only; each
 * button keeps its text (visually hidden) as its name, plus a title. */
export function Sidebar({ tabs, current, labels, onSelect, showAdmin, counts, extraItems = [] }: Props) {
  const idPrefix = useId()
  const { settings, updateSettings } = useSettings()
  // One class drives the icon-only layout for both cases (app-shell.css),
  // so its rules are written once. Keep 1000px in step with that file.
  const roomy = useMediaQuery('(min-width: 1000px)')
  const collapsed = !roomy || settings.sidebarCollapsed
  function item(tab: Tab) {
    const count = counts?.[tab]
    const countId = `${idPrefix}-count-${tab}`
    return (
      <button
        key={tab}
        type="button"
        className={tab === current ? 'sidebar-item active' : 'sidebar-item'}
        aria-current={tab === current ? 'page' : undefined}
        // The count is hidden from the button's name (so the name stays the
        // label) and read as its description instead: a reference reaches
        // hidden text.
        aria-describedby={count != null ? countId : undefined}
        title={labels[tab]}
        onClick={() => onSelect(tab)}
      >
        <TabIcon tab={tab} className="sidebar-icon" />
        <span className="sidebar-label">{labels[tab]}</span>
        {count != null && (
          <span className="sidebar-count" id={countId} aria-hidden="true">
            {count}
          </span>
        )}
      </button>
    )
  }

  return (
    <div className={collapsed ? 'sidebar sidebar-collapsed' : 'sidebar'}>
      <div className="sidebar-brand">
        <img src="/favicon.svg" alt="" className="app-logo" />
        <span className="sidebar-brand-name">Atriolyric</span>
      </div>
      <nav className="tabs" aria-label="Tabs">
        {tabs.map(item)}
        {(extraItems.length > 0 || showAdmin) && <div className="sidebar-divider" aria-hidden="true" />}
        {extraItems.map((extra) => (
          <button
            key={extra.id}
            type="button"
            className={extra.current ? 'sidebar-item active' : 'sidebar-item'}
            aria-current={extra.current ? 'page' : undefined}
            title={extra.label}
            onClick={extra.onSelect}
          >
            <span className="sidebar-icon" aria-hidden="true">
              {extra.icon}
            </span>
            <span className="sidebar-label">{extra.label}</span>
          </button>
        ))}
        {showAdmin && item('admin')}
      </nav>
      {roomy && (
        <button
          type="button"
          className="sidebar-toggle"
          aria-expanded={!collapsed}
          aria-label={collapsed ? 'Expand navigation' : 'Collapse navigation'}
          title={collapsed ? 'Expand navigation' : 'Collapse navigation'}
          onClick={() => updateSettings({ sidebarCollapsed: !settings.sidebarCollapsed })}
        >
          {collapsed ? <ChevronRightIcon aria-hidden="true" /> : <ChevronLeftIcon aria-hidden="true" />}
        </button>
      )}
    </div>
  )
}
