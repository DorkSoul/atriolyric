import { useEffect } from 'react'

// Like RenderProbe (which counts every commit under it, so cannot tell a
// component from its children), for one component: how many times it has
// rendered and committed, in `window.__renderCommits[id]`. Development builds
// only; the end-to-end tests read it to check that a position tick re-renders
// Now Playing's seek row and neither the rest of the view nor the queue.

function useCountedRenders(id: string): void {
  useEffect(() => {
    const counts = (window.__renderCommits ??= {})
    counts[id] = (counts[id] ?? 0) + 1
  })
}

function useNoProbe(): void {}

/** Counts the caller's renders in development; nothing in a production build
 * (the choice is fixed per build, so the hook order never changes). */
export const useRenderProbe: (id: string) => void = import.meta.env.DEV ? useCountedRenders : useNoProbe
