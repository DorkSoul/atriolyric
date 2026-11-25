import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  type ButtonHTMLAttributes,
  type CSSProperties,
  type ReactNode,
  type RefObject,
} from 'react'
import type { MenuAnchor } from '../menuTrigger'
import { useMenuPlacement } from '../useMenuPlacement'
import { useOutsideClick, type OutsideClickInside } from '../useOutsideClick'

const ITEM_SELECTOR =
  '[role="menuitem"]:not([aria-disabled="true"]), [role="menuitemradio"]:not([aria-disabled="true"]), [role="menuitemcheckbox"]:not([aria-disabled="true"])'
const TYPEAHEAD_RESET_MS = 700
// A stable stand-in so an unanchored menu does not re-run the placement effect.
const NO_ANCHOR = { x: 0, y: 0 }

interface MenuProps {
  /** The menu's accessible name (usually "Actions for <thing>"). */
  label: string
  /** Closes the menu: Escape, Tab, a press outside it. Item handlers call it
   * themselves after acting. */
  onClose: () => void
  /** For a sub-panel: Escape and Left Arrow step back to the parent panel
   * instead of closing everything. */
  onBack?: () => void
  /** Open at a point (a fixed panel nudged back onscreen) rather than where
   * the stylesheet puts a dropdown. */
  anchor?: MenuAnchor
  /** The button that opened a dropdown. Focus returns to it on close, and a
   * press on it is not "outside". Anchored menus carry it in `anchor.trigger`. */
  triggerRef?: RefObject<HTMLElement | null>
  /** More areas a press may land in without closing the menu (the Now
   * Playing art the menu floats over). */
  alsoInside?: OutsideClickInside[]
  id?: string
  className?: string
  /** Changes when the content is swapped for another panel's: focus moves to
   * that panel's first (or default) item and the placement is measured again. */
  view?: string | number
  /** Takes focus when the panel opens instead of an item (e.g. a search
   * field at the top of a long list). */
  initialFocusRef?: RefObject<HTMLElement | null>
  children: ReactNode
}

/** The one popup menu: role="menu" with roving focus. Opening moves focus to
 * the checked item, else the first; Up/Down/Home/End move it, a letter jumps
 * to the next item starting with it, Escape closes and returns focus to the
 * trigger, Tab closes and lets focus go on from the trigger, and a press
 * outside closes. Clicks and keys inside never reach an ancestor, so a menu
 * rendered inside a clickable card cannot also activate the card. */
