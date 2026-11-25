import {
  useCallback,
  useEffect,
  useEffectEvent,
  useId,
  useRef,
  type HTMLAttributes,
  type ReactNode,
  type RefObject,
} from 'react'
import { createPortal } from 'react-dom'
import { pushTopLayerDialog } from '../topLayer'
import { type HistoryEntry, historyEntries, newHistoryId, openStack, selfPop } from '../modalHistory'
import { CloseIcon } from './Icons'
import { useViewActive } from '../viewActive'

let scrollLocks = 0
let previousBodyOverflow = ''

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

interface Props {
  /** Heading text, also the dialog's accessible name. */
  title: ReactNode
  onClose: () => void
  children: ReactNode
  /** Extra classes on the panel, e.g. a narrower or taller variant. */
  className?: string
  /** Replaces the default title + close row (the close button is still
   * required somewhere inside it for keyboard and touch users). */
  header?: (titleId: string) => ReactNode
  /** Below the title, still part of the header (e.g. a subtitle). */
  subtitle?: ReactNode
  /** Blocks every way of closing, e.g. while a save is in flight. */
  closeDisabled?: boolean
  closeLabel?: string
  /** What takes focus on open, when the panel itself is not the right
   * place (a confirmation's safe button, a prompt's input). */
  initialFocus?: RefObject<HTMLElement | null>
  /** 'sheet' is a full-bleed overlay instead of a centred panel (Now Playing):
   * no header of its own - `children` carry the whole screen, `title` is only
   * the dialog's accessible name - and the page behind is made inert. It keeps
   * everything else: focus moved in and trapped, Escape, the stack, the scroll
   * lock, focus restored to the opener, a press on the backdrop to close. */
  variant?: 'dialog' | 'sheet'
  /** Sheet only: classes of the full-bleed backdrop element, and handlers for
   * it (swipe gestures, pointer tracking). `className` styles the panel. */
  backdropClassName?: string
  backdropProps?: Omit<HTMLAttributes<HTMLDivElement>, 'className' | 'role' | 'onMouseDown'>
  /** Sheet only: the panel element, for callers that measure or scroll it. */
  panelRef?: RefObject<HTMLDivElement | null>
  /** Sheet only: regions of the page to make inert while open. */
  inertSelector?: string
  /** Gives the modal its own history entry, so the phone's Back button closes
   * it instead of navigating the page underneath. Stack-aware like Escape: only
   * the top modal reacts. Not for a modal that already handles Back itself
   * (Now Playing, Settings). */
  history?: boolean
}

/** The app's one dialog: a panel over the blurred page, closed by Escape,
 * the close button or a click on the backdrop. It traps focus, locks the page's
 * scroll and gives focus back to whatever opened it. */
