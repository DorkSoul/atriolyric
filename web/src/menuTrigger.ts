/** Where a menu opens, plus the element that opened it (so focus can go
 * back there and so pressing it again toggles instead of "clicking outside"). */
export interface MenuAnchor {
  x: number
  y: number
  trigger?: HTMLElement | null
}

/** Props for the button that opens a menu: the popup relationship for
 * assistive technology. `aria-controls` is only set while the menu exists,
 * since it must point at a real element. */
export function menuTriggerProps(open: boolean, menuId: string) {
  return {
    'aria-haspopup': 'menu' as const,
    'aria-expanded': open,
    'aria-controls': open ? menuId : undefined,
  }
}

