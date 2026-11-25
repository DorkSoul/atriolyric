import { useEffect, useState } from 'react'
import { chooseArtCandidate, effectiveArt, listArtCandidates, type ArtCandidate, type ArtRef } from '../artEntity'
import { useToast } from '../ToastContext'
import { Modal } from './Modal'
import './wantList.css'

const SOURCE_ORDER = ['Embedded', 'Folder', 'Cover Art Archive', 'Wikimedia Commons']
const CHOICE_KEY = 'atriolyric.artChoices'

/** The candidate last picked here for each piece of content, with the art
 * version that pick produced (kept on this device only: the server does not
 * say which candidate a custom image came from). */
function readChoices(): Record<string, { id: string; version: string }> {
  try {
    const parsed = JSON.parse(localStorage.getItem(CHOICE_KEY) ?? '{}')
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function rememberChoice(art: ArtRef, id: string) {
  try {
    const choices = readChoices()
    choices[`${art.type}:${art.key}`] = { id, version: String(effectiveArt(art).version) }
    localStorage.setItem(CHOICE_KEY, JSON.stringify(choices))
  } catch {
    // Storage unavailable: the picker just cannot mark the picture in use.
  }
}

/** The candidate whose picture is in use now, when that is known: a song's
 * own embedded picture while it has no custom art, or the custom image this
 * device picked (as long as the art has not changed since). */
function currentCandidateId(art: ArtRef): string | null {
  const { custom, version } = effectiveArt(art)
  if (!custom) return art.type === 'media_item' ? `embedded:${art.key}` : null
  const remembered = readChoices()[`${art.type}:${art.key}`]
  return remembered && remembered.version === String(version) ? remembered.id : null
}

/** "Choose from found artwork…": every cover the files, the Cover Art Archive and
 * Wikimedia Commons (artist photos) have for this song, album or artist. Picking one stores it as
 * custom art (Reset art undoes it). */
export function ArtPickerModal({ art, title, onClose }: { art: ArtRef; title: string; onClose: () => void }) {
  const { showToast } = useToast()
  const [candidates, setCandidates] = useState<ArtCandidate[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    listArtCandidates(art).then(
      (rows) => {
        if (!cancelled) setCandidates(rows)
      },
      (err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Could not load artwork.')
      },
    )
    return () => {
      cancelled = true
    }
    // The ref is rebuilt each render; the modal only ever opens for one.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function choose(candidate: ArtCandidate) {
    setSaving(candidate.id)
    try {
      await chooseArtCandidate(art, candidate.id)
      rememberChoice(art, candidate.id)
      showToast('Artwork updated.', 'success')
      onClose()
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Could not set the artwork.', 'error')
      setSaving(null)
    }
  }

  const groups = SOURCE_ORDER.map((source) => ({
    source,
    rows: (candidates ?? []).filter((c) => c.source === source),
  })).filter((group) => group.rows.length > 0)
  const currentId = currentCandidateId(art)
  const hasCurrent = groups.some((group) => group.rows.some((row) => row.id === currentId))

  return (
    <Modal title="Choose artwork" subtitle={title} onClose={onClose} className="art-picker" closeDisabled={saving != null} history>
      {error ? (
        <p className="empty">{error}</p>
      ) : candidates === null ? (
        <div className="want-loading" role="status">
          <span className="want-spinner" aria-hidden /> Looking for artwork…
        </div>
      ) : groups.length === 0 ? (
        <p className="empty">No artwork found for this. You can still upload your own with “Upload artwork…”.</p>
      ) : (
        groups.map((group) => (
          <section key={group.source} className="art-picker-group" aria-label={group.source}>
            <h3>{group.source}</h3>
            <div className="art-picker-grid">
              {group.rows.map((candidate) => (
                <button
                  key={candidate.id}
                  type="button"
                  className="art-picker-option"
                  onClick={() => void choose(candidate)}
                  disabled={saving != null}
                  aria-busy={saving === candidate.id}
                  aria-current={candidate.id === currentId ? 'true' : undefined}
                  title={candidate.id === currentId ? `${candidate.label} (in use now)` : candidate.label}
                >
                  <img src={candidate.thumb_url} alt={candidate.label} loading="lazy" />
                  <span>{candidate.label}</span>
                  {candidate.id === currentId && <span className="badge badge-accent art-picker-current">Current</span>}
                </button>
              ))}
            </div>
          </section>
        ))
      )}
      {hasCurrent && <p className="art-picker-note">The picture in use now is outlined. Pick another to switch.</p>}
    </Modal>
  )
}
