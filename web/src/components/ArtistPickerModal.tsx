import { useEffect, useState } from 'react'
import { api } from '../api'
import { artistRef } from '../artEntity'
import { ROLE_LABELS } from '../artistCredits'
import type { Artist, ArtistCredit } from '../types'
import { Art } from './Art'
import { closeModalThen } from '../modalHistory'
import { Modal } from './Modal'
import { MicIcon } from './Icons'

interface Props {
  credits: ArtistCredit[]
  onPick: (name: string) => void
  onClose: () => void
}

/** Lists the individual artists behind a multi-artist display string so one
 * can be opened. */
export function ArtistPickerModal({ credits, onPick, onClose }: Props) {
  const [summaries, setSummaries] = useState<Map<string, Artist>>(new Map())

  useEffect(() => {
    let cancelled = false
    api
      .artistSummaries(credits.map((credit) => credit.name))
      .then((list) => {
        if (!cancelled) setSummaries(new Map(list.map((artist) => [artist.artist, artist])))
      })
      .catch(() => {
        // Avatars are decorative; the names still work without them.
      })
    return () => {
      cancelled = true
    }
  }, [credits])

  return (
    <Modal title="Choose an artist" onClose={onClose} className="artist-picker" history>
      <ul className="artist-picker-list">
        {credits.map((credit) => {
          const summary = summaries.get(credit.name)
          return (
            <li key={credit.name}>
              <button
                type="button"
                className="artist-picker-row"
                // The picker's history entry goes first, so the artist's page
                // follows the page the picker was opened on.
                onClick={() => closeModalThen(onClose, () => onPick(credit.name))}
              >
                <Art
                  artRef={artistRef(summary)}
                  mediaItemId={summary?.representative_media_item_id ?? null}
                  className="art-sm"
                  fallback={<MicIcon />}
                />
                <span className="artist-picker-name">{credit.name}</span>
                <span className="artist-picker-role">{ROLE_LABELS[credit.role]}</span>
              </button>
            </li>
          )
        })}
      </ul>
    </Modal>
  )
}
