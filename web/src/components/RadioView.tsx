import { useState } from 'react'
import { api } from '../api'
import { errorMessage } from '../errorMessage'
import { useToast } from '../ToastContext'
import { KEPT_TAB_STALE_MS, useAsync } from '../useAsync'
import { useAuth } from '../AuthContext'
import { usePlayerActions } from '../PlayerContext'
import { usePlayerSelector } from '../playerSelectors'
import type { RadioStation } from '../types'
import { ErrorState, LoadingState } from './ErrorState'
import { HeartIcon, PlayIcon } from './Icons'
import { RadioIcon, TrashIcon } from './BrowseIcons'
import { HelpTip } from './HelpTip'
import { TopBarTitlePortal } from '../headerControlsPortal'
import { useConfirm } from '../dialogs'
import { useOptimisticChange } from '../useOptimisticChange'
import { Switch } from './Switch'
import { SelectMenu } from './SelectMenu'

const NO_STATIONS: RadioStation[] = []

type SortMode = 'name-asc' | 'name-desc' | 'recent'

function sortStations(stations: RadioStation[], mode: SortMode): RadioStation[] {
  const sorted = [...stations]
  if (mode === 'name-asc') sorted.sort((a, b) => a.name.localeCompare(b.name))
  else if (mode === 'name-desc') sorted.sort((a, b) => b.name.localeCompare(a.name))
  else sorted.sort((a, b) => b.created_at.localeCompare(a.created_at))
  return sorted
}

function groupByProvider(stations: RadioStation[]): [string, RadioStation[]][] {
  const groups = new Map<string, RadioStation[]>()
  for (const s of stations) {
    const key = s.provider ?? ''
    const group = groups.get(key)
    if (group) group.push(s)
    else groups.set(key, [s])
  }
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b))
}

interface StationRowProps {
  station: RadioStation
  isPlaying: boolean
  canDelete: boolean
  onPlay: (station: RadioStation) => void
  onDelete: (id: number) => void
  /** Saves the favourite, then updates the station list. */
  onSetFavorite: (station: RadioStation, favorite: boolean) => Promise<void>
}

function StationRow({ station, isPlaying, canDelete, onPlay, onDelete, onSetFavorite }: StationRowProps) {
  // The heart turns at once; a refused save turns it back by itself.
  const favorite = useOptimisticChange(station.is_favorite, (next) => onSetFavorite(station, next))
  return (
    <li className={isPlaying ? 'radio-station-row radio-station-playing' : 'radio-station-row'}>
      <button className="radio-station-btn" onClick={() => onPlay(station)}>
        <span className="radio-station-icon" aria-hidden="true">
          {isPlaying ? <PlayIcon /> : <RadioIcon />}
        </span>
        <span className="radio-station-text">
          <span className="radio-station-name">
            {isPlaying && <span className="visually-hidden">Playing: </span>}
            <span className="radio-station-name-text">{station.name}</span>
            {!station.is_shared && <span className="badge">private</span>}
          </span>
          {station.description && (
            <span className="radio-station-desc">{station.description}</span>
          )}
        </span>
      </button>
      <div className="radio-station-actions">
        <button
          className={`favorite-btn ${favorite.shown ? 'favorite-active' : ''}`}
          onClick={() => favorite.change(!favorite.shown)}
          disabled={favorite.busy}
          aria-label={favorite.shown ? 'Unfavourite' : 'Favourite'}
        >
          <HeartIcon filled={favorite.shown} />
        </button>
        {canDelete && (
          <button
            className="radio-station-remove"
            onClick={() => onDelete(station.id)}
            aria-label={`Remove ${station.name}`}
            title="Remove"
          >
            <TrashIcon />
          </button>
        )}
      </div>
    </li>
  )
}

