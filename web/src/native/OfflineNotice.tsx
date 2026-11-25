import { useOffline } from './offline'
import '../styles/downloads.css'

/** Shown in the Android app while its server cannot be reached: the app goes
 * on working, with only the downloads to play. Gone by itself when the
 * server answers again. */
export default function OfflineNotice() {
  if (!useOffline()) return null
  return (
    <output className="offline-notice" data-testid="offline-notice">
      Offline · only downloads play
    </output>
  )
}