export function Menu({ label, onClose, onBack, anchor, triggerRef, alsoInside, id, className, view, initialFocusRef, children }: MenuProps) {
  const { ref: panelRef, style: placement } = useMenuPlacement(anchor ?? NO_ANCHOR, [view])
  const typeahead = useRef({ text: '', at: 0 })
  const closeRef = useRef(onClose)
  const backRef = useRef(onBack)
  useEffect(() => {
    closeRef.current = onClose
    backRef.current = onBack
  })
  // Only an explicit trigger counts as inside: a press on it toggles the menu.
  const triggerElRef = useRef<HTMLElement | null>(null)

  // A press outside only closes the menu - it never also clicks what is under it.
  useOutsideClick([panelRef, triggerElRef, ...(alsoInside ?? [])], () => closeRef.current(), true, false, true)

  // Remember what opened the menu before focus moves into it, and give focus
  // back when it goes away. The cleanup is a layout effect so it runs while
  // the panel is still in the DOM (focus is still inside it); by the time a
  // passive cleanup ran, focus would already have dropped to <body>. An item
  // that opens a dialog therefore hands that dialog the trigger as its opener.
  useLayoutEffect(() => {
    const panel = panelRef.current
    const trigger = triggerRef?.current ?? anchor?.trigger ?? null
    const opener = trigger ?? (document.activeElement as HTMLElement | null)
    triggerElRef.current = trigger
    return () => {
      const active = document.activeElement
      const focusWasInside = !active || active === document.body || panel?.contains(active)
      if (focusWasInside && opener && opener !== document.body && opener.isConnected) {
        opener.focus({ preventScroll: true })
      }
    }
    // Mount-only: the opener is whoever had focus when the menu appeared.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const items = useCallback(
    () => [...(panelRef.current?.querySelectorAll<HTMLElement>(ITEM_SELECTOR) ?? [])],
    [panelRef],
  )

  const focusItem = useCallback(
    (index: number) => {
      const list = items()
      if (list.length === 0) return
      const target = list[(index + list.length) % list.length]
      for (const item of list) item.tabIndex = item === target ? 0 : -1
      target.focus({ preventScroll: true })
    },
    [items],
  )

  // The placement hides a fixed panel until it is measured, and a hidden
  // element cannot take focus: wait for it to be visible.
  const ready = !anchor || placement.visibility === 'visible'
  useEffect(() => {
    if (!ready) return
    if (initialFocusRef?.current) {
      initialFocusRef.current.focus({ preventScroll: true })
      return
    }
    const list = items()
    const checked = list.findIndex((item) => item.getAttribute('aria-checked') === 'true')
    const preferred = list.findIndex((item) => item.hasAttribute('data-menu-default'))
    if (list.length > 0) focusItem(checked >= 0 ? checked : Math.max(0, preferred))
    else panelRef.current?.focus({ preventScroll: true })
  }, [ready, view, items, focusItem, panelRef, initialFocusRef])

  function currentIndex() {
    return items().indexOf(document.activeElement as HTMLElement)
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    // Tab leaves the menu: close it, and the browser then moves on from the
    // trigger that focus returns to.
    if (event.key === 'Tab') {
      closeRef.current()
      return
    }
    event.stopPropagation()
    const count = items().length
    const at = currentIndex()
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault()
        focusItem(at < 0 ? 0 : at + 1)
        return
      case 'ArrowUp':
        event.preventDefault()
        focusItem(at < 0 ? count - 1 : at - 1)
        return
      case 'Home':
        event.preventDefault()
        focusItem(0)
        return
      case 'End':
        event.preventDefault()
        focusItem(count - 1)
        return
      case 'ArrowRight': {
        const item = items()[at]
        if (item?.getAttribute('aria-haspopup') === 'menu') {
          event.preventDefault()
          item.click()
        }
        return
      }
      case 'ArrowLeft':
        if (backRef.current) {
          event.preventDefault()
          backRef.current()
        }
        return
      case 'Escape':
        event.preventDefault()
        if (backRef.current) backRef.current()
        else closeRef.current()
        return
    }
    // Type-ahead: letters build a prefix (reset after a pause) and jump to the
    // next item whose text starts with it.
    if (event.key.length === 1 && event.key !== ' ' && !event.ctrlKey && !event.metaKey && !event.altKey) {
      const now = Date.now()
      const state = typeahead.current
      state.text = now - state.at > TYPEAHEAD_RESET_MS ? event.key : state.text + event.key
      state.at = now
      const typed = state.text.toLowerCase()
      const list = items()
      // The same letter pressed again cycles through the items sharing it;
      // a longer prefix narrows down from the current item.
      const repeated = [...typed].every((c) => c === typed[0])
      const prefix = repeated ? typed[0] : typed
      const from = repeated ? at + 1 : Math.max(at, 0)
      for (let step = 0; step < list.length; step++) {
        const index = (from + step + list.length) % list.length
        if ((list[index].textContent ?? '').trim().toLowerCase().startsWith(prefix)) {
          focusItem(index)
          return
        }
      }
    }
  }

  const stop = (event: React.SyntheticEvent) => event.stopPropagation()
  const style: CSSProperties | undefined = anchor ? placement : undefined

  return (
    <div
      ref={panelRef}
      id={id}
      role="menu"
      aria-label={label}
      className={className}
      style={style}
      tabIndex={-1}
      onKeyDown={onKeyDown}
      onClick={stop}
      onPointerDown={stop}
      onPointerUp={stop}
      onContextMenu={stop}
    >
      {children}
    </div>
  )
}

interface MenuItemProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'role' | 'onSelect'> {
  /** A choice among several (a sort order, a device): `checked` says which. */
  choice?: 'radio' | 'checkbox'
  checked?: boolean
  /** Opens a sub-panel: announced as a popup, and Right Arrow activates it. */
  hasSubmenu?: boolean
  /** Takes focus when the panel opens, instead of the first item. */
  defaultFocus?: boolean
  onSelect?: () => void
}

export function MenuItem({
  choice,
  checked,
  hasSubmenu,
  defaultFocus,
  onSelect,
  onClick,
  children,
  ...rest
}: MenuItemProps) {
  const role = choice === 'radio' ? 'menuitemradio' : choice === 'checkbox' ? 'menuitemcheckbox' : 'menuitem'
  return (
    // The name is the item's children.
    // oxlint-disable-next-line jsx-a11y/control-has-associated-label
    <button
      type="button"
      {...rest}
      role={role}
      tabIndex={-1}
      aria-checked={choice ? Boolean(checked) : undefined}
      aria-haspopup={hasSubmenu ? 'menu' : undefined}
      data-menu-default={defaultFocus ? '' : undefined}
      onClick={(event) => {
        onClick?.(event)
        onSelect?.()
      }}
    >
      {children}
    </button>
  )
}

/** Non-actionable text inside a menu ("No playlists yet."). */
export function MenuNote({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div role="menuitem" aria-disabled="true" className={className}>
      {children}
    </div>
  )
}

export function MenuSeparator({ className }: { className?: string }) {
  // A divider has no name to give.
  // oxlint-disable-next-line jsx-a11y/control-has-associated-label
  return <div role="separator" className={className} />
}
