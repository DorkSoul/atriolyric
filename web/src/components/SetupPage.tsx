import { useActionState, useId, useState } from 'react'
import { api, ApiError } from '../api'
import { useAuth } from '../AuthContext'
import { PasswordInput } from './PasswordInput'
import { SubmitButton } from './SubmitButton'

interface SetupError {
  message: string
  field: 'password' | 'confirm' | null
}

/** First run: no accounts exist yet. Creates the admin account, gated by
 * the one-time setup code the server printed to its log. */
export function SetupPage({ onComplete }: { onComplete: () => void }) {
  const { setUser } = useAuth()
  const [code, setCode] = useState('')
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const id = useId()
  const errorId = `${id}-error`
  // The error, and which field it is about so only that one is flagged. The
  // inputs are controlled: what was typed stays for another attempt.
  const [result, createAccount, creating] = useActionState(async (): Promise<SetupError | null> => {
    if (password.length < 8) return { message: 'Password must be at least 8 characters.', field: 'password' }
    if (password !== confirm) return { message: "Passwords don't match.", field: 'confirm' }
    try {
      const created = await api.completeSetup(code.trim(), username.trim(), password)
      onComplete()
      setUser(created)
      return null
    } catch (e) {
      return { message: e instanceof ApiError ? e.message : 'Could not create the account.', field: null }
    }
  }, null)
  // A new attempt clears the last one's message.
  const error = creating ? null : result?.message ?? null
  const errorField = creating ? null : result?.field ?? null

  return (
    <div className="login-page">
      <form className="login-form" action={createAccount}>
        <h1>
          <img src="/favicon.svg" alt="" className="app-logo" />
          Atriolyric
        </h1>
        <p className="setup-intro">
          Create the admin account. The setup code is in the server log
          (<code>docker logs atriolyric</code>).
        </p>
        <div className="form-field">
          <label htmlFor={`${id}-code`}>Setup code</label>
          <input
            id={`${id}-code`}
            placeholder="Setup code"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            autoComplete="one-time-code"
            autoFocus
          />
        </div>
        <div className="form-field">
          <label htmlFor={`${id}-username`}>Username</label>
          <input
            id={`${id}-username`}
            placeholder="Username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            // A name, not prose: a phone must not capitalise or "correct" it.
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
          />
        </div>
        <div className="form-field">
          <label htmlFor={`${id}-password`}>Password</label>
          <PasswordInput
            id={`${id}-password`}
            placeholder="Password (min. 8 characters)"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            aria-invalid={errorField === 'password' ? true : undefined}
            aria-describedby={errorField === 'password' ? errorId : undefined}
          />
        </div>
        <div className="form-field">
          <label htmlFor={`${id}-confirm`}>Confirm password</label>
          <PasswordInput
            id={`${id}-confirm`}
            placeholder="Confirm password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            aria-invalid={errorField === 'confirm' ? true : undefined}
            aria-describedby={errorField === 'confirm' ? errorId : undefined}
          />
        </div>
        {error && <p className="login-error" id={errorId} role="alert">{error}</p>}
        <SubmitButton pendingLabel="Creating account…" disabled={!code.trim() || !username.trim()}>
          Create admin account
        </SubmitButton>
      </form>
    </div>
  )
}
