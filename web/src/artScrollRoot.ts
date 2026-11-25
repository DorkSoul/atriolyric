import { createContext, useContext, type RefObject } from 'react'

// The scroll container most art tiles live in (App's .app-main). An
// IntersectionObserver's rootMargin only grows its root, and every
// scrolling ancestor between a tile and the root still clips it - so with
// the default (viewport) root, a tile scrolled just out of .app-main never
// counts as near, whatever the margin. Observing against this container
// instead makes the margin reach past its edges. See Art.tsx.
const ArtScrollRootContext = createContext<RefObject<HTMLElement | null> | null>(null)

export const ArtScrollRootProvider = ArtScrollRootContext

export function useArtScrollRoot(): RefObject<HTMLElement | null> | null {
  return useContext(ArtScrollRootContext)
}

/** The scroll container to observe `el` against, or null (the viewport)
 * when `el` isn't inside it - e.g. the player bar. */
export function scrollRootFor(
  rootRef: RefObject<HTMLElement | null> | null,
  el: Element,
): HTMLElement | null {
  const root = rootRef?.current ?? null
  return root && root !== el && root.contains(el) ? root : null
}
