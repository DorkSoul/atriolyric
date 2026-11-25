import { useEffect, useState } from 'react'
import { onServerVersion } from '../serverVersion'

/** Hears which app version the server serves (serverVersion.ts asks once the
 * page is idle, and again whenever the app comes back to the foreground,
 * since a phone can keep one page alive for days) and, when it differs from
 * this build's, offers a restart. */
export default function UpdatePrompt() {
  const [serverVersion, setServerVersion] = useState<string | null>(null)
  const [dismissed, setDismissed] = useState<string | null>(null)

  // null in development, where Vite serves the app instead of a build.
  useEffect(() => onServerVersion((answer) => answer.version && setServerVersion(answer.version)), [])

  if (!serverVersion || serverVersion === __APP_VERSION__ || serverVersion === dismissed) return null
  // A labelled region, not an alertdialog: it neither takes focus nor
  // blocks the page (the app goes on working under it), which an alert
  // dialog promises screen readers it does.
  return (
    <div className="update-prompt" role="region" aria-labelledby="update-prompt-title">
      <div className="update-prompt-text">
        <strong id="update-prompt-title">Update available</strong>
        <span>
          Version {serverVersion} is ready (you have {__APP_VERSION__}). Restart to get it.
        </span>
      </div>
      <div className="update-prompt-actions">
        <button type="button" className="update-prompt-later" onClick={() => setDismissed(serverVersion)}>
          Later
        </button>
        <button type="button" className="update-prompt-restart" onClick={() => window.location.reload()}>
          Restart
        </button>
      </div>
    </div>
  )
}
