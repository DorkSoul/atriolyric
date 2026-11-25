import { useState } from 'react'
import { api, ApiError } from '../api'
import { useAuth } from '../AuthContext'
import { listenStatsLine, yearSpan } from '../infoText'
import { useToast } from '../ToastContext'
import type { ArtistInfo, ArtistMatchCandidate, ArtistRelation } from '../types'
import { ExternalLink, ImageCredit, InfoChips, InfoFacts, InfoProse, InfoSection, InfoTechnical } from './InfoParts'
import { closeModalThen } from '../modalHistory'
import { Modal } from './Modal'
import { WantButton } from './Catalog'
import { wantApi } from '../wantList'
import { usePendingAsync } from '../usePendingAsync'
import { FetchingMore } from './ErrorState'

interface Props {
  name: string
  onClose: () => void
  /** Opens another artist's page (members, similar artists in the library). */
  onOpenArtist?: (name: string) => void
}

function RelationList({
  relations,
  onOpen,
}: {
  relations: ArtistRelation[]
  onOpen?: (name: string) => void
}) {
  return (
    <ul className="info-relations">
      {relations.map((relation) => {
        const span = yearSpan(relation.begin, relation.end, relation.ended)
        const detail = [relation.attributes.join(', '), span].filter(Boolean).join(' · ')
        return (
          <li key={`${relation.mbid ?? ''}:${relation.name}`}>
            {relation.in_library && onOpen ? (
              <button type="button" className="info-link-btn" onClick={() => onOpen(relation.name)}>
                {relation.name}
              </button>
            ) : (
              <span>{relation.name}</span>
            )}
            {detail && <span className="info-dim"> · {detail}</span>}
          </li>
        )
      })}
    </ul>
  )
}

/** The artist "Info" popup: ListenBrainz stats, Wikipedia bio, MusicBrainz facts,
 * members and links. Admins can fix which MusicBrainz artist it's matched to. */
