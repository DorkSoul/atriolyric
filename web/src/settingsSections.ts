// Kept apart from SettingsPanel so the gear menu and the section chips (App.tsx)
// can list the sections without loading the panel itself, which loads on demand.
// One entry per <section> of the Settings page (#/settings), in page order.
import { isNativeApp } from './native/bridge'

export const SETTINGS_SECTIONS = [
  { id: 'settings-account', label: 'Account' },
  { id: 'settings-playback', label: 'Playback' },
  // Only in the Android app: the PWA cannot keep media for offline use.
  ...(isNativeApp() ? [{ id: 'settings-downloads', label: 'Downloads' }] : []),
  { id: 'settings-appearance', label: 'Appearance' },
  { id: 'settings-navigation', label: 'Navigation' },
  { id: 'settings-discovery', label: 'Discovery' },
  { id: 'settings-scrobbling', label: 'Scrobbling' },
  { id: 'settings-about', label: 'About' },
]

/** The part of a section id used in the address: `#/settings/account`. */
export function settingsSectionSlug(id: string): string {
  return id.replace(/^settings-/, '')
}

/** A section id from an address part (`account` or `settings-account`);
 * undefined for anything that is not a section, so a stale link still opens
 * the page itself. */
export function settingsSectionFromSlug(slug: string | undefined): string | undefined {
  if (!slug) return undefined
  const id = slug.startsWith('settings-') ? slug : `settings-${slug}`
  return SETTINGS_SECTIONS.some((s) => s.id === id) ? id : undefined
}
