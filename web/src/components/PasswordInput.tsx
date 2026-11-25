import { useState, type InputHTMLAttributes, type Ref } from 'react'
import { EyeIcon, EyeOffIcon } from './Icons'

type Props = Omit<InputHTMLAttributes<HTMLInputElement>, 'type'> & {
  /** What the field holds, for the reveal toggle's label ("Show token"). */
  secretName?: string
  /** React 19 passes `ref` as a prop: it reaches the input. */
  ref?: Ref<HTMLInputElement>
}

/** A password `<input>` with a toggle to reveal what was typed - wraps the
 * input rather than replacing it, so every existing prop (value, onChange,
 * placeholder, autoComplete, etc.) still passes straight through. */
export function PasswordInput({ secretName = 'password', ...props }: Props) {
  const [visible, setVisible] = useState(false)
  const toggleLabel = `${visible ? 'Hide' : 'Show'} ${secretName}`

  return (
    <div className="password-input-wrap">
      <input {...props} type={visible ? 'text' : 'password'} />
      <button
        type="button"
        className="password-toggle-btn"
        onClick={() => setVisible((v) => !v)}
        aria-label={toggleLabel}
        title={toggleLabel}
      >
        {visible ? <EyeOffIcon /> : <EyeIcon />}
      </button>
    </div>
  )
}