export function ArtistInfoModal({ name, onClose, onOpenArtist }: Props) {
  const { user } = useAuth()
  const { showToast } = useToast()
  // What is stored shows at once; the rest fills in while the server fetches it.
  const state = usePendingAsync((signal) => api.artistInfo(name, false, signal), [name])
  const info: ArtistInfo | null = state.data ?? null
  const failed = state.status === 'error'
  const setInfo = state.setData
  const [refreshing, setRefreshing] = useState(false)
  const [candidates, setCandidates] = useState<ArtistMatchCandidate[] | null>(null)
  const [fixing, setFixing] = useState(false)
  const [saving, setSaving] = useState(false)

  async function refresh() {
    setRefreshing(true)
    try {
      setInfo(await api.artistInfo(name, true))
    } catch {
      showToast(`Couldn't refresh info for "${name}".`, 'error')
    } finally {
      setRefreshing(false)
    }
  }

  async function startFix() {
    setFixing(true)
    setCandidates(null)
    try {
      setCandidates(await api.artistMatchCandidates(name))
    } catch {
      showToast('Couldn\'t search MusicBrainz for matches.', 'error')
      setFixing(false)
    }
  }

  async function chooseMatch(mbid: string | null) {
    setSaving(true)
    try {
      setInfo(await api.setArtistMatch(name, mbid))
      setFixing(false)
      setCandidates(null)
      showToast(mbid ? 'Artist match updated.' : 'Artist match cleared.', 'success')
    } catch (err) {
      showToast(err instanceof ApiError ? err.message : 'Couldn\'t change the artist match.', 'error')
    } finally {
      setSaving(false)
    }
  }

  // The dialog's history entry goes first, so the artist's page follows the
  // page the dialog was opened on.
  const openArtist = onOpenArtist && ((artist: string) => closeModalThen(onClose, () => onOpenArtist(artist)))

  const stats = info && listenStatsLine(info.playcount, info.listeners)
  const span = info && yearSpan(info.begin, info.end, info.ended)
  const person = info?.type === 'Person'

  return (
    <Modal
      title={info?.name ?? name}
      subtitle={info?.disambiguation || undefined}
      onClose={onClose}
      className="info-modal"
      history
    >
      {failed && !info ? (
        <p className="empty">Couldn't load info for this artist.</p>
      ) : !info ? (
        <p className="loading">Loading…</p>
      ) : (
        <div className="info-body">
          {state.fetchingMore && <FetchingMore />}
          {stats && <p className="info-stats">{stats}</p>}
          <InfoFacts
            facts={[
              ['Type', [info.type, person && info.gender].filter(Boolean).join(' · ')],
              ['From', [info.area, info.country && info.country !== info.area ? info.country : null].filter(Boolean).join(', ')],
              [person ? 'Born in' : 'Formed in', info.begin_area],
              [person ? 'Life' : 'Active', span],
            ]}
          />
          {info.bio && (
            <InfoSection heading="Biography">
              <InfoProse text={info.bio.text} html={info.bio.html} source={info.bio.source} url={info.bio.url} />
            </InfoSection>
          )}
          {info.tags.length > 0 && (
            <InfoChips label="Tags" items={info.tags.map((tag) => <span key={tag} className="labels-chip">{tag}</span>)} />
          )}
          {info.members.length > 0 && (
            <InfoSection heading="Members">
              <RelationList relations={info.members} onOpen={openArtist} />
            </InfoSection>
          )}
          {info.member_of.length > 0 && (
            <InfoSection heading="Member of">
              <RelationList relations={info.member_of} onOpen={openArtist} />
            </InfoSection>
          )}
          {info.similar.length > 0 && (
            <InfoSection heading="Similar artists">
              {/* Artists in the library open their page; the rest are plain
                  chips, each its own element so per-artist actions can be
                  added beside them later. */}
              <InfoChips
                label="Similar artists"
                items={info.similar.map((artist) =>
                  artist.in_library && openArtist ? (
                    <button key={artist.name} type="button" className="labels-chip" onClick={() => openArtist(artist.name)}>
                      {artist.name}
                    </button>
                  ) : (
                    <span key={artist.name} className="info-chip-want">
                      <span className="labels-chip info-chip-missing">{artist.name}</span>
                      <WantButton compact wanted={false} onAdd={() => wantApi.add({ kind: 'artist', artist: artist.name })} />
                    </span>
                  ),
                )}
              />
            </InfoSection>
          )}
          {(info.links.length > 0 || info.listenbrainz_url) && (
            <InfoSection heading="Links">
              <ul className="info-links">
                {info.listenbrainz_url && (
                  <li>
                    <ExternalLink href={info.listenbrainz_url}>ListenBrainz</ExternalLink>
                  </li>
                )}
                {info.links.map((link) => (
                  <li key={link.url}>
                    <ExternalLink href={link.url}>{link.label || link.type}</ExternalLink>
                  </li>
                ))}
              </ul>
            </InfoSection>
          )}
          <InfoTechnical
            rows={[
              ['MusicBrainz ID', info.mbid ? `${info.mbid}${info.mbid_locked ? ' (set by an admin)' : ''}` : null],
              ['Sources', info.sources.join(', ')],
            ]}
          />
          {info.image_credit && <ImageCredit credit={info.image_credit} />}

          {fixing && (
            <InfoSection heading="Choose the right artist">
              {!candidates ? (
                <p className="loading">Searching MusicBrainz…</p>
              ) : candidates.length === 0 ? (
                <p className="empty">No MusicBrainz artists found.</p>
              ) : (
                <ul className="info-candidates">
                  {candidates.map((candidate) => (
                    <li key={candidate.mbid}>
                      <button
                        type="button"
                        className="info-candidate"
                        disabled={saving}
                        aria-current={candidate.mbid === info.mbid ? 'true' : undefined}
                        onClick={() => void chooseMatch(candidate.mbid)}
                      >
                        <span className="info-candidate-name">{candidate.name}</span>
                        <span className="info-dim">
                          {[
                            candidate.disambiguation,
                            candidate.type,
                            candidate.country,
                            candidate.begin?.slice(0, 4),
                            candidate.score != null && `${candidate.score}% match`,
                          ]
                            .filter(Boolean)
                            .join(' · ')}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <div className="info-actions">
                {info.mbid && (
                  <button type="button" className="info-btn" disabled={saving} onClick={() => void chooseMatch(null)}>
                    Clear match
                  </button>
                )}
                <button type="button" className="info-btn" disabled={saving} onClick={() => setFixing(false)}>
                  Cancel
                </button>
              </div>
            </InfoSection>
          )}

          <div className="info-actions">
            <button type="button" className="info-btn" onClick={() => void refresh()} disabled={refreshing}>
              {refreshing ? 'Refreshing…' : 'Refresh'}
            </button>
            {user?.is_admin && !fixing && (
              <button type="button" className="info-btn" onClick={() => void startFix()}>
                Wrong artist? Fix match
              </button>
            )}
          </div>
        </div>
      )}
    </Modal>
  )
}