export function RadioView() {
  const { user } = useAuth()
  const playingStationId = usePlayerSelector((player) => player.radioStation?.id ?? null)
  const { playRadioStation } = usePlayerActions()
  const confirm = useConfirm()
  const { showToast } = useToast()
  // A kept-mounted tab: shown again soon after, it keeps the list it has.
  const stationsState = useAsync(() => api.listStations(), [], { staleMs: KEPT_TAB_STALE_MS })
  const stations = stationsState.data ?? NO_STATIONS
  const [addError, setAddError] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [newName, setNewName] = useState('')
  const [newUrl, setNewUrl] = useState('')
  const [newDescription, setNewDescription] = useState('')
  const [newShared, setNewShared] = useState(false)
  const [sortMode, setSortMode] = useState<SortMode>('name-asc')

  function handlePlayStation(station: RadioStation) {
    playRadioStation(station)
    api
      .recordPlaySource({ kind: 'radio', label: station.name, payload: { station_id: station.id } })
      .catch(() => {})
  }

  async function createStation() {
    if (!newName.trim() || !newUrl.trim() || adding) return
    setAdding(true)
    setAddError(null)
    try {
      await api.createStation(newName.trim(), newUrl.trim(), newShared, newDescription.trim())
      setNewName('')
      setNewUrl('')
      setNewDescription('')
      setNewShared(false)
      stationsState.reload()
    } catch (error) {
      // The server says what is wrong with the stream (a bad URL, a duplicate).
      setAddError(errorMessage(error, 'Could not add that station.'))
    } finally {
      setAdding(false)
    }
  }

  async function removeStation(id: number, name: string) {
    if (!(await confirm({
      title: 'Remove radio station',
      body: `Remove radio station "${name}"?`,
      confirmLabel: 'Remove',
      danger: true,
    }))) return
    try {
      await api.deleteStation(id)
      stationsState.reload()
    } catch (error) {
      showToast(errorMessage(error, `Could not remove "${name}".`), 'error')
    }
  }

  async function setFavorite(station: RadioStation, favorite: boolean) {
    try {
      await api.setStationFavorite(station.id, favorite)
    } catch (error) {
      // The heart turns back by itself (useOptimisticChange); say why.
      showToast(
        errorMessage(error, `Could not ${favorite ? 'add' : 'remove'} "${station.name}" ${favorite ? 'to' : 'from'} your favourites.`),
        'error',
      )
      return
    }
    stationsState.setData((prev) => (prev ?? []).map((s) => (s.id === station.id ? { ...s, is_favorite: favorite } : s)))
  }

  function canDelete(station: RadioStation): boolean {
    return !!user?.is_admin || station.owner_user_id === user?.id
  }

  const curated = stations.filter((s) => s.provider != null)
  const own = sortStations(stations.filter((s) => s.provider == null), sortMode)
  const curatedGroups = groupByProvider(curated)

  return (
    <div className="radio-view">
      {/* The page title is the top bar's; its help tip sits beside it. */}
      <TopBarTitlePortal>
        <HelpTip
          label="About radio"
          text="Add internet radio streams by name and URL, then play or favourite stations. While a station is playing, save songs you like to your Want list. Admins can share stations with everyone."
          className="section-help-tip"
          buttonClassName="home-rail-help-button"
          bubbleClassName="section-help-bubble"
          align="start"
        />
      </TopBarTitlePortal>
      <div className="page-toolbar radio-sort-row">
        <div className="radio-sort-label">
          <span className="page-toolbar-label" id="radio-sort-label">Sort</span>
          <SelectMenu<SortMode>
            label="Sort"
            labelledBy="radio-sort-label"
            value={sortMode}
            options={[
              { value: 'name-asc', label: 'Name (A–Z)' },
              { value: 'name-desc', label: 'Name (Z–A)' },
              { value: 'recent', label: 'Recently added' },
            ]}
            onChange={setSortMode}
          />
        </div>
      </div>

      <section className="radio-section" aria-labelledby="radio-own-heading">
        <div className="sec-head">
          <h3 id="radio-own-heading">Your stations</h3>
        </div>
        {stationsState.data === undefined ? (
          stationsState.status === 'error' ? (
            <ErrorState message={errorMessage(stationsState.error, 'Could not load the radio stations.')} onRetry={stationsState.retry} />
          ) : (
            <LoadingState />
          )
        ) : own.length === 0 ? (
          <p className="empty">No stations added yet.</p>
        ) : (
          <ul className="radio-station-list radio-tiles radio-tiles-own">
            {own.map((s) => (
              <StationRow
                key={s.id}
                station={s}
                isPlaying={playingStationId === s.id}
                canDelete={canDelete(s)}
                onPlay={handlePlayStation}
                onDelete={(id) => removeStation(id, s.name)}
                onSetFavorite={setFavorite}
              />
            ))}
          </ul>
        )}
      </section>

      <section className="radio-section" aria-labelledby="radio-add-heading">
        <div className="sec-head">
          <h3 id="radio-add-heading">Add a station</h3>
        </div>
        <div className="radio-add-row">
          <input
            aria-label="Station name"
            placeholder="Station name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            autoComplete="off"
          />
          <input
            aria-label="Stream URL"
            placeholder="Stream URL"
            value={newUrl}
            onChange={(e) => setNewUrl(e.target.value)}
            autoComplete="off"
          />
          <input
            aria-label="Description (optional)"
            placeholder="Description (optional)"
            value={newDescription}
            onChange={(e) => setNewDescription(e.target.value)}
            autoComplete="off"
          />
          {user?.is_admin && (
            <Switch className="radio-shared-check" checked={newShared} onChange={setNewShared}>
              Shared
              <span className="badge badge-accent">admin</span>
            </Switch>
          )}
          <button className="btn btn-primary" onClick={createStation} disabled={adding}>
            {adding ? 'Adding…' : 'Add station'}
          </button>
        </div>
        {addError && <p className="login-error" role="alert">{addError}</p>}
      </section>

      {curatedGroups.length > 0 && (
        <section className="radio-section" aria-labelledby="radio-free-heading">
          <div className="sec-head">
            <h3 id="radio-free-heading">Free stations</h3>
          </div>
          <div className="radio-provider-groups">
            {curatedGroups.map(([provider, group]) => (
              <details key={provider} className="radio-provider-group" open={curatedGroups.length === 1}>
                <summary>
                  {provider} <span className="text-dim">({group.length})</span>
                </summary>
                <ul className="radio-station-list radio-tiles">
                  {sortStations(group, sortMode).map((s) => (
                    <StationRow
                      key={s.id}
                      station={s}
                      isPlaying={playingStationId === s.id}
                      canDelete={canDelete(s)}
                      onPlay={handlePlayStation}
                      onDelete={(id) => removeStation(id, s.name)}
                      onSetFavorite={setFavorite}
                    />
                  ))}
                </ul>
              </details>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}
