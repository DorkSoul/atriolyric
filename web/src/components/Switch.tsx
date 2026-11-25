import { useState, type ChangeEvent, type ReactNode } from 'react'

interface SwitchProps {
  /** Controlled state; pass `defaultChecked` instead for an uncontrolled form field. */
  checked?: boolean
  defaultChecked?: boolean
  onChange: (checked: boolean, event: ChangeEvent<HTMLInputElement>) => void
  /** Visible text beside the track (part of the accessible name). */
  children?: ReactNode
  /** No pill outline: for settings rows and forms, where the label may wrap. */
  plain?: boolean
  className?: string
  disabled?: boolean
  'aria-label'?: string
  'aria-labelledby'?: string
  'aria-describedby'?: string
}

/** An on/off sliding switch - the app's one style for a yes/no choice (the
 * same look as "Show missing"). A real checkbox with role="switch" sits over
 * the whole control, so a tap anywhere on it toggles, and keyboard and screen
 * readers get a native control; the track draws the state. */
export function Switch({
  checked,
  defaultChecked,
  onChange,
  children,
  plain,
  className,
  disabled,
  ...aria
}: SwitchProps) {
  // An uncontrolled switch (a form field given `defaultChecked`) leaves the
  // DOM in charge - a form action's reset puts it back to defaultChecked - and
  // only mirrors its state for aria-checked. Callers keep defaultChecked equal
  // to the value they hold, so a reset after a refused submit keeps it.
  const controlled = checked !== undefined
  const [ownChecked, setOwnChecked] = useState(defaultChecked ?? false)
  const [seenDefault, setSeenDefault] = useState(defaultChecked)
  if (!controlled && defaultChecked !== seenDefault) {
    setSeenDefault(defaultChecked)
    setOwnChecked(defaultChecked ?? false)
  }
  const on = controlled ? checked : ownChecked
  const classes = ['switch', plain ? 'switch-plain' : '', className ?? ''].filter(Boolean).join(' ')
  return (
    <label className={classes}>
      <input
        type="checkbox"
        role="switch"
        aria-checked={on}
        {...(controlled ? { checked } : { defaultChecked })}
        disabled={disabled}
        onChange={(e) => {
          if (!controlled) setOwnChecked(e.target.checked)
          onChange(e.target.checked, e)
        }}
        {...aria}
      />
      <span className="switch-track" aria-hidden="true" />
      {children}
    </label>
  )
}
