import { Profiler, type ProfilerOnRenderCallback, type ReactNode } from 'react'

declare global {
  interface Window {
    /** Development builds only: how many times each probed subtree has
     * committed a render. The end-to-end tests read it to check that a
     * position tick re-renders the player and nothing else. */
    __renderCommits?: Record<string, number>
  }
}

const countCommit: ProfilerOnRenderCallback = (id) => {
  const counts = (window.__renderCommits ??= {})
  counts[id] = (counts[id] ?? 0) + 1
}

/** Counts the commits of its subtree in development (the e2e suite runs the
 * dev server). In a production build `import.meta.env.DEV` is false, so this
 * renders its children as they are and the Profiler is never mounted. */
export function RenderProbe({ id, children }: { id: string; children: ReactNode }) {
  if (!import.meta.env.DEV) return children
  return (
    <Profiler id={id} onRender={countCommit}>
      {children}
    </Profiler>
  )
}
