import { useId, useRef, useState } from 'react'
import { menuTriggerProps, type MenuAnchor } from '../menuTrigger'
import { ChevronDownIcon } from './Icons'
import { Menu, MenuItem } from './Menu'

export interface SelectOption<V extends string | number> {
  value: V
  label: string
}

/** The app's drop-down: a chip naming the current choice that opens a menu
 * of radio items, in place of a native <select> (which every browser and
 * phone draws its own way). The menu opens at the chip in a fixed layer, so
 * a scrolling dialog or card around it can't clip it; on a phone it becomes
 * the same centred sheet as the other menus.
 *
 * Its accessible name is `label` plus the current choice ("Equaliser preset:
 * Flat"), or the element named by `labelledBy`. */
export function SelectMenu<V extends string | number>({
  label,
  labelledBy,
  value,
  options,
  onChange,
  disabled,
  className,
}: {
  label: string
  labelledBy?: string
  value: V
  options: SelectOption<V>[]
  onChange: (value: V) => void
  disabled?: boolean
  className?: string
}) {
  const [anchor, setAnchor] = useState<MenuAnchor | null>(null)
  const toggleRef = useRef<HTMLButtonElement>(null)
  const menuId = useId()
  const current = options.find((o) => o.value === value)
  const open = anchor !== null

  function toggle() {
    if (open) {
      setAnchor(null)
      return
    }
    const button = toggleRef.current
    if (!button) return
    const rect = button.getBoundingClientRect()
    setAnchor({ x: rect.left, y: rect.bottom + 4, trigger: button })
  }

  function pick(next: V) {
    setAnchor(null)
    if (next !== value) onChange(next)
  }

  return (
    <>
      <button
        ref={toggleRef}
        type="button"
        className={['chip', 'select-menu-toggle', className ?? ''].filter(Boolean).join(' ')}
        onClick={toggle}
        disabled={disabled}
        aria-labelledby={labelledBy ? `${labelledBy} ${menuId}-value` : undefined}
        {...menuTriggerProps(open, menuId)}
      >
        {!labelledBy && <span className="visually-hidden">{label}: </span>}
        <span id={`${menuId}-value`} className="select-menu-value">
          {current?.label ?? String(value)}
        </span>
        <ChevronDownIcon aria-hidden="true" />
      </button>
      {anchor && (
        <Menu
          id={menuId}
          className="browse-sort-panel select-menu-panel"
          label={label}
          anchor={anchor}
          triggerRef={toggleRef}
          onClose={() => setAnchor(null)}
        >
          {options.map((o) => (
            <MenuItem
              key={String(o.value)}
              choice="radio"
              checked={o.value === value}
              className={o.value === value ? 'active' : ''}
              onSelect={() => pick(o.value)}
            >
              {o.label}
            </MenuItem>
          ))}
        </Menu>
      )}
    </>
  )
}
