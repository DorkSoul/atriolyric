import { useCallback, type ReactNode } from 'react'
import { useToast } from '../ToastContext'
import { ErrorBoundary } from './ErrorBoundary'

/** An error boundary for something drawn over the app: Now Playing, an info
 * dialog, the art picker, the player bar. Without one, a failure there (its
 * code not loading after a deploy replaced the file, or a rendering bug)
 * would reach the root boundary, which replaces the whole app, the player
 * and its audio elements included, so the music would stop. Here the overlay is
 * closed, the user is told why, and everything under it keeps running. */
export function OverlayBoundary({
  what,
  onClose,
  resetKey,
  children,
}: {
  /** Names the thing in the message: "Now Playing", "that dialog". */
  what: string
  /** Closes the overlay, so the next attempt starts from scratch. */
  onClose?: () => void
  resetKey?: string
  children: ReactNode
}) {
  const { showToast } = useToast()
  const onFailed = useCallback(
    (cause: 'load' | 'error') => {
      onClose?.()
      showToast(
        cause === 'load'
          ? `Couldn't load ${what}. Check your connection; if Atriolyric was just updated, restart the app.`
          : `Something went wrong showing ${what}.`,
        'error',
        'overlay-failed',
      )
    },
    [onClose, showToast, what],
  )
  return (
    <ErrorBoundary variant="overlay" onFailed={onFailed} resetKey={resetKey}>
      {children}
    </ErrorBoundary>
  )
}
