import { Switch } from '../components/Switch'
import { useConfirm } from '../dialogs'
import { usePlayerActions } from '../PlayerContext'
import { STREAM_QUALITIES } from '../settings'
import { useToast } from '../ToastContext'
import { nativeCall } from './bridge'
import { DownloadsList } from './DownloadsList'
import {
  downloadsAtOtherQuality,
  musicQualityLabel,
  redownloadAtChosenQuality,
  updateDownloadSettings,
  useDownloadSettings,
  VIDEO_DOWNLOAD_QUALITIES,
  videoQualityLabel,
  type DownloadSettings,
} from './downloadActions'
import { isOffline } from './offline'

/** Settings > Downloads in the Android app: the two download qualities (apart
 * from the streaming quality) and what is on the device. */
export function DownloadsSettings() {
  const chosen = useDownloadSettings()
  const { playQueueIds } = usePlayerActions()
  const { showToast } = useToast()
  const confirm = useConfirm()

  /** Changes a download quality. What is on the device at another quality no
   * longer matches: after a yes it is deleted and downloaded again. */
  async function choose(patch: Partial<DownloadSettings>) {
    const stale = downloadsAtOtherQuality(patch)
    if (stale.length === 0) {
      updateDownloadSettings(patch)
      return
    }
    const video = patch.video !== undefined
    const things = video ? (stale.length === 1 ? 'video' : 'videos') : stale.length === 1 ? 'song' : 'songs'
    const quality = video ? videoQualityLabel(patch.video ?? 'original') : musicQualityLabel(patch.musicKbps ?? null)
    if (isOffline()) {
      showToast(`Connect to your server to change this: the ${things} on this device would have to be downloaded again.`, 'error')
      return
    }
    const yes = await confirm({
      title: 'Change download quality?',
      body: `${stale.length} ${things} on this device ${stale.length === 1 ? 'was' : 'were'} saved at another quality. ${
        stale.length === 1 ? 'It' : 'They'
      } will be deleted and downloaded again at ${quality}.`,
      confirmLabel: 'Delete and download again',
      danger: true,
    })
    if (!yes) return
    updateDownloadSettings(patch)
    showToast(`Downloading ${stale.length} ${things} again at ${quality}.`, 'success')
    redownloadAtChosenQuality(stale)
      .then(({ failed }) => {
        if (failed > 0) {
          showToast(`${failed} of ${stale.length} could not be downloaded again; ${failed === 1 ? 'it keeps' : 'they keep'} the quality ${failed === 1 ? 'it' : 'they'} had.`, 'error')
        }
      })
      .catch((err: unknown) =>
        showToast(err instanceof Error ? err.message : 'Could not download everything again.', 'error'),
      )
  }

  return (
    <>
      <p className="settings-hint">
        The quality songs and videos are saved at. This is separate from the streaming quality
        above. A downloaded copy is always the one that plays, with or without a connection, and
        with no connection the app shows what it loaded before and plays your downloads.
      </p>
      <div className="settings-row"><span>Music downloads</span></div>
      <div className="settings-segmented settings-segmented-wide" role="group" aria-label="Music download quality">
        <button
          className={chosen.musicKbps === null ? 'active' : ''}
          aria-pressed={chosen.musicKbps === null}
          onClick={() => void choose({ musicKbps: null })}
        >
          Original
        </button>
        {[...STREAM_QUALITIES].reverse().map((q) => (
          <button
            key={q}
            className={chosen.musicKbps === q ? 'active' : ''}
            aria-pressed={chosen.musicKbps === q}
            onClick={() => void choose({ musicKbps: q })}
          >
            {q} kbps
          </button>
        ))}
      </div>
      <div className="settings-row settings-hint-spaced"><span>Video downloads</span></div>
      <div className="settings-segmented settings-segmented-wide" role="group" aria-label="Video download quality">
        {VIDEO_DOWNLOAD_QUALITIES.map((q) => (
          <button
            key={q}
            className={chosen.video === q ? 'active' : ''}
            aria-pressed={chosen.video === q}
            onClick={() => void choose({ video: q })}
          >
            {videoQualityLabel(q)}
          </button>
        ))}
      </div>
      <p className="settings-hint settings-hint-spaced">
        A video is never enlarged: one smaller than the chosen size is saved at the next size
        down, or as the original. Changing a quality deletes what is on this device at another
        quality and downloads it again (you are asked first).
      </p>
      <div className="settings-row settings-hint-spaced">
        <span>Download over Wi-Fi only</span>
        <Switch
          plain
          aria-label="Download over Wi-Fi only"
          checked={chosen.wifiOnly}
          onChange={(wifiOnly) => updateDownloadSettings({ wifiOnly })}
        >
          {chosen.wifiOnly ? 'On' : 'Off'}
        </Switch>
      </div>
      <p className="settings-hint">
        On mobile data, downloads wait for Wi-Fi, and so do the app's background fetches (new songs of a
        downloaded playlist, the daily copy of your library for offline use).
      </p>
      <div className="settings-row settings-hint-spaced">
        <span>Server address</span>
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => void nativeCall('openShell', { reason: 'chosen' }).catch(() => {})}
        >
          Change server
        </button>
      </div>
      <DownloadsList
        onPlay={(item, completed) => {
          if (item.kind !== 'track') {
            void nativeCall('playVideo', { mediaId: item.mediaId }).catch(() => showToast('Could not play this video.', 'error'))
            return
          }
          const songs = completed.filter((d) => d.kind === 'track')
          playQueueIds(songs.map((d) => d.mediaId), Math.max(0, songs.findIndex((d) => d.mediaId === item.mediaId)))
        }}
      />
    </>
  )
}
