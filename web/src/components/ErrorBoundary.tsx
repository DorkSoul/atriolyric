import { Component, type ReactNode } from 'react'

interface Props {
  children: ReactNode
  /** 'view' wraps the area under the header, so the shell, the tabs and the
   * player keep working when one view throws; 'root' is the last resort. */
  variant: 'view' | 'root' | 'overlay'
  /** For 'overlay' (Now Playing, a dialog, the player bar): called once when
   * something below fails, with whether it was the code failing to load. The
   * caller closes the overlay and tells the user; nothing is drawn in its
   * place, so the page under it, and the music, carry on. */
  onFailed?: (cause: 'load' | 'error') => void
  /** The fallback clears itself when this changes (navigating to another
   * view), so one broken view does not stay broken after leaving it. */
  resetKey?: string
}

interface State {
  failed: boolean
  /** The `resetKey` the current `failed` belongs to. */
  resetKey: string | undefined
}

/** A lazy view's code could not be fetched: offline, or a deploy replaced the
 * file this build asks for. Each browser words it differently. */
function isChunkLoadError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)
  return /dynamically imported module|Importing a module script failed|module script/i.test(message)
}

/** Catches an exception thrown while rendering below it and shows a way out
 * instead of a blank page. Reporting is not done here: the app's root passes
 * `onCaughtError` to `createRoot` (see main.tsx), which sees every caught error. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { failed: false, resetKey: this.props.resetKey }

  static getDerivedStateFromError(): Partial<State> {
    return { failed: true }
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    return props.resetKey === state.resetKey ? null : { failed: false, resetKey: props.resetKey }
  }

  componentDidCatch(error: unknown) {
    this.props.onFailed?.(isChunkLoadError(error) ? 'load' : 'error')
  }

  render() {
    if (!this.state.failed) return this.props.children
    if (this.props.variant === 'overlay') return null
    if (this.props.variant === 'root') {
      return (
        <div className="login-page">
          <div className="login-form" role="alert">
            <h1>Something went wrong</h1>
            <p>Atriolyric hit an unexpected problem. Reloading usually fixes it.</p>
            <button type="button" onClick={() => window.location.reload()}>
              Reload app
            </button>
          </div>
        </div>
      )
    }
    return (
      <div className="empty error-state" role="alert">
        <p className="error-state-message">Something went wrong — this view could not be shown.</p>
        <button type="button" onClick={() => this.setState({ failed: false })}>
          Reload this view
        </button>
      </div>
    )
  }
}
