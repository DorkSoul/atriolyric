import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { useToast } from '../ToastContext'
import type { MixOption, MixPlaylist } from '../types'
import { trackCountLabel } from '../plural'
import { Modal } from './Modal'

/** What to tell the user when the new mix could not be filled with tracks
 * carrying every chosen genre and tag (the server then uses tracks carrying
 * only some of them), or null when it could. */
export function partialMatchNotice(mix: MixPlaylist | undefined): string | null {
  const full = mix?.full_match_count
  if (!mix || full == null || full >= mix.track_count) return null
  if (full === 0) {
    return 'No tracks match everything you chose, so this mix has tracks that match just some of it.'
  }
  return `Only ${trackCountLabel(full)} ${full === 1 ? 'matches' : 'match'} everything you chose, so the rest of the mix matches just some of it.`
}

/** The "Specify mix" dialog, shared by Home's "Mixes for you" rail and the
 * Playlists tab's Generated section. The option search, loading/error
 * handling and the markup all live here so both entry points behave
 * identically; only the trigger button (owned by each caller) and what
 * happens to the fresh mix list (onCreated) differ.
 *
 * Rendered only while open - the caller owns that boolean, so closing (the ✕,
 * a backdrop click) and finishing a mix both call onClose. */
export function SpecifyMixDialog({
  hint,
  onCreated,
  onClose,
}: {
  /** Copy under the title saying where the new mix will show up. */
  hint: string
  /** Receives the server's fresh mix list from the create response. Home
   * stores it as its rail's state and awaits its own onPlaylistsChanged (so a
   * just-created mix is in App's playlist list before playMix navigates);
   * Playlists reloads its list so the new mix card appears. Awaited before
   * the dialog closes. */
  onCreated: (mixes: MixPlaylist[]) => void | Promise<void>
  onClose: () => void
}) {
  const [mixOptions, setMixOptions] = useState<MixOption[]>([])
  const [mixOptionSearch, setMixOptionSearch] = useState('')
  // Tri-state per option: absent = neutral, 'include', 'exclude'.
  const [picks, setPicks] = useState<Record<string, 'include' | 'exclude'>>({})
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { showToast } = useToast()

  useEffect(() => {
    let cancelled = false
    api.listMixOptions().then(
      (options) => {
        if (!cancelled) setMixOptions(options)
      },
      (err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Could not load genres and tags.')
        }
      },
    )
    return () => {
      cancelled = true
    }
  }, [])

  const visibleMixOptions = useMemo(() => {
    const search = mixOptionSearch.trim().toLocaleLowerCase()
    return [...mixOptions]
      .sort((a, b) => b.track_count - a.track_count || a.name.localeCompare(b.name))
      .filter((option) => !search || option.name.toLocaleLowerCase().includes(search))
  }, [mixOptionSearch, mixOptions])

  const keyOf = (option: MixOption) => `${option.kind}:${option.name}`
  const includes = mixOptions.filter((option) => picks[keyOf(option)] === 'include')
  const excludes = mixOptions.filter((option) => picks[keyOf(option)] === 'exclude')

  function cycle(option: MixOption) {
    const key = keyOf(option)
    setPicks((current) => {
      const next = { ...current }
      if (!current[key]) next[key] = 'include'
      else if (current[key] === 'include') next[key] = 'exclude'
      else delete next[key]
      return next
    })
  }

  async function makeMix() {
    if (includes.length === 0) return
    setCreating(true)
    setError(null)
    try {
      const pick = ({ kind, name }: MixOption) => ({ kind, name })
      const list = await api.addSpecifiedMix({ include: includes.map(pick), exclude: excludes.map(pick) })
      await onCreated(list)
      onClose()
      // Only the mix just made carries full_match_count.
      const notice = partialMatchNotice(list.find((mix) => mix.full_match_count != null))
      if (notice) showToast(notice, 'warning', 'specified-mix-partial')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create that mix.')
    } finally {
      setCreating(false)
    }
  }

  return (
    <Modal title="Specify mix" onClose={onClose} closeDisabled={creating} className="specify-mix-dialog" history>
      <p className="settings-hint">{hint}</p>
      <p className="settings-hint">Tap once to include a genre or tag, twice to exclude it, a third time to clear it.</p>
      {error && <p className="form-error" role="alert">{error}</p>}
      <input
        type="search"
        className="specify-mix-search"
        placeholder="Search genres and tags…"
        aria-label="Search genres and tags"
        autoComplete="off"
        value={mixOptionSearch}
        onChange={(event) => setMixOptionSearch(event.target.value)}
        disabled={creating || mixOptions.length === 0}
      />
      {mixOptions.length > 0 ? (
        visibleMixOptions.length > 0 ? (
          <div className="specify-mix-options">
            {visibleMixOptions.map((option) => {
              const state = picks[keyOf(option)]
              const label = state === 'include' ? 'included' : state === 'exclude' ? 'excluded' : 'not selected'
              return (
                <button
                  key={keyOf(option)}
                  type="button"
                  className={state ? `is-${state}` : undefined}
                  aria-pressed={state === 'include' ? true : state === 'exclude' ? 'mixed' : false}
                  aria-label={`${option.kind === 'genre' ? 'Genre' : 'Tag'} ${option.name}, ${trackCountLabel(option.track_count)}, ${label}`}
                  onClick={() => cycle(option)}
                  disabled={creating}
                >
                  <span>{option.name}</span>
                  <small>
                    <span className="mix-detail-full">{option.kind === 'genre' ? 'Genre' : 'Tag'} · {trackCountLabel(option.track_count)}</span>
                    <span className="mix-detail-count" aria-hidden="true">{option.track_count}</span>
                  </small>
                </button>
              )
            })}
          </div>
        ) : <p className="specify-mix-empty">No genres or tags match “{mixOptionSearch}”.</p>
      ) : !error ? <p>Loading genres and tags…</p> : null}
      <div className="specify-mix-footer">
        <button type="button" className="primary" onClick={makeMix} disabled={creating || includes.length === 0}>
          {creating ? 'Making mix…' : 'Make mix'}
        </button>
      </div>
    </Modal>
  )
}
