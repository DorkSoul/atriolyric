import { useActionState, useId, useState } from 'react'
import { ApiError } from '../api'
import { useAuth } from '../AuthContext'
import { PasswordInput } from './PasswordInput'
import { SubmitButton } from './SubmitButton'

// Each failure gets its own message: a wrong username or password, the rate
// limit, or a server that could not be reached.
function loginErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 401) return 'Incorrect username or password.'
  if (error instanceof ApiError && error.status === 429) {
    const seconds = error.retryAfterSeconds
    if (seconds == null || seconds <= 0) return 'Too many sign-in attempts - try again later.'
    const wait = seconds < 60
      ? `${Math.ceil(seconds)} second${Math.ceil(seconds) === 1 ? '' : 's'}`
      : `${Math.ceil(seconds / 60)} minute${Math.ceil(seconds / 60) === 1 ? '' : 's'}`
    return `Too many sign-in attempts - try again in ${wait}.`
  }
  // Status 0 is no answer at all (offline, a timeout): the message below.
  if (error instanceof ApiError && error.status > 0 && error.status < 500) return error.message
  return "Couldn't reach the server - try again in a moment."
}

export function LoginPage() {
  const { login, sessionExpired } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  // The inputs are controlled, so the reset React gives a form after its
  // action leaves what was typed in place for another attempt.
  const [result, signIn, signingIn] = useActionState(async (): Promise<string | null> => {
    try {
      // A phone's keyboard leaves a space after the name it suggested; the
      // server matches the name exactly. The password goes as typed.
      await login(username.trim(), password)
      return null
    } catch (err) {
      return loginErrorMessage(err)
    }
  }, null)
  // A new attempt clears the last one's message.
  const error = signingIn ? null : result
  const id = useId()
  const errorId = `${id}-error`

  return (
    <div className="login-page">
      <form className="login-form" action={signIn}>
        <h1>
          <img src="/favicon.svg" alt="" className="app-logo" />
          Atriolyric
        </h1>
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
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? errorId : undefined}
            autoFocus
          />
        </div>
        <div className="form-field">
          <label htmlFor={`${id}-password`}>Password</label>
          <PasswordInput
            id={`${id}-password`}
            placeholder="Password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? errorId : undefined}
          />
        </div>
        {sessionExpired && !error && <output className="login-error">Your session has expired - please sign in again.</output>}
        {error && <p className="login-error" id={errorId} role="alert">{error}</p>}
        <SubmitButton pendingLabel="Signing in…">Sign in</SubmitButton>
      </form>
    </div>
  )
}
