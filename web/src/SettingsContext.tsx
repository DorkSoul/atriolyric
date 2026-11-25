import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import {
  applyGridSize,
  applySidePanelWidth,
  applyListDensity,
  applyTheme,
  type AppSettings,
  loadSettings,
  saveSettings,
} from './settings'
import { useIsDesktop } from './useIsDesktop'

interface SettingsContextValue {
  settings: AppSettings
  updateSettings: (patch: Partial<AppSettings>) => void
}

const SettingsContext = createContext<SettingsContextValue | null>(null)

export function SettingsProvider({ children }: { children: ReactNode }) {
  const [settings, setSettings] = useState<AppSettings>(() => loadSettings())
  // Resizing across the breakpoint (or rotating a tablet) should swap tile
  // sizes live, the same way the tab bar itself switches layout.
  const isDesktop = useIsDesktop()

  useEffect(() => {
    applyTheme(settings.theme)
    // Accent color is deliberately NOT applied here - it lives on the
    // account (see PlayerContext), and re-applying the localStorage value
    // on every settings change would stomp the account's choice.
    applyGridSize(isDesktop ? settings.gridSizeDesktop : settings.gridSizeMobile)
    applyListDensity(settings.listDensity)
    applySidePanelWidth(settings.sidePanelWidth)
    saveSettings(settings)
  }, [settings, isDesktop])

  // "System" mode should react live if the OS/browser theme changes while
  // the app is open, not just on next load.
  useEffect(() => {
    if (settings.theme !== 'system') return
    const mq = window.matchMedia('(prefers-color-scheme: light)')
    function handle() {
      applyTheme('system')
    }
    mq.addEventListener('change', handle)
    return () => mq.removeEventListener('change', handle)
  }, [settings.theme])

  function updateSettings(patch: Partial<AppSettings>) {
    setSettings((prev) => ({ ...prev, ...patch }))
  }

  return (
    <SettingsContext value={{ settings, updateSettings }}>
      {children}
    </SettingsContext>
  )
}

export function useSettings(): SettingsContextValue {
  const ctx = useContext(SettingsContext)
  if (!ctx) throw new Error('useSettings must be used within SettingsProvider')
  return ctx
}
