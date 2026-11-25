import type { ArtRef } from '../artEntity'
import { isNativeApp } from './bridge'
import { useCollectionDownloadStatus } from './downloadActions'
import { useDownloadPercent, useDownloadStatus, type DownloadStatus } from './downloadStore'
import '../styles/downloads.css'

function Mark({ status, percent, subject }: { status: DownloadStatus; percent: number | null; subject: string }) {
  if (status === 'none') return null
  const label =
    status === 'downloaded'
      ? `${subject}: downloaded`
      : status === 'failed'
        ? `${subject}: download failed`
        : `${subject}: downloading${percent != null ? ` ${percent}%` : ''}`
  return (
    <span className={`download-mark download-mark-${status}`} role="img" aria-label={label} title={label} data-testid="download-mark" data-status={status}>
      <svg viewBox="0 0 24 24" width="1em" height="1em" aria-hidden="true">
        {status === 'downloaded' ? (
          <>
            <circle cx="12" cy="12" r="10" fill="currentColor" />
            <path d="M12 7v8m0 0-3.5-3.5M12 15l3.5-3.5" fill="none" stroke="var(--bg)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </>
        ) : (
          <>
            <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="2" opacity={status === 'failed' ? 1 : 0.35} />
            {status === 'failed' ? (
              <path d="M12 7v6m0 3.5v.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            ) : (
              // The part fetched so far, as an arc from the top.
              // The turning is on a group of its own, about the ring's centre
              // in the drawing's own units: on the circle it would meet the
              // circle's own rotation and swing the arc off the ring.
              <g className={percent == null ? 'download-mark-spin' : undefined}>
                <circle
                  cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"
                  pathLength={100} strokeDasharray={`${Math.max(4, percent ?? 12)} 100`} transform="rotate(-90 12 12)"
                />
              </g>
            )}
          </>
        )}
      </svg>
    </span>
  )
}

function ItemMark({ mediaId, subject }: { mediaId: number; subject: string }) {
  return <Mark status={useDownloadStatus(mediaId)} percent={useDownloadPercent(mediaId)} subject={subject} />
}

function CollectionMark({ kind, itemKey, subject }: { kind: string; itemKey: string | number; subject: string }) {
  return <Mark status={useCollectionDownloadStatus(kind, itemKey)} percent={null} subject={subject} />
}

/** Whether one song or video is on this device: a filled mark when it is, a
 * ring that fills while it downloads, nothing otherwise. Android app only. */
export function DownloadMark({ mediaId, subject }: { mediaId: number; subject: string }) {
  return isNativeApp() ? <ItemMark mediaId={mediaId} subject={subject} /> : null
}

/** The same mark for an album, artist or playlist that is kept downloaded
 * (what is added to it later is fetched too). */
export function CollectionDownloadMark({ kind, itemKey, subject }: { kind: string; itemKey: string | number; subject: string }) {
  return isNativeApp() ? <CollectionMark kind={kind} itemKey={itemKey} subject={subject} /> : null
}

/** That mark for whatever `artRef` names: an album, an artist or a playlist. */
export function RefDownloadMark({ artRef, subject }: { artRef: ArtRef | undefined; subject: string }) {
  if (!artRef || artRef.type === 'media_item') return null
  return <CollectionDownloadMark kind={artRef.type} itemKey={artRef.key} subject={subject} />
}
