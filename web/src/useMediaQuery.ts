import { useEffect, useState } from 'react'

/** At/above this width the desktop side panel (SidePanel.tsx) takes the
 * player bar's place. Keep in step with the 1200px rules in app-shell.css,
 * side-panel.css and player-bar.css. */
export const WIDE_MEDIA_QUERY = '(min-width: 1200px)'

/** Whether `query` matches, kept live through a matchMedia listener. */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches)

  useEffect(() => {
    const mq = window.matchMedia(query)
    function handle() {
      setMatches(mq.matches)
    }
    handle()
    mq.addEventListener('change', handle)
    return () => mq.removeEventListener('change', handle)
  }, [query])

  return matches
}

/** True where the side panel is shown instead of the player bar. */
export function useIsWide(): boolean {
  return useMediaQuery(WIDE_MEDIA_QUERY)
}
