// Runs as soon as the page's HTML is read, while the app's own code is still
// downloading (a plain script file: the server's Content-Security-Policy
// allows no inline scripts). Three jobs, all best-effort:
// 1. Mark the page when it has a window of its own (the Android app or an
//    installed PWA): only then does it draw under the system bars and pad
//    for their insets (html[data-own-window], styles/base.css). In a browser
//    tab it also drops viewport-fit=cover, as the browser keeps the bars.
// 2. Apply the saved theme, so the placeholder in #root already has the right
//    colours (main.tsx applies it again).
// 3. Start the sign-in check (/api/auth/me) and the first-run check
//    (/api/setup/status), so their answers are on their way by the time the
//    app starts. AuthContext picks the requests up once (window.__atrioBoot);
//    if the app got there first it has marked them taken and nothing is sent.
;(function () {
  try {
    var ownWindow =
      !!window.Capacitor ||
      window.navigator.standalone === true ||
      window.matchMedia('(display-mode: standalone), (display-mode: fullscreen)').matches
    if (ownWindow) document.documentElement.setAttribute('data-own-window', '')
    var dropCover = function () {
      var viewport = document.querySelector('meta[name="viewport"]')
      if (!viewport) return false
      viewport.setAttribute('content', viewport.getAttribute('content').replace(/,\s*viewport-fit=cover/, ''))
      return true
    }
    // This script is async and may run before the meta has been parsed.
    if (!ownWindow && !dropCover()) document.addEventListener('DOMContentLoaded', dropCover)
  } catch {
    // main.tsx sets the mark again; the meta stays as written.
  }
  try {
    var raw = localStorage.getItem('atriolyric.settings')
    var theme = raw ? JSON.parse(raw).theme : 'system'
    var light =
      theme === 'light' || (theme !== 'dark' && window.matchMedia('(prefers-color-scheme: light)').matches)
    if (light) document.documentElement.setAttribute('data-theme', 'light')
  } catch {
    // No storage, or a corrupt entry: main.tsx sorts the theme out.
  }
  try {
    if (window.__atrioBoot) return
    var ask = function (path) {
      return fetch(path, { credentials: 'same-origin', headers: { 'Content-Type': 'application/json' } })
    }
    var me = ask('/api/auth/me')
    var setup = ask('/api/setup/status')
    // Never an unhandled rejection: the app reads these and handles failures.
    me.catch(function () {})
    setup.catch(function () {})
    window.__atrioBoot = { me: me, setup: setup }
  } catch {
    // The app makes the requests itself.
  }
})()
