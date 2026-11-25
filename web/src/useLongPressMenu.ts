import { useId, useState } from 'react'
import type { MenuAnchor } from './menuTrigger'
import { useLongPress } from './useLongPress'

/** Which item's menu is open and where. The menu itself (components/Menu)
 * closes on Escape, Tab and outside presses by calling `close`. */
export function useLongPressMenu<T>() {
  const [openFor, setOpenFor] = useState<T | null>(null)
  const [anchor, setAnchor] = useState<MenuAnchor | null>(null)
  const menuId = useId()

  function open(item: T, x: number, y: number, trigger?: HTMLElement | null) {
    setAnchor({ x, y, trigger })
    setOpenFor(item)
  }

  const press = useLongPress<T>((item, point) => open(item, point.x, point.y))

  function onContextMenu(e: React.MouseEvent, item: T) {
    e.preventDefault()
    open(item, e.clientX, e.clientY)
  }

  function close() {
    press.cancel()
    setOpenFor(null)
    setAnchor(null)
  }

  return {
    openFor,
    anchor,
    menuId,
    open,
    onPointerDown: press.start,
    onPointerUp: press.cancel,
    onPointerLeave: press.cancel,
    onPointerCancel: press.cancel,
    onContextMenu,
    consumeClick: press.consumeClick,
    close,
  }
}
