import { useState } from 'react'
import { useToast } from '../ToastContext'
import { installAppUpdate, useAndroidApp } from './appUpdate'

/** In the Android app: says when the server carries a newer app than the one
 * installed, and installs it on request (Android asks before replacing it). */
export default function AppUpdatePrompt() {
  const { offered, installed, updateAvailable } = useAndroidApp()
  const { showToast } = useToast()
  const [dismissed, setDismissed] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  if (!updateAvailable || !offered || offered.version === dismissed) return null
  // A labelled region, not an alertdialog: it neither takes focus nor
  // blocks the page (the app goes on working under it), which an alert
  // dialog promises screen readers it does.
  return (
    <div className="update-prompt" role="region" aria-labelledby="app-update-prompt-title" data-testid="app-update-prompt">
      <div className="update-prompt-text">
        <strong id="app-update-prompt-title">App update available</strong>
        <span>
          Version {offered.version} of the Android app is ready (you have {installed}).
        </span>
      </div>
      <div className="update-prompt-actions">
        <button type="button" className="update-prompt-later" onClick={() => setDismissed(offered.version)}>
          Later
        </button>
        <button
          type="button"
          className="update-prompt-restart"
          disabled={busy}
          onClick={() => {
            setBusy(true)
            installAppUpdate()
              .then(() => setDismissed(offered.version))
              .catch((error: unknown) => showToast(error instanceof Error ? error.message : 'Could not update the app.', 'error'))
              .finally(() => setBusy(false))
          }}
        >
          {busy ? 'Downloading…' : 'Update'}
        </button>
      </div>
    </div>
  )
}
