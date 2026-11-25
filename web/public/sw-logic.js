// The service worker's decisions, kept apart from its event handlers so a
// browser test can load this file into an ordinary page and exercise them
// (service workers are blocked in the e2e config). A classic script: sw.js
// pulls it in with importScripts, a test page with a <script> tag.
;(function (root) {
  var CACHE_PREFIX = 'atriolyric-shell-'
  var PRECACHE = [
    '/',
    '/boot.js',
    '/favicon.svg',
    '/manifest.json',
    '/icons/icon-192.png',
    '/icons/icon-512.png',
    '/icons/icon-maskable-192.png',
    '/icons/icon-maskable-512.png',
    '/icons/apple-touch-icon.png',
  ]

  /** One cache per release: the version comes from the URL the page registered
   * the worker with (`/sw.js?v=1.2.3`), so a new release is a new worker with
   * a new cache, and activating it drops every older one - the old hashed
   * assets go with it. */
  function cacheNameFor(version) {
    return CACHE_PREFIX + (version || 'dev')
  }

  function versionFromScriptUrl(scriptUrl) {
    return new URL(scriptUrl).searchParams.get('v') || 'dev'
  }

  /** Caches to delete on activate: this release's is the only one kept. */
  function staleCaches(keys, current) {
    return keys.filter(function (key) {
      return key !== current
    })
  }

  /** What to do with a request:
   *  'ignore'   - not ours (writes, other origins, the API and the sockets)
   *  'asset'    - a content-hashed /assets/ file: cache first, it never changes
   *  'navigate' - a page load: network first, the cached shell when offline
   *  'other'    - the manifest, icons, version.json: network first, own copy offline */
  function classify(request, origin) {
    if (request.method !== 'GET') return 'ignore'
    var url = new URL(request.url)
    if (url.origin !== origin) return 'ignore'
    if (url.pathname.indexOf('/api/') === 0 || url.pathname.indexOf('/ws/') === 0) return 'ignore'
    if (url.pathname.indexOf('/assets/') === 0) return 'asset'
    if (request.mode === 'navigate') return 'navigate'
    return 'other'
  }

  /** Only a complete, successful answer is worth keeping: not an error page,
   * and not a 206 partial (response.ok is true for one) that would later be
   * served as if it were the whole file. */
  function isCacheable(response) {
    return response.ok === true && response.status === 200
  }

  /** The fallback when the network fails: the shell for a page load only.
   * A script, style or image never gets index.html. */
  function offlineFallback(kind) {
    return kind === 'navigate' ? '/' : null
  }

  root.atriolyricSw = {
    CACHE_PREFIX: CACHE_PREFIX,
    PRECACHE: PRECACHE,
    cacheNameFor: cacheNameFor,
    versionFromScriptUrl: versionFromScriptUrl,
    staleCaches: staleCaches,
    classify: classify,
    isCacheable: isCacheable,
    offlineFallback: offlineFallback,
  }
})(self)
