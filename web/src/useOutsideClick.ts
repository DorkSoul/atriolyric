import { useEffect, useEffectEvent, type RefObject } from 'react'

/** Something a press can land "inside": a ref'd element, or a CSS selector
 * matched against the pressed element and its ancestors (for panels that are
 * not under one ref, e.g. a menu portaled out of its trigger's wrapper). */
export type OutsideClickInside = RefObject<Element | null> | string

function isInside(target: EventTarget | null, inside: OutsideClickInside): boolean {
  if (typeof inside === 'string') return target instanceof Element && target.closest(inside) !== null
  return inside.current?.contains(target as Node) ?? false
}

/** Whether the press landed in a dialog that is not around the given areas -
 * one opened on top of them (a confirm asked from a menu item). Its buttons
 * are the answer the user is giving, so their click must not be swallowed. */
function inLaterDialog(target: EventTarget | null, areas: OutsideClickInside[]): boolean {
  if (!(target instanceof Element)) return false
  const dialog = target.closest('dialog, [role="dialog"], [role="alertdialog"]')
  if (!dialog) return false
  return !areas.some((area) => typeof area !== 'string' && area.current !== null && dialog.contains(area.current))
}

/** Calls `onOutside` for a pointer press anywhere except inside the given
 * ref(s)/selector(s). Listens only while `enabled`, on the document's bubble
 * phase unless `capture` says otherwise - the phase matters: a React handler
 * that stops propagation (a "..." button, say) hides its press from a bubble
 * listener, which is how those buttons toggle their own menu.
 *
 * With `swallowClick`, the outside press only closes: the click that press
 * would produce is stopped before it reaches whatever is underneath (a card,
 * a link), so dismissing a menu never also opens something. */
export function useOutsideClick(
  inside: OutsideClickInside | OutsideClickInside[],
  onOutside: (event: PointerEvent) => void,
  enabled = true,
  capture = false,
  swallowClick = false,
) {
  // Latest values without re-subscribing on every render.
  const handlePointerDown = useEffectEvent((event: PointerEvent) => {
    const areas = Array.isArray(inside) ? inside : [inside]
    if (areas.some((area) => isInside(event.target, area))) return
    if (swallowClick && !inLaterDialog(event.target, areas)) swallowNextClick()
    onOutside(event)
  })

  useEffect(() => {
    if (!enabled) return
    const listener = (event: PointerEvent) => handlePointerDown(event)
    document.addEventListener('pointerdown', listener, capture)
    return () => document.removeEventListener('pointerdown', listener, capture)
  }, [enabled, capture])
}

/** How long after the outside press its click may still arrive. A press that
 * turns into a scroll never clicks, so the guard must not wait forever and
 * eat a later, unrelated click. */
const SWALLOW_WINDOW_MS = 800

/** Stops the next click anywhere in the document (capture phase, so no
 * handler underneath sees it). Dropped after one click, at the next press, or
 * once the window has passed. */
function swallowNextClick() {
  let timer = 0
  const clear = () => {
    window.clearTimeout(timer)
    document.removeEventListener('click', onClick, true)
    document.removeEventListener('pointerdown', onNextPress, true)
  }
  const onClick = (event: MouseEvent) => {
    event.preventDefault()
    event.stopPropagation()
    clear()
  }
  // The press that armed this is already being dispatched; only a later one
  // (a second, separate press) clears it.
  const onNextPress = () => clear()
  document.addEventListener('click', onClick, true)
  window.setTimeout(() => document.addEventListener('pointerdown', onNextPress, true), 0)
  timer = window.setTimeout(clear, SWALLOW_WINDOW_MS)
}
