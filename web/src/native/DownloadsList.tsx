import { useEffect, useState } from 'react'
import { useConfirm } from '../dialogs'
import { useToast } from '../ToastContext'
import { nativeCall, type NativeDownload } from './bridge'
import { downloadArtUrl, formatBytes, removeDownload, totalDownloadedBytes, useDownloads } from './downloadStore'
import '../styles/downloads.css'

/** Rows rendered at first, and added by each "Show more", so a phone with
 * thousands of downloads does not render them all at once, each asking for
 * its own picture. */
const PAGE_SIZE = 100

/** The art of an app too old to serve it by address (it does not say an
 * `artVersion`): asked once over the bridge. */
function useLegacyArt(item: NativeDownload): string | null {
  const [src, setSrc] = useState<string | null>(null)
  const legacy = item.hasArt && item.artVersion == null
  useEffect(() => {
    if (!legacy) return
    let live = true
    void nativeCall<{ dataUrl: string | null }>('downloadArt', { mediaId: item.mediaId }).then(
      ({ dataUrl }) => {
        if (live) setSrc(dataUrl)
      },
      () => {},
    )
    return () => {
      live = false
    }
  }, [item.mediaId, legacy])
  return legacy ? src : null
}

function DownloadArt({ item }: { item: NativeDownload }) {
  const legacy = useLegacyArt(item)
  const src = downloadArtUrl(item) ?? legacy
  return src
    ? <img className="download-art" src={src} alt="" loading="lazy" decoding="async" />
    : <div className="download-art" aria-hidden="true" />
}

function stateText(item: NativeDownload): string | null {
  if (item.state === 'downloading') {
    return item.percent != null ? `Downloading ${Math.round(item.percent)}%` : 'Downloading'
  }
  if (item.state === 'queued' || item.state === 'stopped') return 'Waiting'
  if (item.state === 'removing') return 'Removing'
  return null
}

/** Every song and video on the device with its quality and size, and Play and
 * Remove for each. `onPlay` gets the item and the finished downloads in list
 * order, so a caller can queue the songs that follow. */
export function DownloadsList({
  onPlay,
}: {
  onPlay: (item: NativeDownload, completed: NativeDownload[]) => void
}) {
  const { items, preparing, loaded } = useDownloads()
  const confirm = useConfirm()
  const { showToast } = useToast()
  const [shown, setShown] = useState(PAGE_SIZE)
  const completed = items.filter((item) => item.state === 'completed')

  /** Removing deletes the file; getting it back means downloading it again
   * (a video may first need minutes on the server), so ask first. */
  async function remove(item: NativeDownload) {
    const yes = await confirm({
      title: 'Remove download?',
      body: `"${item.title || 'Untitled'}" will be deleted from this device. You can download it again later.`,
      confirmLabel: 'Remove',
      danger: true,
    })
    if (!yes) return
    removeDownload(item.mediaId).catch(() => showToast('Could not remove the download.', 'error'))
  }
  if (!loaded) return <p className="downloads-empty" role="status">Loading downloads…</p>
  if (items.length === 0 && preparing.length === 0) {
    return <p className="downloads-empty">Nothing is downloaded to this device yet.</p>
  }
  return (
    <>
      <p className="downloads-summary" data-testid="downloads-summary">
        {completed.length} {completed.length === 1 ? 'item' : 'items'}, {formatBytes(totalDownloadedBytes(items))} on this device
      </p>
      <ul className="downloads-list" aria-label="Downloads">
        {preparing.map((entry) => (
          <li key={`preparing-${entry.mediaId}`} className="download-row" data-testid="download-preparing">
            <div className="download-art" aria-hidden="true" />
            <div className="download-text">
              <div className="download-title">{entry.title}</div>
              <div className="download-meta">
                {entry.qualityLabel} · The server is preparing this video
                {entry.progress != null ? ` (${Math.round(entry.progress * 100)}%)` : ''}
              </div>
            </div>
            <span />
          </li>
        ))}
        {items.slice(0, shown).map((item) => {
          const busy = stateText(item)
          return (
            <li
              key={item.mediaId}
              className="download-row"
              data-testid="download-row"
              data-media-id={item.mediaId}
              data-state={item.state}
              data-kind={item.kind}
            >
              <DownloadArt item={item} />
              <div className="download-text">
                <div className="download-title">{item.title || 'Untitled'}</div>
                <div className="download-meta">
                  {item.artist ? `${item.artist} · ` : ''}
                  {item.kind === 'track' ? '' : 'Video · '}
                  <span data-testid="download-quality">{item.qualityLabel || item.quality}</span>
                  {' · '}
                  {item.state === 'failed'
                    ? <span className="download-failed">Download failed</span>
                    : busy ?? <span data-testid="download-size">{formatBytes(item.bytes)}</span>}
                </div>
                {item.state === 'downloading' && (
                  <progress className="download-progress" max={100} value={item.percent ?? undefined} aria-label={`Downloading ${item.title}`} />
                )}
              </div>
              <div className="download-actions">
                {item.state === 'completed' && (
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => onPlay(item, completed)} aria-label={`Play ${item.title}`}>
                    Play
                  </button>
                )}
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => void remove(item)}
                  aria-label={`Remove ${item.title} from this device`}
                >
                  Remove
                </button>
              </div>
            </li>
          )
        })}
      </ul>
      {items.length > shown && (
        <div className="settings-row settings-row-start">
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShown((count) => count + PAGE_SIZE)}>
            Show more ({items.length - shown} left)
          </button>
        </div>
      )}
    </>
  )
}
