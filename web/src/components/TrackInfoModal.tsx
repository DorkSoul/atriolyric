import { useState } from 'react'
import { api } from '../api'
import { formatCompact, listenStatsLine } from '../infoText'
import { formatDuration } from '../formatDuration'
import { useToast } from '../ToastContext'
import type { MediaItem, TrackInfo } from '../types'
import { usePendingAsync } from '../usePendingAsync'
import { FetchingMore } from './ErrorState'
import { ExternalLink, InfoChips, InfoFacts, InfoSection, InfoTechnical } from './InfoParts'
import { Modal } from './Modal'

/** Credits in the order roles first appear, names joined per role. */
function groupCredits(credits: TrackInfo['credits']): [string, string][] {
  const groups = new Map<string, string[]>()
  for (const credit of credits) {
    const names = groups.get(credit.role) ?? []
    names.push(credit.detail ? `${credit.name} (${credit.detail})` : credit.name)
    groups.set(credit.role, names)
  }
  return [...groups].map(([role, names]) => [role.charAt(0).toUpperCase() + role.slice(1), names.join(', ')])
}

/** The song "Info" popup: ListenBrainz stats and your plays, tags, credits,
 * works and release dates. */
export function TrackInfoModal({ track, onClose }: { track: MediaItem; onClose: () => void }) {
  const { showToast } = useToast()
  // What is stored shows at once; the rest fills in while the server fetches it.
  const state = usePendingAsync((signal) => api.trackInfo(track.id, false, { signal }), [track.id])
  const info = state.data ?? null
  const failed = state.status === 'error'
  const [refreshing, setRefreshing] = useState(false)

  async function refresh() {
    setRefreshing(true)
    try {
      state.setData(await api.trackInfo(track.id, true))
    } catch {
      showToast(`Couldn't refresh info for "${track.title}".`, 'error')
    } finally {
      setRefreshing(false)
    }
  }

  const stats = info && listenStatsLine(info.playcount, info.listeners)
  const yourPlays = info && info.library_playcount != null ? `${formatCompact(info.library_playcount)} here` : null
  const credits = info ? groupCredits(info.credits) : []

  return (
    <Modal
      title={info?.title ?? track.title}
      subtitle={[info?.artist ?? track.artist, info?.album ?? track.album].filter(Boolean).join(' · ') || undefined}
      onClose={onClose}
      className="info-modal"
      history
    >
      {failed && !info ? (
        <p className="empty">Couldn't load info for this song.</p>
      ) : !info ? (
        <p className="loading">Loading…</p>
      ) : (
        <div className="info-body">
          {state.fetchingMore && <FetchingMore />}
          {stats && <p className="info-stats">{stats}</p>}
          <InfoFacts
            facts={[
              ['Your plays', yourPlays],
              ['First released', info.original_release_date],
              ['This release', info.release_date !== info.original_release_date && info.release_date],
              ['Format', track.format_label],
              ['Length', info.duration_seconds == null ? null : formatDuration(Math.round(info.duration_seconds))],
              ['ISRC', info.isrc],
            ]}
          />
          {info.tags.length > 0 && (
            <InfoChips label="Tags" items={info.tags.map((tag) => <span key={tag} className="labels-chip">{tag}</span>)} />
          )}
          {credits.length > 0 && (
            <InfoSection heading="Credits">
              <InfoFacts facts={credits} />
            </InfoSection>
          )}
          {info.works.length > 0 && (
            <InfoSection heading={info.works.length === 1 ? 'Work' : 'Works'}>
              <ul className="info-relations">
                {info.works.map((work) => (
                  <li key={work}>{work}</li>
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
              ['Recording MBID', info.recording_mbid],
              ['Artist MBID', info.artist_mbid],
              ['Release MBID', info.release_mbid],
              ['Release group MBID', info.release_group_mbid],
            ]}
          />
          <div className="info-actions">
            <button type="button" className="info-btn" onClick={() => void refresh()} disabled={refreshing}>
              {refreshing ? 'Refreshing…' : 'Refresh'}
            </button>
          </div>
        </div>
      )}
    </Modal>
  )
}
