import { useId, useState, type ReactNode } from 'react'
import { menuTriggerProps, type MenuAnchor } from '../menuTrigger'
import { MoreHorizontalIcon } from './Icons'

/** The visible "..." button. Never navigates or starts the long-press timer
 * of whatever it sits next to/inside. Its name says what it is for, so a page
 * of them is not thirty identical "More actions". */
export function MoreButton({
  label,
  className = '',
  onOpen,
  open = false,
  menuId = '',
}: {
  label: string
  className?: string
  onOpen: (anchor: MenuAnchor) => void
  /** Whether this button's menu is showing, and that menu's id. */
  open?: boolean
  menuId?: string
}) {
  return (
    <button
      type="button"
      className={`more-btn ${className}`}
      aria-label={`More actions for ${label}`}
      title="More actions"
      {...menuTriggerProps(open, menuId)}
      onPointerDown={(e) => e.stopPropagation()}
      onPointerUp={(e) => e.stopPropagation()}
      onContextMenu={(e) => e.stopPropagation()}
      onClick={(e) => {
        e.stopPropagation()
        e.preventDefault()
        const rect = e.currentTarget.getBoundingClientRect()
        onOpen({ x: rect.left, y: rect.bottom + 4, trigger: e.currentTarget })
      }}
    >
      <MoreHorizontalIcon />
    </button>
  )
}

/** A self-contained "..." button plus the menu it opens - for headers and
 * other places without a long-press hook of their own. */
export function MoreMenu({
  label,
  className,
  children,
}: {
  label: string
  className?: string
  children: (anchor: MenuAnchor, close: () => void, menuId: string) => ReactNode
}) {
  const [anchor, setAnchor] = useState<MenuAnchor | null>(null)
  const menuId = useId()

  return (
    <span className="more-menu-wrap">
      <MoreButton
        label={label}
        className={className}
        open={anchor != null}
        menuId={menuId}
        onOpen={(a) => setAnchor(anchor ? null : a)}
      />
      {anchor && children(anchor, () => setAnchor(null), menuId)}
    </span>
  )
}
