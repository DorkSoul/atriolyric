import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { ErrorBoundary } from './components/ErrorBoundary'
import { installClientErrorReporting, reportRenderError } from './clientErrorReporting'
import { installClientDebugLogging } from './clientDebugLog'
import { applyGridSize, applyListDensity, applyTheme, DESKTOP_MEDIA_QUERY, loadSettings } from './settings'
import { isNativeApp, nativeCall } from './native/bridge'

installClientErrorReporting()
installClientDebugLogging()

// After a touch tap a button keeps focus (and sticky :active/:focus styling on
// some mobile browsers). Drop focus on touch release; keyboard use is untouched.
document.addEventListener(
  'pointerup',
  (event) => {
    if (event.pointerType !== 'touch') return
    const target = event.target as Element | null
    const button = target?.closest?.('button')
    if (button instanceof HTMLElement && document.activeElement === button) {
      button.blur()
    }
  },
  true,
)

// The page draws under the system bars only in a window of its own (the
// Android app or an installed PWA); see --safe-top/--safe-bottom in
// styles/base.css. boot.js sets this too, earlier.
if (
  isNativeApp() ||
  (navigator as Navigator & { standalone?: boolean }).standalone === true ||
  window.matchMedia('(display-mode: standalone), (display-mode: fullscreen)').matches
) {
  document.documentElement.setAttribute('data-own-window', '')
}

if (isNativeApp()) {
  // The Android app keeps its own copy of the page (WebCache.kt in mobile/),
  // which is what opens with no connection: a service worker would answer
  // the same requests from a second, partial copy. Once the page is up, the
  // app is told which built files exist, so it also fetches the views that
  // have not been opened yet. (The list only exists in a build.)
  if ('serviceWorker' in navigator) {
    void navigator.serviceWorker.getRegistrations().then((all) => all.forEach((one) => void one.unregister())).catch(() => {})
  }
  window.addEventListener('load', () => {
    window.setTimeout(() => {
      fetch('/asset-list.json')
        .then((answer) => (answer.ok ? (answer.json() as Promise<string[]>) : []))
        .then((paths) => (paths.length > 0 ? nativeCall('keepPages', { paths }) : undefined))
        .catch(() => {})
    }, 5_000)
  })
} else if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register(`/sw.js?v=${encodeURIComponent(__APP_VERSION__)}`, { updateViaCache: 'none' }).then((registration) => {
      registration.update().catch(() => {})
    }).catch(() => {})
  })
}

// Applied synchronously before React ever paints, so the first frame
// already matches the saved theme/grid size instead of flashing the
// defaults and then swapping. The accent color is not applied here - it
// lives on the account and arrives with /api/auth/me (PlayerContext); the
// CSS default shows until then, so the login screen is neutral rather
// than flashing this device's stale choice.
const initialSettings = loadSettings()
applyTheme(initialSettings.theme)
applyGridSize(
  window.matchMedia(DESKTOP_MEDIA_QUERY).matches
    ? initialSettings.gridSizeDesktop
    : initialSettings.gridSizeMobile,
)
applyListDensity(initialSettings.listDensity)

// React reports render errors here instead of to the console: a caught one
// (an error boundary showed its fallback) and an uncaught one both go to the
// server log through the same capped reporter as window errors.
createRoot(document.getElementById('root')!, {
  onCaughtError: (error, info) => {
    reportRenderError(error, info.componentStack)
    console.warn('A view failed to render and an error boundary caught it:', error)
  },
  onUncaughtError: (error, info) => {
    reportRenderError(error, info.componentStack)
    console.error('Uncaught render error:', error)
  },
}).render(
  <StrictMode>
    <ErrorBoundary variant="root">
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
