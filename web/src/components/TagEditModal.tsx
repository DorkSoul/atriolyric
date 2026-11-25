import { useActionState, useState } from 'react'
import { api, type BulkFailure } from '../api'
import type { MediaItem, TagEdit } from '../types'
import { Modal } from './Modal'
import { SubmitButton } from './SubmitButton'

/** The shared value of `key` across every item, or '' if they differ. */
function commonValue(items: MediaItem[], key: 'artist' | 'album' | 'genre'): string {
  const values = new Set(items.map((i) => i[key] ?? ''))
  return values.size === 1 ? [...values][0] : ''
}

interface SaveResult {
  error: string | null
  failures: BulkFailure[]
  /** Some files of a bulk edit were written, in this or an earlier attempt. */
  partlySaved: boolean
}

const NOTHING_SAVED: SaveResult = { error: null, failures: [], partlySaved: false }

interface Props {
  items: MediaItem[]
  onClose: () => void
  onSaved: () => void
  /** Some files were written and some were not: called when the dialog is
   * closed after that, with the ids that failed, so the rows that did change
   * can be refreshed. */
  onPartiallySaved?: (failedIds: number[]) => void
}

export function TagEditModal({ items, onClose, onSaved, onPartiallySaved }: Props) {
  const isBulk = items.length > 1
  // `items` can be empty only through a caller's stale selection; the form
  // below must not read a first item that is not there.
  const [title, setTitle] = useState(isBulk ? '' : (items[0]?.title ?? ''))
  const [artist, setArtist] = useState(commonValue(items, 'artist'))
  const [album, setAlbum] = useState(commonValue(items, 'album'))
  const [genre, setGenre] = useState(commonValue(items, 'genre'))
  const [touched, setTouched] = useState(new Set<string>())

  function touch(field: string) {
    setTouched((current) => new Set(current).add(field))
  }

  // The inputs are controlled, so what was typed stays after a failed save.
  const [result, save, saving] = useActionState(async (previous: SaveResult): Promise<SaveResult> => {
    const fields: TagEdit = {}
    if (!isBulk && touched.has('title')) fields.title = title.trim()
    if (touched.has('artist')) fields.artist = artist.trim()
    if (touched.has('album')) fields.album = album.trim()
    if (touched.has('genre')) fields.genre = genre.trim()

    try {
      if (isBulk) {
        const saved = await api.adminBulkEditTags(items.map((i) => i.id), fields)
        if (saved.failed.length > 0) {
          return {
            error: `${saved.failed.length} of ${items.length} couldn't be updated (read-only file?).`,
            failures: saved.failed,
            partlySaved: previous.partlySaved || saved.updated > 0,
          }
        }
      } else {
        await api.adminEditTags(items[0].id, fields)
      }
      onSaved()
      return { ...previous, error: null, failures: [] }
    } catch {
      return { ...previous, error: "Couldn't save - check the file is writable.", failures: [] }
    }
  }, NOTHING_SAVED)
  // A new attempt clears the last one's message and list.
  const error = saving ? null : result.error
  const failures = saving ? [] : result.failures

  if (items.length === 0) return null

  function close() {
    onClose()
    // Closing is when the rows that did change get refreshed.
    if (result.partlySaved) onPartiallySaved?.(result.failures.map((failure) => failure.id))
  }

  return (
    <Modal title={isBulk ? `Edit ${items.length} tracks` : 'Edit tags'} onClose={close} closeDisabled={saving} history>

      <form className="tag-edit-form" action={save}>
        {!isBulk && (
          <label>
            Title
            <input value={title} onChange={(e) => { setTitle(e.target.value); touch('title') }} />
          </label>
        )}
        <label>
          Artist
          <input
            value={artist}
            placeholder={isBulk ? '(mixed - leave blank to keep)' : ''}
            onChange={(e) => { setArtist(e.target.value); touch('artist') }}
          />
        </label>
        <label>
          Album
          <input
            value={album}
            placeholder={isBulk ? '(mixed - leave blank to keep)' : ''}
            onChange={(e) => { setAlbum(e.target.value); touch('album') }}
          />
        </label>
        <label>
          Genre
          <input
            value={genre}
            placeholder={isBulk ? '(mixed - leave blank to keep)' : ''}
            onChange={(e) => { setGenre(e.target.value); touch('genre') }}
          />
        </label>
        {error && <p className="login-error" role="alert">{error}</p>}
        {failures.length > 0 && (
          <ul className="tag-edit-failures" aria-label="Files that could not be updated">
            {failures.map((failure) => (
              <li key={failure.id}>
                {items.find((item) => item.id === failure.id)?.title ?? `Track ${failure.id}`}
                {failure.reason ? ` — ${failure.reason}` : ''}
              </li>
            ))}
          </ul>
        )}
        <SubmitButton pendingLabel="Saving…">Save</SubmitButton>
      </form>
    </Modal>
  )
}
