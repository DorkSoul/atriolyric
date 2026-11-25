import { useEffect, useState } from 'react'
import { DESKTOP_MEDIA_QUERY } from './settings'

/** True at/above the same width the rest of the layout switches from the
 * mobile tab picker to the always-visible desktop tab bar (see
 * DESKTOP_MEDIA_QUERY) - kept live via a matchMedia listener so resizing
 * or rotating a device updates it immediately, not just on next mount. */
export function useIsDesktop(): boolean {
  const [isDesktop, setIsDesktop] = useState(() => window.matchMedia(DESKTOP_MEDIA_QUERY).matches)

  useEffect(() => {
    const mq = window.matchMedia(DESKTOP_MEDIA_QUERY)
    function handle() {
      setIsDesktop(mq.matches)
    }
    mq.addEventListener('change', handle)
    return () => mq.removeEventListener('change', handle)
  }, [])

  return isDesktop
}
