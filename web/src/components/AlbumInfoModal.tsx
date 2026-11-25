import { useState } from 'react'
import { api, ApiError } from '../api'
import { useAuth } from '../AuthContext'
import { listenStatsLine } from '../infoText'
import { useToast } from '../ToastContext'
import type { AlbumEdition, AlbumInfo } from '../types'
import { trackCountLabel } from '../plural'
import { usePendingAsync } from '../usePendingAsync'
import { FetchingMore } from './ErrorState'
import { ExternalLink, InfoChips, InfoFacts, InfoSection, InfoTechnical } from './InfoParts'
import { Modal } from './Modal'
import { SelectMenu } from './SelectMenu'

interface Props {
  artist: string | null
  album: string
  onClose: () => void
}

function editionLabel(edition: AlbumEdition): string {
  const parts = [
    edition.date,
    edition.country,
    edition.format,
    edition.track_count != null && trackCountLabel(edition.track_count),
    edition.disambiguation,
  ].filter(Boolean)
  const title = edition.title ?? 'Untitled'
  return parts.length ? `${title} (${parts.join(' · ')})` : title
}

/** The album "Info" popup: release type and dates, the matched edition's
 * label/catalog/barcode, ListenBrainz stats and tags. Admins can pick which
 * edition the album is. */
export function AlbumInfoModal({ artist, album, onClose }: Props) {
  const { user } = useAuth()
  const { showToast } = useToast()
  // What is stored shows at once; the rest fills in while the server fetches it.
  const state = usePendingAsync((signal) => api.albumInfo(artist, album, false, signal), [artist, album])
  const info: AlbumInfo | null = state.data ?? null
  const failed = state.status === 'error'
  const setInfo = state.setData
  const [busy, setBusy] = useState(false)

  async function refresh() {
    setBusy(true)
    try {
      setInfo(await api.albumInfo(artist, album, true))
    } catch {
      showToast(`Couldn't refresh info for "${album}".`, 'error')
    } finally {
      setBusy(false)
    }
  }

  async function chooseEdition(releaseMbid: string | null) {
    setBusy(true)
    try {
      setInfo(await api.setAlbumEdition(artist, album, releaseMbid))
      showToast('Album edition updated.', 'success')
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : 'Couldn\'t change the album edition.', 'error')
    } finally {
      setBusy(false)
    }
  }

  const stats = info && listenStatsLine(info.playcount, info.listeners)
  const type = info && (info.section || [info.primary_type, ...info.secondary_types].filter(Boolean).join(' + '))
  const labels = info?.labels.filter((label) => label.label || label.catalog_number) ?? []

  return (
    <Modal title={album} subtitle={artist ?? 'Unknown artist'} onClose={onClose} className="info-modal" history>
      {failed && !info ? (
        <p className="empty">Couldn't load info for this album.</p>
      ) : !info ? (
        <p className="loading">Loading…</p>
      ) : (
        <div className="info-body">
          {state.fetchingMore && <FetchingMore />}
          {stats && <p className="info-stats">{stats}</p>}
          <InfoFacts
            facts={[
              ['Type', type],
              ['Released', info.original_release_date],
              ['This edition', [info.edition_date, info.country].filter(Boolean).join(' · ')],
              [
                labels.length === 1 ? 'Label' : 'Labels',
                labels
                  .map((label) => [label.label, label.catalog_number].filter(Boolean).join(' · '))
                  .join(', '),
              ],
              ['Barcode', info.barcode],
              ['Status', info.status],
            ]}
          />
          {info.tags.length > 0 && (
            <InfoChips label="Tags" items={info.tags.map((tag) => <span key={tag} className="labels-chip">{tag}</span>)} />
          )}
          {user?.is_admin && info.editions.length > 0 && (
            <div className="info-edition-picker">
              <span id="album-info-edition-label">Edition</span>
              <SelectMenu
                label="Edition"
                labelledBy="album-info-edition-label"
                value={info.release_locked ? info.release_mbid ?? '' : ''}
                disabled={busy}
                options={[
                  { value: '', label: 'Automatic' },
                  ...info.editions.map((edition) => ({ value: edition.mbid, label: editionLabel(edition) })),
                ]}
                onChange={(mbid) => void chooseEdition(mbid || null)}
              />
            </div>
          )}
          {info.editions.length > 0 && (
            <InfoSection heading={`Editions (${info.editions.length})`}>
              <ul className="info-relations">
                {info.editions.map((edition) => (
                  <li key={edition.mbid} aria-current={edition.mbid === info.release_mbid ? 'true' : undefined}>
                    {editionLabel(edition)}
                    {edition.mbid === info.release_mbid && <span className="info-dim"> · this one</span>}
                  </li>
                ))}
              </ul>
            </InfoSection>
          )}
          {info.listenbrainz_url && (
            <p className="info-links-inline">
              <ExternalLink href={info.listenbrainz_url}>View on ListenBrainz</ExternalLink>
            </p>
          )}
          <InfoTechnical
            rows={[
              ['Release group MBID', info.release_group_mbid],
              ['Release MBID', info.release_mbid ? `${info.release_mbid}${info.release_locked ? ' (set by an admin)' : ''}` : null],
            ]}
          />
          <div className="info-actions">
            <button type="button" className="info-btn" onClick={() => void refresh()} disabled={busy}>
              Refresh
            </button>
          </div>
        </div>
      )}
    </Modal>
  )
}
