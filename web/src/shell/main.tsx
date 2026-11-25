// The page bundled into the Android app (see vite.shell.config.ts). The app
// itself is the web app loaded from the user's server (and from the app's own
// copy of it when there is no connection); this page is only what comes
// before that: it asks for the server's address, and says so when a server
// that was never loaded cannot be reached.

import { StrictMode, useCallback, useEffect, useState, type FormEvent } from 'react'
import { createRoot } from 'react-dom/client'
import '../index.css'
import '../styles/shell.css'
import { nativeCall, syncSystemBars, type ShellState } from '../native/bridge'

// This page is always in the dark palette, whatever the web app last used.
syncSystemBars()

type Stage = 'loading' | 'setup' | 'connecting' | 'unreachable'

function Shell() {
  const [stage, setStage] = useState<Stage>('loading')
  const [shell, setShell] = useState<ShellState | null>(null)
  const [address, setAddress] = useState('')
  const [error, setError] = useState<string | null>(null)

  const connect = useCallback(async () => {
    setError(null)
    setStage('connecting')
    try {
      // On success the app reloads onto the server's page; nothing more runs here.
      await nativeCall('connect')
    } catch {
      setStage('unreachable')
    }
  }, [])

  useEffect(() => {
    void nativeCall<ShellState>('shellState').then((current) => {
      setShell(current)
      setAddress(current.serverUrl ?? '')
      // Here to change the server, or because none is set.
      if (!current.serverUrl || current.reason === 'chosen') setStage('setup')
      // Sent back here because the server's page could not be loaded: do not loop.
      else if (current.reason) setStage('unreachable')
      else void connect()
    }, () => setStage('setup'))
  }, [connect])

  async function saveServer(event: FormEvent) {
    event.preventDefault()
    setError(null)
    try {
      const saved = await nativeCall<{ serverUrl: string }>('setServerUrl', { url: address })
      setShell((previous) => ({ serverUrl: saved.serverUrl, remote: false, reason: null, appVersion: previous?.appVersion ?? '' }))
      setStage('connecting')
      try {
        await nativeCall('connect')
      } catch {
        setStage('setup')
        setError(`No Atriolyric server answered at ${saved.serverUrl}. Check the address and that this device can reach it.`)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Enter the address of your Atriolyric server.')
    }
  }

  if (stage === 'loading' || stage === 'connecting') {
    return (
      <main className="shell">
        <div className="shell-centre" role="status">
          <h1>Atriolyric</h1>
          <p className="shell-note">{stage === 'connecting' ? `Connecting to ${shell?.serverUrl ?? 'your server'}…` : 'Starting…'}</p>
        </div>
      </main>
    )
  }

  if (stage === 'unreachable') {
    return (
      <main className="shell" data-testid="unreachable-view">
        <h1>Cannot reach your server</h1>
        <p className="shell-note">
          {shell?.serverUrl ?? 'Your server'} did not answer, and this device has not loaded it before.
          Once it has, the app opens with or without a connection.
        </p>
        <div className="shell-actions">
          <button type="button" className="btn btn-primary" onClick={() => void connect()}>Try again</button>
          <button type="button" className="btn btn-ghost" onClick={() => setStage('setup')}>Change server</button>
        </div>
      </main>
    )
  }

  return (
    <main className="shell">
      <h1>Connect to your server</h1>
      <p className="shell-note">Enter the address you use to open Atriolyric in a browser. Without http:// or https:// in front, https:// is used.</p>
      <form className="shell-form" onSubmit={(event) => void saveServer(event)}>
        {/* type="text": a url field would refuse an address without https:// before
            the app could add it. */}
        <input
          type="text"
          inputMode="url"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          placeholder="music.example.org"
          aria-label="Server address"
          value={address}
          onChange={(event) => setAddress(event.target.value)}
        />
        {error && <p className="shell-error" role="alert">{error}</p>}
        <div className="shell-actions">
          <button type="submit" className="btn btn-primary">Connect</button>
          {shell?.serverUrl && (
            // Back to the app as it was, connection or not.
            <button type="button" className="btn btn-ghost" onClick={() => void nativeCall('openApp').catch(() => setStage('unreachable'))}>
              Cancel
            </button>
          )}
        </div>
      </form>
    </main>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Shell />
  </StrictMode>,
)
