import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { CheckIcon, CloseIcon, WarningIcon } from './components/Icons'
import { useTopLayerDialog } from './topLayer'

// No default kind on purpose - a toast's color is meaningful (green only
// ever means "that succeeded"), so every call site must say which one it
// means rather than silently inheriting a fallback that may not match.
export type ToastKind = 'success' | 'error' | 'warning'

interface Toast {
  id: number
  message: string
  kind: ToastKind
  key?: string
  onClick?: () => void
}

interface ToastContextValue {
  /** A `key` makes the toast replace any visible toast with the same key
   * (progress messages) instead of stacking another. */
  showToast: (message: string, kind: ToastKind, key?: string) => void
  /** A success toast that jumps to the playlist when clicked. */
  showPlaylistToast: (message: string, playlistId: number) => void
  /** Drops error toasts for a while: when the session expires, every request
   * already in flight fails too, and each view would report its own. */
  muteErrorToasts: (durationMs: number) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)
const PlaylistOpenerContext = createContext<React.MutableRefObject<((playlistId: number) => void) | null> | null>(null)

const TOAST_DURATION_MS = 6000
// A toast you can act on (it opens the playlist) gets time to be read and used.
const ACTION_TOAST_DURATION_MS = 12000

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const nextId = useRef(0)
  const timers = useRef(new Map<number, number>())
  // When each toast's clock last started and how much it has left, so hovering
  // or focusing one can stop it and let go of it where it was.
  const clocks = useRef(new Map<number, { startedAt: number; remaining: number }>())
  const playlistOpener = useRef<((playlistId: number) => void) | null>(null)
  const errorsMutedUntil = useRef(0)

  const dismiss = useCallback((id: number) => {
    window.clearTimeout(timers.current.get(id))
    timers.current.delete(id)
    clocks.current.delete(id)
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const startClock = useCallback(
    (id: number, remaining: number) => {
      clocks.current.set(id, { startedAt: Date.now(), remaining })
      timers.current.set(id, window.setTimeout(() => dismiss(id), remaining))
    },
    [dismiss],
  )

  const pause = useCallback((id: number) => {
    const clock = clocks.current.get(id)
    if (!clock || !timers.current.has(id)) return
    window.clearTimeout(timers.current.get(id))
    timers.current.delete(id)
    clock.remaining = Math.max(1000, clock.remaining - (Date.now() - clock.startedAt))
  }, [])

  const resume = useCallback(
    (id: number) => {
      const clock = clocks.current.get(id)
      if (clock && !timers.current.has(id)) startClock(id, clock.remaining)
    },
    [startClock],
  )

  const showToast = useCallback(
    (message: string, kind: ToastKind, key?: string, onClick?: () => void) => {
      if (kind === 'error' && Date.now() < errorsMutedUntil.current) return
      const id = nextId.current++
      setToasts((prev) => {
        const replaced = key ? prev.filter((t) => t.key === key) : []
        for (const old of replaced) {
          window.clearTimeout(timers.current.get(old.id))
          timers.current.delete(old.id)
          clocks.current.delete(old.id)
        }
        return [...prev.filter((t) => !key || t.key !== key), { id, message, kind, key, onClick }]
      })
      startClock(id, onClick ? ACTION_TOAST_DURATION_MS : TOAST_DURATION_MS)
    },
    [startClock],
  )

  const showPlaylistToast = useCallback(
    (message: string, playlistId: number) => {
      showToast(message, 'success', undefined, () => playlistOpener.current?.(playlistId))
    },
    [showToast],
  )

  const muteErrorToasts = useCallback((durationMs: number) => {
    errorsMutedUntil.current = Date.now() + durationMs
  }, [])

  // One value for the provider's life: a toast appearing re-renders the stack,
  // not every component that merely uses toasts.
  const value = useMemo(
    () => ({ showToast, showPlaylistToast, muteErrorToasts }),
    [showToast, showPlaylistToast, muteErrorToasts],
  )

  const topDialog = useTopLayerDialog()
  const toastStack = (
    <div className="toast-stack" aria-live="polite">
      {toasts.map((t) => (
        // The clock stops while a pointer is over the toast or focus is in it
        // (reading it, reaching for its button), and carries on after.
        // oxlint-disable-next-line jsx-a11y/no-static-element-interactions
        <div
          key={t.id}
          className={`toast toast-${t.kind}`}
          onMouseEnter={() => pause(t.id)}
          onMouseLeave={() => resume(t.id)}
          onFocus={() => pause(t.id)}
          onBlur={() => resume(t.id)}
        >
          {/* A real element, not CSS content, so it is sized and coloured like the other icons. */}
          <span className="toast-icon" aria-hidden="true">
            {t.kind === 'success' ? <CheckIcon /> : <WarningIcon />}
          </span>
          {t.onClick ? (
            <button
              className="toast-link"
              onClick={() => {
                t.onClick?.()
                dismiss(t.id)
              }}
            >
              {t.message}
            </button>
          ) : (
            <span>{t.message}</span>
          )}
          <button onClick={() => dismiss(t.id)} aria-label="Dismiss">
            <CloseIcon />
          </button>
        </div>
      ))}
    </div>
  )

  return (
    <ToastContext value={value}>
      <PlaylistOpenerContext value={playlistOpener}>
      {children}
      </PlaylistOpenerContext>
      {/* Inside the innermost open modal <dialog> while there is one: the
          rest of the page is inert and below the top layer then, and a toast
          has to stay visible and clickable. */}
      {topDialog ? createPortal(toastStack, topDialog) : toastStack}
    </ToastContext>
  )
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used within ToastProvider')
  return ctx
}

/** Lets the app say how a playlist toast opens its playlist. */
export function useRegisterPlaylistOpener(open: (playlistId: number) => void) {
  const ref = useContext(PlaylistOpenerContext)
  useEffect(() => {
    if (!ref) return
    ref.current = open
    return () => {
      ref.current = null
    }
  }, [ref, open])
}
