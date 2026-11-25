import { useRef } from 'react'
import { effectiveArt, resetArt, uploadArt, useArtRevision, type ArtRef } from '../artEntity'
import { openArtPicker } from '../artPicker'
import { downloadCollection, downloadMedia, type CollectionKind } from '../download'
import { useAuth } from '../AuthContext'
import { useToast } from '../ToastContext'
import { MenuItem } from './Menu'
import { isNativeApp } from '../native/bridge'
import {
  downloadCollectionToDevice,
  removeCollectionFromDevice,
  useCollectionDownloadStatus,
} from '../native/downloadActions'
import { useDownloadStatus } from '../native/downloadStore'
import { useOffline } from '../native/offline'

export type DownloadTarget = { kind: 'media'; key: number } | { kind: CollectionKind; key: string | number }

/** "Choose from found artwork…", "Upload artwork…", "Reset art" (only while custom) and "Download" - the
 * entries every song/video/album/artist/playlist menu shares. Either part
 * is omitted when its target is unknown. */
export function EntityMenuItems({
  art,
  artLabel = '',
  download,
  downloadRefusal,
  onClose,
}: {
  art?: ArtRef
  /** What the art belongs to, shown in "Choose from found artwork…". */
  artLabel?: string
  download?: DownloadTarget
  /** Said in the Android app instead of downloading (a generated mix, which
   * a refresh replaces: it is saved as a playlist first). */
  downloadRefusal?: string
  onClose: () => void
}) {
  const { showToast } = useToast()
  const { user } = useAuth()
  // Album, artist and track art is shared by everyone on the server, and it
  // only accepts a change from an admin; a playlist's art is the owner's own.
  const canEditArt = art?.type === 'playlist' || !!user?.is_admin
  const inputRef = useRef<HTMLInputElement>(null)
  useArtRevision()
  const custom = art ? effectiveArt(art).custom : false

  async function onFile(file: File | undefined) {
    if (inputRef.current) inputRef.current.value = ''
    onClose()
    if (!file || !art) return
    try {
      await uploadArt(art, file)
      showToast('Artwork updated.', 'success')
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Could not upload the artwork.', 'error')
    }
  }

  async function onReset() {
    onClose()
    if (!art) return
    try {
      await resetArt(art)
      showToast('Artwork reset.', 'success')
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Could not reset the artwork.', 'error')
    }
  }

  // In the Android app a download is kept in the app, at the download
  // quality, and plays from there with or without a connection. What is on
  // the device (or on its way) is offered for removal, never a second time.
  // An album, artist or playlist stays downloaded: what is added to it later
  // is fetched too and what leaves it goes, until it is removed here.
  const native = isNativeApp()
  const offline = useOffline()
  const mediaStatus = useDownloadStatus(download?.kind === 'media' ? download.key : 0)
  const collectionStatus = useCollectionDownloadStatus(download?.kind ?? 'media', download?.kind === 'media' ? 0 : download?.key ?? 0)
  const onDevice = download?.kind === 'media' ? mediaStatus : collectionStatus

  function onRemoveDownload() {
    onClose()
    if (!download) return
    const removing = removeCollectionFromDevice(download.kind, download.key)
    removing.then(
      () => showToast('Removed from this device.', 'success'),
      () => showToast('Could not remove the download.', 'error'),
    )
  }

  function onDownload() {
    onClose()
    if (!download) return
    if (native && downloadRefusal && onDevice === 'none') {
      showToast(downloadRefusal, 'error')
      return
    }
    if (native) {
      // Said at once: a video may first be prepared by the server for minutes.
      showToast(
        download.kind === 'media'
          ? 'Downloading to this device.'
          : 'Downloading to this device. Anything added to it later will download automatically.',
        'success',
      )
      downloadCollectionToDevice(download.kind, download.key).then(
        ({ started }) => {
          if (started === 0) showToast('Already on this device.', 'success')
        },
        (err: unknown) => showToast(err instanceof Error ? err.message : 'Could not start the download.', 'error'),
      )
      return
    }
    if (download.kind === 'media') downloadMedia(download.key, showToast)
    else void downloadCollection(download.kind, download.key, showToast)
  }

  return (
    <>
      {art && canEditArt && (
        <>
          {art.type !== 'playlist' && (
            <MenuItem
              onSelect={() => {
                onClose()
                openArtPicker(art, artLabel)
              }}
            >
              Choose from found artwork…
            </MenuItem>
          )}
          <MenuItem onSelect={() => inputRef.current?.click()}>Upload artwork…</MenuItem>
          <input
            ref={inputRef}
            type="file"
            accept="image/*"
            hidden
            aria-label="Choose artwork image"
            onChange={(e) => void onFile(e.target.files?.[0])}
          />
          {custom && <MenuItem onSelect={() => void onReset()}>Reset art</MenuItem>}
        </>
      )}
      {download && !native && <MenuItem onSelect={onDownload}>Download</MenuItem>}
      {download && native && (onDevice === 'downloaded' || onDevice === 'downloading') && (
        <MenuItem onSelect={onRemoveDownload}>{onDevice === 'downloaded' ? 'Remove download' : 'Cancel download'}</MenuItem>
      )}
      {/* A downloaded album, artist or playlist fetches what it gains by itself. */}
      {download && native && !offline && onDevice !== 'downloaded' && onDevice !== 'downloading' && (
        <MenuItem onSelect={onDownload}>Download to this device</MenuItem>
      )}
    </>
  )
}