export function Modal({
  title,
  onClose,
  children,
  className,
  header,
  subtitle,
  closeDisabled = false,
  closeLabel = 'Close',
  initialFocus,
  variant = 'dialog',
  backdropClassName,
  backdropProps,
  panelRef: externalPanelRef,
  inertSelector,
  history: ownsHistoryEntry = false,
}: Props) {
  const titleId = useId()
  // Portaled to <body>, so a hidden tab's <Activity> cannot hide it: a modal
  // owned by a tab that is not showing hides itself (its state is kept).
  const viewActive = useViewActive()
  const hiddenStyle = viewActive ? undefined : ({ display: 'none' } as const)
  const panelRef = useRef<HTMLDivElement | HTMLDialogElement | null>(null)
  const setPanel = useCallback(
    (element: HTMLDivElement | HTMLDialogElement | null) => {
      panelRef.current = element
      if (externalPanelRef && !(element instanceof HTMLDialogElement)) externalPanelRef.current = element
    },
    [externalPanelRef],
  )
  const historyRef = useRef<HistoryEntry | null>(null)
  const popTimerRef = useRef<number | null>(null)
  // The latest props, read from the mount effect's listeners and the Back
  // handler without re-running that effect.
  const close = useEffectEvent(() => onClose())
  const isCloseDisabled = useEffectEvent(() => closeDisabled)
  // A close request that did not come from the Escape key handled below: on
  // Android, the Back button. A modal with its own history entry closes the
  // way Back closes it (through that entry); any other just closes.
  const closeFromRequest = useEffectEvent(() => {
    if (isCloseDisabled()) return
    if (ownsHistoryEntry && historyRef.current && !historyRef.current.popped) window.history.back()
    else close()
  })

  useEffect(() => {
    const token = Symbol('modal')
    openStack.push(token)
    // Read in this effect, after a menu that opened the modal has unmounted and
    // handed focus back to its trigger, and before anything is focused here.
    const opener = document.activeElement as HTMLElement | null
    let historyEntry: HistoryEntry | undefined
    if (ownsHistoryEntry) {
      if (popTimerRef.current != null) {
        // React's development double-mount (unmount and mount again at once):
        // keep the entry already pushed instead of pushing a second.
        window.clearTimeout(popTimerRef.current)
        popTimerRef.current = null
        historyEntry = historyRef.current ?? undefined
      }
      if (!historyEntry) {
        const id = newHistoryId()
        window.history.pushState({ modalEntry: id }, '', window.location.href)
        const created: HistoryEntry = {
          id,
          popped: false,
          pop: () => {
            if (isCloseDisabled()) {
              // Closing is blocked (a save is running): put the entry back.
              window.history.pushState({ modalEntry: id }, '', window.location.href)
              return false
            }
            // Back already took the entry; closing must not take another.
            created.popped = true
            close()
            return true
          },
        }
        historyEntry = created
        historyRef.current = created
      }
      historyEntries.set(token, historyEntry)
    }
    if (scrollLocks++ === 0) {
      previousBodyOverflow = document.body.style.overflow
      document.body.style.overflow = 'hidden'
    }
    // Inert after the opener is read: making the opener's region inert blurs it.
    const inerted = inertSelector
      ? [...document.querySelectorAll<HTMLElement>(inertSelector)].filter((element) => !element.inert)
      : []
    for (const element of inerted) element.inert = true
    const panel = panelRef.current
    // The dialog variant is a native modal <dialog>: the browser puts it in
    // the top layer, above everything, and makes the rest of the page inert.
    // A close request (Escape is handled below; this is Android's Back) is
    // turned into the modal's own close, so React stays in charge of whether
    // it is open. If the browser closed it anyway (it may skip the
    // cancellable step without a user gesture in between), it is reopened
    // while closing is blocked, or closed the same way otherwise.
    const dialog = panel instanceof HTMLDialogElement ? panel : null
    let popTopLayer = () => {}
    const onCancel = (event: Event) => {
      event.preventDefault()
      if (openStack[openStack.length - 1] === token) closeFromRequest()
    }
    const onNativeClose = () => {
      if (!dialog || dialog.open) return
      if (isCloseDisabled()) dialog.showModal()
      else closeFromRequest()
    }
    // Read before showModal(), which focuses the dialog's first control itself.
    const focusWasInside = Boolean(panel?.contains(document.activeElement))
    if (dialog) {
      if (!dialog.open) dialog.showModal()
      popTopLayer = pushTopLayerDialog(dialog)
      dialog.addEventListener('cancel', onCancel)
      dialog.addEventListener('close', onNativeClose)
    }
    if (panel && !focusWasInside) {
      const first = initialFocus?.current ?? panel.querySelector<HTMLElement>('[autofocus]') ?? panel
      first.focus({ preventScroll: true })
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (openStack[openStack.length - 1] !== token) return
      if (event.key === 'Escape') {
        event.stopPropagation()
        // Also keeps the browser from closing the <dialog> itself.
        event.preventDefault()
        if (!isCloseDisabled()) close()
        return
      }
      if (event.key !== 'Tab' || !panelRef.current) return
      const focusable = [...panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
        (element) => element.offsetParent !== null,
      )
      if (focusable.length === 0) {
        event.preventDefault()
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const active = document.activeElement
      if (event.shiftKey && (active === first || active === panelRef.current)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && active === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      if (dialog) {
        dialog.removeEventListener('cancel', onCancel)
        dialog.removeEventListener('close', onNativeClose)
        popTopLayer()
        if (dialog.open) dialog.close()
      }
      openStack.splice(openStack.indexOf(token), 1)
      if (historyEntry) {
        const entry = historyEntry
        historyEntries.delete(token)
        // Closed by Escape, a button, a save: drop the entry it pushed - but
        // only if it is still the current one (after a link inside the modal
        // navigated, going back would undo that navigation). Deferred a tick so
        // a development re-mount can keep the entry instead.
        popTimerRef.current = window.setTimeout(() => {
          popTimerRef.current = null
          historyRef.current = null
          if (!entry.popped && window.history.state?.modalEntry === entry.id) {
            selfPop()
          }
        }, 0)
      }
      if (--scrollLocks === 0) document.body.style.overflow = previousBodyOverflow
      // Before focusing the opener, which may sit inside what was inert.
      for (const element of inerted) element.inert = false
      if (opener?.isConnected) opener.focus({ preventScroll: true })
    }
    // Mount-only on purpose: the stack position, scroll lock and opener belong
    // to the modal's whole lifetime, and `initialFocus`/`inertSelector` are
    // only read on open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (variant === 'sheet') {
    return createPortal(
      // A press on the backdrop closes it (mousedown, as for the dialog);
      // keyboard users have Escape and the close button inside the panel.
      // oxlint-disable-next-line jsx-a11y/no-static-element-interactions
      <div
        {...backdropProps}
        style={hiddenStyle ?? backdropProps?.style}
        className={backdropClassName}
        onMouseDown={(event) => {
          if (event.target === event.currentTarget && !closeDisabled) onClose()
        }}
      >
        <div
          ref={setPanel}
          className={className}
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          tabIndex={-1}
        >
          <h2 id={titleId} className="visually-hidden">
            {title}
          </h2>
          {children}
        </div>
      </div>,
      document.body,
    )
  }

  return createPortal(
    // A press on the backdrop (outside the panel: the <dialog>'s ::backdrop
    // reports the dialog itself as the target) closes it; keyboard users have
    // Escape and the close button. mousedown, not click: a text selection
    // dragged out of the panel must not close it.
    // oxlint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <dialog
      ref={setPanel}
      className={className ? `modal-panel ${className}` : 'modal-panel'}
      style={hiddenStyle}
      aria-modal="true"
      aria-labelledby={titleId}
      tabIndex={-1}
      onMouseDown={(event) => {
        if (event.target !== event.currentTarget || closeDisabled) return
        const box = event.currentTarget.getBoundingClientRect()
        const inside = event.clientX >= box.left && event.clientX <= box.right && event.clientY >= box.top && event.clientY <= box.bottom
        if (!inside) onClose()
      }}
    >
      {header ? (
        header(titleId)
      ) : (
        <div className="modal-header">
          <div className="modal-heading">
            <h2 className="modal-title" id={titleId}>
              {title}
            </h2>
            {subtitle && <div className="modal-subtitle">{subtitle}</div>}
          </div>
          <button
            type="button"
            className="modal-close"
            onClick={onClose}
            disabled={closeDisabled}
            aria-label={closeLabel}
          >
            <CloseIcon />
          </button>
        </div>
      )}
      {children}
    </dialog>,
    document.body,
  )
}
