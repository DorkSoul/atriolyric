import type { ReactNode } from 'react'
import { effectiveArt, useArtRevision, type ArtRef } from '../artEntity'
import { Art } from './Art'
import { BrowseDetailHeader } from './BrowseDetailHeader'
import { MixCollage } from './MixCollage'
import { ImageCredit } from './InfoParts'
import { useArtCredit } from '../artCredit'
import { RefDownloadMark } from '../native/DownloadMark'

interface Props {
  backLabel: string
  onBack: () => void
  /** The back row's scrolling name, and the heading unless `heading` is set. */
  title: string
  /** A shorter heading beside the art (e.g. an album without its artist,
   * which the subtitle then links). */
  heading?: string
  /** The small word above the heading: "Album", "Artist", "Playlist". */
  eyebrow?: string
  subtitle: ReactNode
  /** Free text shown under the subtitle (a playlist's description). */
  description?: string | null
  /** Short facts shown as small pills under the subtitle ("Released 2021",
   * "15 songs"). Empty entries are skipped. */
  facts?: (string | null | undefined | false)[]
  representativeMediaItemId?: number
  collageMediaItemIds?: number[]
  /** Custom-art identity; a custom image replaces the collage/representative art. */
  artRef?: ArtRef
  fallback: ReactNode
  /** Controls shown under the title: Play / Shuffle, the heart, Info, more. */
  actions?: ReactNode
  /** Round artwork (an artist). */
  round?: boolean
}

/** The hero at the top of an album, artist or playlist page: large artwork
 * (as large as Now Playing's on a phone), the name, facts and the page's
 * actions. */
export function CollectionDetailHeader({
  backLabel,
  onBack,
  title,
  heading,
  eyebrow,
  subtitle,
  description,
  facts,
  representativeMediaItemId,
  collageMediaItemIds,
  fallback,
  actions,
  artRef,
  round,
}: Props) {
  useArtRevision()
  const hasCustom = artRef ? effectiveArt(artRef).custom : false
  const credit = useArtCredit(artRef)
  const shownFacts = (facts ?? []).filter((fact): fact is string => Boolean(fact))
  return (
    <div className={round ? 'collection-detail collection-detail-round' : 'collection-detail'}>
      <BrowseDetailHeader backLabel={backLabel} onBack={onBack} title={title} />
      <div className="collection-detail-info">
        {!hasCustom && collageMediaItemIds && collageMediaItemIds.length > 0 ? (
          <div className="collection-detail-art collection-detail-collage">
            <MixCollage mediaItemIds={collageMediaItemIds} />
          </div>
        ) : (
          <Art
            mediaItemId={representativeMediaItemId ?? null}
            artRef={artRef}
            className="collection-detail-art"
            fallback={fallback}
            full
          />
        )}
        <div className="collection-detail-copy">
          {eyebrow && <span className="collection-detail-eyebrow">{eyebrow}</span>}
          <h2>{heading ?? title} <RefDownloadMark artRef={artRef} subject={heading ?? title} /></h2>
          <p className="collection-detail-subtitle">{subtitle}</p>
          {description && <p className="collection-detail-description">{description}</p>}
          {credit && <ImageCredit credit={credit} />}
          {shownFacts.length > 0 && (
            <ul className="collection-detail-facts" aria-label="Facts">
              {shownFacts.map((fact) => (
                <li key={fact} className="collection-detail-fact">
                  {fact}
                </li>
              ))}
            </ul>
          )}
          {actions && <div className="collection-detail-actions">{actions}</div>}
        </div>
      </div>
    </div>
  )
}
