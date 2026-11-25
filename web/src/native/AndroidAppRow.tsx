import { useState } from 'react'
import { useToast } from '../ToastContext'
import { ANDROID_APP_URL, installAppUpdate, useAndroidApp } from './appUpdate'
import { isNativeApp } from './bridge'
import { formatBytes } from './downloadStore'

/** Settings > About: in a browser, the Android app as a download when the
 * server carries it; in the app, its version and an update when there is one. */
export function AndroidAppRow() {
  const { offered, installed, updateAvailable } = useAndroidApp()
  const { showToast } = useToast()
  const [busy, setBusy] = useState(false)

  if (isNativeApp()) {
    return (
      <>
        {installed && <p className="settings-hint" data-testid="app-version">Android app {installed}</p>}
        {updateAvailable && offered && (
          <div className="settings-row settings-row-start">
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setBusy(true)
                installAppUpdate()
                  .catch((error: unknown) => showToast(error instanceof Error ? error.message : 'Could not update the app.', 'error'))
                  .finally(() => setBusy(false))
              }}
            >
              {busy ? 'Downloading…' : `Update the app to ${offered.version}`}
            </button>
          </div>
        )}
      </>
    )
  }
  if (!offered) return null
  return (
    <>
      <div className="settings-row settings-row-start">
        <a className="btn btn-ghost" href={ANDROID_APP_URL} download>
          Get the Android app
        </a>
      </div>
      <p className="settings-hint">
        Version {offered.version}, {formatBytes(offered.size)}. It plays with the screen off and keeps
        downloads for offline use. Android will ask you to allow this browser to install apps.
      </p>
    </>
  )
}
