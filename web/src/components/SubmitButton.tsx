import type { ReactNode } from 'react'
import { useFormStatus } from 'react-dom'

interface Props {
  /** The label while the form's action runs ("Creating…"). */
  pendingLabel: ReactNode
  /** Disabled for a reason of the form's own (an empty field), besides while
   * the action runs. */
  disabled?: boolean
  className?: string
  children: ReactNode
}

/** A form's submit button that says it is working and cannot be pressed
 * again while the form's action runs (a second press, or Enter in a field,
 * would otherwise queue a second request). Reads the form's state itself, so
 * the form does not pass "pending" down. */
export function SubmitButton({ pendingLabel, disabled = false, className, children }: Props) {
  const { pending } = useFormStatus()
  return (
    <button type="submit" className={className} disabled={pending || disabled}>
      {pending ? pendingLabel : children}
    </button>
  )
}
