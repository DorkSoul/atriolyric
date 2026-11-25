/* global atriolyricSw */
importScripts('/sw-logic.js')

// The cache is named for the release (see sw-logic.js): main.tsx registers
// this file as /sw.js?v=<version>.
const CACHE_NAME = atriolyricSw.cacheNameFor(atriolyricSw.versionFromScriptUrl(self.location.href))

self.addEventListener('install', (event) => {
  self.skipWaiting()
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(atriolyricSw.PRECACHE.map((path) => new Request(path, { cache: 'reload' })))),
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim())
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(atriolyricSw.staleCaches(keys, CACHE_NAME).map((key) => caches.delete(key)))),
  )
})

function remember(request, response) {
  if (atriolyricSw.isCacheable(response)) {
    const copy = response.clone()
    caches.open(CACHE_NAME).then((cache) => cache.put(request, copy)).catch(() => {})
  }
  return response
}

self.addEventListener('fetch', (event) => {
  const kind = atriolyricSw.classify(event.request, self.location.origin)
  if (kind === 'ignore') return

  if (kind === 'asset') {
    // Content-hashed, so a cached copy is always right: no network at all.
    event.respondWith(
      caches.match(event.request).then((cached) => cached || fetch(event.request).then((response) => remember(event.request, response))),
    )
    return
  }

  // The page shell and the small files around it are revalidated with the
  // server, so the browser's HTTP cache can never hand back an index.html that
  // points at an older build; offline, the copy from the last visit.
  const fallback = atriolyricSw.offlineFallback(kind)
  event.respondWith(
    fetch(event.request, { cache: 'no-cache' })
      .then((response) => remember(event.request, response))
      .catch(() =>
        caches.match(event.request).then((cached) => cached || (fallback ? caches.match(fallback) : undefined) || Response.error()),
      ),
  )
})
