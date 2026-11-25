// One IntersectionObserver per (scroll root, margin), shared by every element
// watched against it - a grid of a few hundred art tiles costs two observers,
// not two per tile. Each element gets its own entries, as with its own
// observer (including the first one, delivered when it starts being watched).

type Listener = (entry: IntersectionObserverEntry) => void

interface Shared {
  observer: IntersectionObserver
  listeners: Map<Element, Listener>
}

// Keyed by the root element (the viewport has a stand-in key), then margin.
const VIEWPORT = {}
const byRoot = new WeakMap<object, Map<string, Shared>>()

/** Calls `listener` with `el`'s intersection entries against `root` (null:
 * the viewport) grown by `rootMargin`, until the returned function is called. */
export function observeIntersection(
  el: Element,
  root: Element | null,
  rootMargin: string,
  listener: Listener,
): () => void {
  const rootKey = root ?? VIEWPORT
  let byMargin = byRoot.get(rootKey)
  if (!byMargin) {
    byMargin = new Map()
    byRoot.set(rootKey, byMargin)
  }
  let shared = byMargin.get(rootMargin)
  if (!shared) {
    const listeners = new Map<Element, Listener>()
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) listeners.get(entry.target)?.(entry)
      },
      { root, rootMargin },
    )
    shared = { observer, listeners }
    byMargin.set(rootMargin, shared)
  }
  const current = shared
  const margins = byMargin
  current.listeners.set(el, listener)
  current.observer.observe(el)
  return () => {
    if (current.listeners.get(el) !== listener) return
    current.listeners.delete(el)
    current.observer.unobserve(el)
    if (current.listeners.size === 0) {
      current.observer.disconnect()
      if (margins.get(rootMargin) === current) margins.delete(rootMargin)
    }
  }
}
