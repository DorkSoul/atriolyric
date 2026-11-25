import { useActionState, useEffect, useState, type ReactNode } from 'react'
import { api, ApiError } from '../api'
import { useAuth } from '../AuthContext'
import type { AdminUser, LibraryRoot, LibraryTask, ScanMode } from '../types'
import { PasswordInput } from './PasswordInput'
import { useConfirm } from '../dialogs'
import { AdminHelpTip as HelpTip } from './AdminHelpTip'
import { ActivityLog } from './ActivityLog'
import { TopBarTitlePortal } from '../headerControlsPortal'
import { SubmitButton } from './SubmitButton'
import { BackgroundWorkSection } from './BackgroundWorkSection'
import { TrashIcon } from './BrowseIcons'
import { ErrorState, LoadingState } from './ErrorState'
import { errorMessage } from '../errorMessage'
import { useAsync } from '../useAsync'
import { Switch } from './Switch'

function formatDuration(seconds: number | null): string | null {
  if (seconds == null) return null
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}

// A rescan has no "matched" count (that is for the metadata steps), so a full
// setup's rescan step is counted like a plain scan: every file processed.
/** A Last.fm tags run (one can still be in a root's history). */
function isOldLastfmTask(task: LibraryTask): boolean {
  const kind: string = task.kind
  return kind === 'lf_metadata' || kind === 'lf_metadata_all'
}

function isFullSetup(task: LibraryTask): boolean {
  return task.kind === 'full_setup' || task.kind === 'full_setup_new'
}

function isScanStage(task: LibraryTask): boolean {
  return task.kind === 'scan' || (isFullSetup(task) && /(Full rescan|Scan)$/.test(task.stage ?? ''))
}

function taskSuccesses(task: LibraryTask): number {
  return isScanStage(task) ? task.processed : task.matched
}

function taskFailures(task: LibraryTask): number {
  return isScanStage(task) ? 0 : Math.max(0, task.processed - task.matched)
}

const TASK_POLL_MS = 1500
const IDLE_POLL_MS = 30_000

function TaskProgress({ task }: { task: LibraryTask }) {
  const label = task.kind === 'scan'
    ? 'Scanning library'
    : task.kind === 'mb_isrc'
      ? 'Resolving MusicBrainz IDs'
      : task.kind === 'mb_mbid'
        ? 'Resolving MusicBrainz IDs'
      : task.kind === 'lb_tags_all'
        ? 'Refreshing all ListenBrainz tags'
      : task.kind === 'lb_tags'
        ? 'Fetching missing ListenBrainz tags'
      : task.kind === 'enrich'
        ? 'Using MusicBrainz fallback'
      : task.kind === 'cover_art'
        ? 'Fetching album art'
      : task.kind === 'artist_info'
        ? 'Fetching artist info'
      : isFullSetup(task)
        ? `${task.kind === 'full_setup_new' ? 'Setup for new entries' : 'Full setup scan'}${task.stage ? ` - step ${task.stage}` : ''}`
        : 'Fetching metadata'
  const percent = task.total > 0 ? Math.round((task.processed / task.total) * 100) : 0
  const eta = formatDuration(task.eta_seconds)
  const speed = task.items_per_second && task.items_per_second > 0
    ? `${task.items_per_second.toFixed(task.items_per_second < 10 ? 1 : 0)} tracks/s`
    : null
  const progressLabel = task.status === 'stopping' ? 'Stopping ' + label.toLowerCase() : label
  const successes = taskSuccesses(task)
  const failures = taskFailures(task)
  // A screen reader hears the task when it starts, when its stage or status
  // changes and at each 10% - not on every poll, which re-read the whole block
  // every 1.5 s. Derived while rendering so the announcement is in this render.
  const announceKey = `${task.id}:${task.status}:${task.stage ?? ''}:${Math.floor(percent / 10)}`
  const [announced, setAnnounced] = useState({ key: announceKey, text: `${progressLabel}: ${percent}%` })
  if (announced.key !== announceKey) setAnnounced({ key: announceKey, text: `${progressLabel}: ${percent}%` })
  return (
    <div className="admin-task-progress">
      <div className="visually-hidden" role="status" aria-live="polite">{announced.text}</div>
      <div>{task.total > 0 ? `${progressLabel}: ${task.processed.toLocaleString()} / ${task.total.toLocaleString()} (${percent}%)` : `${progressLabel}: preparing…`}</div>
      <progress value={task.processed} max={Math.max(task.total, 1)} />
      <small>Successes: {successes.toLocaleString()} · Failures: {failures.toLocaleString()}</small>
      <small>{[speed, eta && `ETA ${eta}`].filter(Boolean).join(' · ') || 'Estimating speed…'}</small>
    </div>
  )
}

/** The text behind each scan button's info control. */
const SCAN_HELP = {
  normal: 'Finds new, changed, or removed media.',
  metadata: 'Re-reads the tags and embedded lyrics of every file.',
  full: 'Rescan metadata, and also rebuilds artwork and thumbnails.',
}
const SETUP_NEW_HELP = 'Use this after adding an artist or album. Scans for new and changed files, then does the full setup steps only for what is still missing: MusicBrainz IDs and metadata, ListenBrainz artist tags, cover art and artist info. Tracks that already have their data are left alone, so it is much faster than a full setup scan.'
const FULL_SETUP_HELP = 'Rescans every file, then refetches everything: MusicBrainz IDs and metadata for every track, ListenBrainz artist tags for every track, missing cover art and artist info. Use it when first setting up a library, or when something has gone wrong and you want to rebuild it all. It can take hours on a large library (MusicBrainz allows about one request a second); you can stop it at any point.'

const SINGLE_STEP_TASKS: LibraryTask['kind'][] = ['mb_mbid', 'mb_isrc', 'enrich', 'lb_tags', 'lb_tags_all', 'artist_info']

/** The collapsed "Run a single step instead" group. A running single step
 * opens it, so its Stop button is never hidden. */
function SingleSteps({ running, children }: { running: boolean; children: ReactNode }) {
  const [userOpen, setUserOpen] = useState(false)
  return (
    <details className="admin-steps" open={userOpen || running} onToggle={(e) => setUserOpen(e.currentTarget.open)}>
      <summary>Run a single step instead</summary>
      {children}
    </details>
  )
}

/** A scan or fetch button with its own info control beside it. The help's
 * label names the action, so each control can be told apart. */
function ActionButton({
  name,
  help,
  onClick,
  disabled,
  title,
  children,
}: {
  name: string
  help: string
  onClick: () => void
  disabled?: boolean
  title?: string
  children?: ReactNode
}) {
  return (
    <span className="admin-action-control">
      <button type="button" onClick={onClick} disabled={disabled} title={title}>
        {children ?? name}
      </button>
      <HelpTip label={`What ${name} does`} text={help} />
    </span>
  )
}

/** What the Users and Library roots sections show, loaded together. */
interface AdminData {
  users: AdminUser[]
  roots: LibraryRoot[]
}

const NO_ADMIN_DATA: AdminData = { users: [], roots: [] }

async function loadAdminData(): Promise<AdminData> {
  const [users, roots] = await Promise.all([api.adminListUsers(), api.adminListRoots()])
  return { users, roots }
}

/** Admin tools, for admins only: anyone else who types #/admin gets a short
 * message and no admin requests are made. */
export function AdminPanel() {
  const { user } = useAuth()
  if (!user?.is_admin) return <p className="empty" role="alert">Admin access required.</p>
  return <AdminTools />
}

function AdminTools() {
  const { user: currentUser } = useAuth()
  const confirm = useConfirm()
  // The first load has a loading and a failed state of its own: an empty
  // Users list and no library roots must only ever mean there are none.
  // Everything after it (the polls below, a refresh after a change) replaces
  // the data quietly.
  const loaded = useAsync(loadAdminData, [])
  const setAdminData = loaded.setData
  const ready = loaded.data !== undefined
  const { users, roots } = loaded.data ?? NO_ADMIN_DATA
  const [newUsername, setNewUsername] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newUserIsAdmin, setNewUserIsAdmin] = useState(false)
  const [userError, setUserError] = useState<string | null>(null)
  const [userMessage, setUserMessage] = useState<string | null>(null)
  const [resetUserId, setResetUserId] = useState<number | null>(null)
  const [resetPassword, setResetPassword] = useState('')
  const [newRootPath, setNewRootPath] = useState('')
  const [newRootName, setNewRootName] = useState('')
  const [newRootShared, setNewRootShared] = useState(true)
  const [rootError, setRootError] = useState<string | null>(null)
  const [rootMessage, setRootMessage] = useState<string | null>(null)
  const [editRootId, setEditRootId] = useState<number | null>(null)
  const [editPath, setEditPath] = useState('')
  const [editName, setEditName] = useState('')
  const [editShared, setEditShared] = useState(true)
  const [scanMessages, setScanMessages] = useState<Record<number, string>>({})

  async function refresh() {
    setAdminData(await loadAdminData())
  }

  // Three requests go out when this refreshes, so it does not run on a short
  // timer all day: every 1.5 s while a library task is running (its progress is
  // what the page is for), otherwise once a half minute - and only while the
  // page is visible, with an immediate refresh when it comes back.
  const taskActive = roots.some((root) => root.active_task != null)
  useEffect(() => {
    // Polls only refresh what the first load (above, with its own error and
    // Retry) put on screen.
    if (!ready) return
    let cancelled = false
    async function load() {
      if (document.visibilityState !== 'visible') return
      try {
        const next = await loadAdminData()
        if (!cancelled) setAdminData(next)
      } catch {
        // Keep previously loaded data visible if a transient poll fails.
      }
    }
    const pollId = window.setInterval(() => void load(), taskActive ? TASK_POLL_MS : IDLE_POLL_MS)
    document.addEventListener('visibilitychange', load)
    return () => {
      cancelled = true
      window.clearInterval(pollId)
      document.removeEventListener('visibilitychange', load)
    }
  }, [taskActive, ready, setAdminData])

  // React resets a form once its action is done, failed or not. The text
  // fields are controlled, and React keeps a controlled field's default value
  // in step with its value, so what was typed stays. It does not do that for
  // a controlled checkbox (the reset put back its first state while the state
  // here kept the new one), so each form's checkbox sets its default from
  // the state instead (`defaultChecked`) and reports changes to it: the reset
  // then puts back what the state holds - after a failure what was ticked,
  // after a success the cleared value.
  const [, createUser, creatingUser] = useActionState(async () => {
    setUserError(null)
    if (!newUsername.trim()) {
      setUserError('Username cannot be blank.')
      return
    }
    if (newPassword.length < 8) {
      setUserError('Password must be at least 8 characters.')
      return
    }
    try {
      await api.adminCreateUser(newUsername.trim(), newPassword, newUserIsAdmin)
      setNewUsername('')
      setNewPassword('')
      setNewUserIsAdmin(false)
      void refresh()
    } catch (e) {
      setUserError(e instanceof ApiError ? e.message : 'Could not create user.')
    }
  }, undefined)

  async function deleteUser(id: number, username: string) {
    if (!(await confirm({
      title: 'Delete user',
      body: `Delete user "${username}"? This cannot be undone.`,
      confirmLabel: 'Delete',
      danger: true,
    }))) return
    setUserError(null)
    setUserMessage(null)
    try {
      await api.adminDeleteUser(id)
      refresh()
    } catch (e) {
      setUserError(e instanceof ApiError ? e.message : 'Could not delete user.')
    }
  }

  async function setUserAdmin(u: AdminUser, isAdmin: boolean) {
    const verb = isAdmin ? 'Make' : 'Remove'
    if (!(await confirm({
      title: `${verb} admin`,
      body: `${verb} "${u.username}" ${isAdmin ? 'an admin' : 'as an admin'}?`,
      confirmLabel: verb,
    }))) return
    setUserError(null)
    setUserMessage(null)
    try {
      await api.adminUpdateUser(u.id, isAdmin)
      refresh()
    } catch (e) {
      setUserError(e instanceof ApiError ? e.message : 'Could not change the admin role.')
    }
  }

  // For the user whose reset form is open (one at a time).
  const [, submitPasswordReset, resettingPassword] = useActionState(async () => {
    const u = users.find((candidate) => candidate.id === resetUserId)
    if (!u) return
    setUserError(null)
    setUserMessage(null)
    if (resetPassword.length < 8) {
      setUserError('Password must be at least 8 characters.')
      return
    }
    try {
      await api.adminResetUserPassword(u.id, resetPassword)
      setResetUserId(null)
      setResetPassword('')
      setUserMessage(`Password reset for "${u.username}". They have been signed out everywhere.`)
    } catch (e) {
      setUserError(e instanceof ApiError ? e.message : 'Could not reset the password.')
    }
  }, undefined)
  // A new attempt clears the last one's message at once (what an
  // action sets before its request only shows when the action is done).
  const userFormBusy = creatingUser || resettingPassword

  const [, createRoot, addingRoot] = useActionState(async () => {
    setRootError(null)
    setRootMessage(null)
    if (!newRootPath.trim() || !newRootName.trim()) return
    try {
      await api.adminCreateRoot(newRootPath.trim(), newRootName.trim(), newRootShared)
      setNewRootPath('')
      setNewRootName('')
      setNewRootShared(true)
      void refresh()
    } catch (e) {
      setRootError(e instanceof ApiError ? e.message : 'Could not add library root.')
    }
  }, undefined)

  function startEditRoot(r: LibraryRoot) {
    setRootError(null)
    setRootMessage(null)
    setEditRootId(r.id)
    setEditPath(r.path)
    setEditName(r.display_name)
    setEditShared(r.is_shared)
  }

  // For the root whose edit form is open (one at a time).
  const [, saveRootEdit, savingRoot] = useActionState(async () => {
    const r = roots.find((candidate) => candidate.id === editRootId)
    if (!r) return
    setRootError(null)
    setRootMessage(null)
    const path = editPath.trim()
    const name = editName.trim()
    if (!path || !name) {
      setRootError('Path and display name cannot be blank.')
      return
    }
    const changes: { path?: string; display_name?: string; is_shared?: boolean } = {}
    if (path !== r.path) changes.path = path
    if (name !== r.display_name) changes.display_name = name
    if (editShared !== r.is_shared) changes.is_shared = editShared
    if (Object.keys(changes).length === 0) {
      setEditRootId(null)
      return
    }
    try {
      await api.adminUpdateRoot(r.id, changes)
      setEditRootId(null)
      setRootMessage(
        changes.path
          ? `Library path "${name}" updated. Run a scan so the library matches the new location.`
          : `Library path "${name}" updated.`,
      )
      void refresh()
    } catch (e) {
      setRootError(e instanceof ApiError ? e.message : 'Could not update library root.')
    }
  }, undefined)
  const rootFormBusy = addingRoot || savingRoot

  async function deleteRoot(id: number, displayName: string) {
    if (!(await confirm({
      title: 'Remove library path',
      body: `Remove library path "${displayName}" from Atriolyric? Its indexed tracks and associated database records will be removed, along with generated Atriolyric cache files. The music files themselves will not be changed or deleted.`,
      confirmLabel: 'Remove',
      danger: true,
    }))) return
    setRootError(null)
    setRootMessage(null)
    try {
      await api.adminDeleteRoot(id)
      setRootMessage(`Library path "${displayName}" was removed from Atriolyric. Its indexed tracks and associated database records were removed; the music files themselves were not changed or deleted.`)
      refresh()
    } catch (e) {
      setRootError(e instanceof ApiError ? e.message : 'Could not remove library path.')
    }
  }

  async function scanRoot(id: number, mode: ScanMode = 'normal') {
    try {
      await api.adminScanRoot(id, mode)
      setScanMessages((prev) => ({ ...prev, [id]: mode === 'confirm_removals' ? 'Scan started; missing tracks will be removed.' : 'Scan started.' }))
      await refresh()
    } catch (e) {
      setScanMessages((prev) => ({ ...prev, [id]: e instanceof Error ? e.message : 'Could not start scan.' }))
    }
  }

  async function removeMissing(id: number, displayName: string, count: number) {
    // Removal also drops every user's favourites, playlist entries and play
    // history for those tracks; a later rescan cannot bring them back.
    if (!(await confirm({
      title: 'Remove missing tracks',
      body: `Remove ${count} missing ${count === 1 ? 'track' : 'tracks'} from "${displayName}"? Check that the folder is mounted first: their favourites, playlist entries and play history are removed too and do not come back if the files reappear.`,
      confirmLabel: 'Remove',
      danger: true,
    }))) return
    scanRoot(id, 'confirm_removals')
  }

  type MetadataMode = 'mbids' | 'musicbrainz_missing' | 'listenbrainz_missing' | 'listenbrainz_all' | 'artist_info'
  const metadataTasks: Record<MetadataMode, LibraryTask['kind']> = {
    mbids: 'mb_mbid',
    musicbrainz_missing: 'enrich',
    listenbrainz_missing: 'lb_tags',
    listenbrainz_all: 'lb_tags_all',
    artist_info: 'artist_info',
  }
  const metadataLabels: Record<MetadataMode, string> = {
    mbids: 'Fetch MBIDs',
    musicbrainz_missing: 'Fetch missing MusicBrainz data',
    listenbrainz_missing: 'Fetch missing ListenBrainz tags',
    listenbrainz_all: 'Fetch all ListenBrainz tags',
    artist_info: 'Artist info',
  }
  const metadataHelp: Record<MetadataMode, string> = {
    mbids: 'Resolves missing MusicBrainz recording IDs. It tries ISRC first, then artist and title. Use this before fetching detailed metadata.',
    musicbrainz_missing: 'Fetches full MusicBrainz metadata only for tracks that do not have cached MusicBrainz data. Best for the public service after an ID lookup.',
    listenbrainz_missing: 'Recommended. Adds each artist\'s tags and genres from ListenBrainz (the artist\'s MusicBrainz community tags) to tracks that have not been looked up yet. Needs the tracks\' MusicBrainz artist IDs (Fetch MBIDs). They are added alongside file tags and MusicBrainz data, never replacing them.',
    listenbrainz_all: 'Re-fetches the ListenBrainz artist tags and genres for every track. Only those tags are refreshed; file tags and MusicBrainz data are left alone.',
    artist_info: 'Prefetches the Info popup (Wikipedia bio, ListenBrainz stats, MusicBrainz facts) and the Top tracks list for every artist on this root, so artist pages open with them ready.',
  }

  async function enrichRoot(id: number, mode: MetadataMode) {
    try {
      await api.adminEnrichRoot(id, mode)
      setScanMessages((prev) => ({
        ...prev,
        [id]: `${metadataLabels[mode]} started.`,
      }))
      await refresh()
    } catch (e) {
      setScanMessages((prev) => ({ ...prev, [id]: e instanceof Error ? e.message : 'Could not start metadata refresh.' }))
    }
  }

  async function fullSetup(id: number, name: string) {
    if (!(await confirm({
      title: 'Run a full setup scan',
      body: `Run a full setup scan of ${name}? It rescans every file and refetches all metadata from MusicBrainz and ListenBrainz, which can take hours on a large library.`,
      confirmLabel: 'Run scan',
    }))) return
    try {
      await api.adminFullSetup(id)
      setScanMessages((prev) => ({ ...prev, [id]: 'Full setup scan started.' }))
      await refresh()
    } catch (e) {
      setScanMessages((prev) => ({ ...prev, [id]: e instanceof Error ? e.message : 'Could not start the full setup scan.' }))
    }
  }

  async function fullSetupNew(id: number) {
    try {
      await api.adminFullSetupNew(id)
      setScanMessages((prev) => ({ ...prev, [id]: 'Setup for new entries started.' }))
      await refresh()
    } catch (e) {
      setScanMessages((prev) => ({ ...prev, [id]: e instanceof Error ? e.message : 'Could not start the setup for new entries.' }))
    }
  }

  async function stopTask(rootId: number, taskId: number) {
    try {
      await api.adminStopTask(rootId, taskId)
      setScanMessages((prev) => ({ ...prev, [rootId]: 'Stop requested; finishing the current track.' }))
      await refresh()
    } catch (e) {
      setScanMessages((prev) => ({ ...prev, [rootId]: e instanceof Error ? e.message : 'Could not stop task.' }))
    }
  }

  async function toggleAccess(rootId: number, userId: number, hasAccess: boolean) {
    // Optimistic update - `checked` is a controlled prop driven by this
    // state, so without it the checkbox visibly snaps back to its old
    // value on the next render (a plain click doesn't move React state)
    // and only catches up once the request/refresh round-trip lands.
    setAdminData((prev) => {
      const data = prev ?? NO_ADMIN_DATA
      return {
        ...data,
        users: data.users.map((u) =>
          u.id !== userId
            ? u
            : {
                ...u,
                accessible_root_ids: hasAccess
                  ? u.accessible_root_ids.filter((id) => id !== rootId)
                  : [...u.accessible_root_ids, rootId],
              },
        ),
      }
    })
    try {
      if (hasAccess) await api.adminRevokeAccess(rootId, userId)
      else await api.adminGrantAccess(rootId, userId)
    } finally {
      refresh()
    }
  }

  return (
    <div className="admin-panel">
      {/* The page title is the top bar's; its help tip sits beside it. */}
      <TopBarTitlePortal>
        <HelpTip label="About Admin" text="Manage user accounts and library paths here. Library roots can be scanned and enriched with metadata; the app manages its database records but does not delete your original music files." />
      </TopBarTitlePortal>
      {!ready ? (
        loaded.status === 'error' ? (
          <ErrorState
            message={errorMessage(loaded.error, 'Could not load the users and library roots.')}
            onRetry={loaded.retry}
          />
        ) : (
          <LoadingState />
        )
      ) : (
      <>
      <section className="admin-section">
        <div className="admin-section-heading">
          <h2>Users</h2>
          <HelpTip label="About users" text="Create accounts and grant or revoke access to private library roots. Shared roots are available to all users." />
        </div>
        <ul className="admin-list">
          {users.map((u) => (
            <li key={u.id} className="admin-user-row">
              <div className="admin-user-summary">
                <span>
                  {u.username} {u.is_admin && <em>(admin)</em>}
                </span>
                {u.id !== currentUser?.id && (
                  <span className="admin-user-actions">
                    <button onClick={() => setUserAdmin(u, !u.is_admin)}>
                      {u.is_admin ? 'Remove admin' : 'Make admin'}
                    </button>
                    <button
                      onClick={() => {
                        setResetUserId(resetUserId === u.id ? null : u.id)
                        setResetPassword('')
                      }}
                      aria-expanded={resetUserId === u.id}
                    >
                      Reset password
                    </button>
                    <button className="admin-icon-button" onClick={() => deleteUser(u.id, u.username)} aria-label={`Delete ${u.username}`}>
                      <TrashIcon />
                    </button>
                  </span>
                )}
              </div>
              {resetUserId === u.id && (
                <form className="admin-reset-row" action={submitPasswordReset}>
                  <PasswordInput
                    aria-label={`New password for ${u.username}`}
                    placeholder={`New password for ${u.username}`}
                    value={resetPassword}
                    onChange={(e) => setResetPassword(e.target.value)}
                    autoComplete="new-password"
                  />
                  <SubmitButton pendingLabel="Saving…">Save</SubmitButton>
                  <button type="button" onClick={() => setResetUserId(null)}>Cancel</button>
                </form>
              )}
            </li>
          ))}
        </ul>
        <form className="admin-create-row" action={createUser}>
          <input
            aria-label="Username"
            placeholder="Username"
            value={newUsername}
            onChange={(e) => setNewUsername(e.target.value)}
            // A name, not prose: a phone must not capitalise or "correct" it.
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            autoComplete="off"
          />
          <PasswordInput
            aria-label="Password"
            placeholder="Password (min. 8 characters)"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            autoComplete="new-password"
          />
          <Switch plain defaultChecked={newUserIsAdmin} onChange={setNewUserIsAdmin}>
            Admin
          </Switch>
          <SubmitButton className="primary" pendingLabel="Adding…">Add user</SubmitButton>
        </form>
        {userError && !userFormBusy && <p className="login-error" role="alert">{userError}</p>}
        {userMessage && !userFormBusy && <p className="settings-hint" role="status">{userMessage}</p>}
      </section>

      <section className="admin-section">
        <div className="admin-section-heading">
          <h2>Library roots</h2>
          <HelpTip label="About library roots" text="Add or remove indexed library paths, run scans, and fetch metadata. Removing a path deletes its app database records only; the music files are left untouched." />
        </div>
        <ul className="admin-list admin-roots">
          {roots.map((r) => (
            <li key={r.id} className="admin-root-row">
              {(() => {
                const task = r.active_task
                const lastTask = r.last_task
                return <>
              <div className="admin-root-main">
                <strong>{r.display_name}</strong>{' '}
                {r.is_shared && <span className="badge">shared</span>}{' '}
                <span className="admin-root-path">{r.path}</span>
                <div className="admin-root-buttons">
                  <button onClick={() => startEditRoot(r)} disabled={editRootId === r.id} title={`Edit library path ${r.display_name}`}>
                    Edit
                  </button>
                  <button className="danger" onClick={() => deleteRoot(r.id, r.display_name)} title={`Remove library path ${r.display_name}`}>
                    Remove path
                  </button>
                </div>
              </div>
              {editRootId === r.id && (
                <form className="admin-create-row admin-root-edit" action={saveRootEdit}>
                  <input
                    aria-label="Filesystem path"
                    placeholder="Filesystem path"
                    value={editPath}
                    onChange={(e) => setEditPath(e.target.value)}
                  />
                  <input
                    aria-label="Display name"
                    placeholder="Display name"
                    value={editName}
                    onChange={(e) => setEditName(e.target.value)}
                  />
                  <Switch plain defaultChecked={editShared} onChange={setEditShared}>
                    Shared (visible to everyone)
                  </Switch>
                  <SubmitButton className="primary" pendingLabel="Saving…">Save</SubmitButton>
                  <button type="button" onClick={() => setEditRootId(null)}>Cancel</button>
                </form>
              )}
              <div className="admin-root-actions">
                <h3 className="admin-subhead">Library scan</h3>
                <div className="admin-tks">
                  <ActionButton name="Scan" help={SCAN_HELP.normal} onClick={() => scanRoot(r.id)} disabled={task != null}>
                    {task?.kind === 'scan' ? `Scanning ${task.total ? `${Math.round((task.processed / task.total) * 100)}%` : '…'}` : 'Scan'}
                  </ActionButton>
                  <ActionButton name="Rescan metadata" help={SCAN_HELP.metadata} onClick={() => scanRoot(r.id, 'metadata')} disabled={task != null} />
                  <ActionButton name="Full rescan" help={SCAN_HELP.full} onClick={() => scanRoot(r.id, 'full')} disabled={task != null} />
                </div>
                <h3 className="admin-subhead">Setup scan</h3>
                <div className="admin-tks">
                  {(() => {
                    const ownsTask = task?.kind === 'full_setup_new'
                    return (
                      <ActionButton
                        name="Set up new entries"
                        help={SETUP_NEW_HELP}
                        onClick={() => ownsTask ? stopTask(r.id, task.id) : fullSetupNew(r.id)}
                        disabled={task != null && (!ownsTask || task.status === 'stopping')}
                      >
                        {ownsTask ? task.status === 'stopping' ? 'Stopping…' : 'Stop' : 'Set up new entries'}
                      </ActionButton>
                    )
                  })()}
                  {(() => {
                    const ownsTask = task?.kind === 'full_setup'
                    return (
                      <ActionButton
                        name="Full setup scan"
                        help={FULL_SETUP_HELP}
                        onClick={() => ownsTask ? stopTask(r.id, task.id) : fullSetup(r.id, r.display_name)}
                        disabled={task != null && (!ownsTask || task.status === 'stopping')}
                      >
                        {ownsTask ? task.status === 'stopping' ? 'Stopping…' : 'Stop' : 'Full setup scan'}
                      </ActionButton>
                    )
                  })()}
                </div>
                <SingleSteps running={task != null && SINGLE_STEP_TASKS.includes(task.kind)}>
                  <div className="admin-tks">
                    {(['mbids', 'musicbrainz_missing', 'listenbrainz_missing', 'listenbrainz_all', 'artist_info'] as MetadataMode[]).map((mode) => {
                      const ownsTask = task?.kind === metadataTasks[mode] || (mode === 'mbids' && task?.kind === 'mb_isrc')
                      return (
                        <ActionButton
                          key={mode}
                          name={metadataLabels[mode]}
                          help={metadataHelp[mode]}
                          onClick={() => ownsTask ? stopTask(r.id, task!.id) : enrichRoot(r.id, mode)}
                          disabled={
                            ownsTask
                              ? task!.status === 'stopping'
                              : task != null
                          }
                        >
                          {ownsTask ? task!.status === 'stopping' ? 'Stopping…' : 'Stop' : metadataLabels[mode]}
                        </ActionButton>
                      )
                    })}
                  </div>
                  <p className="admin-note">Both setup scans already do all of these.</p>
                </SingleSteps>
              </div>
              {task && <TaskProgress task={task} />}
              {scanMessages[r.id] && <div className="admin-scan-message">{scanMessages[r.id]}</div>}
              {!task && lastTask?.status === 'completed' && (
                <div className="admin-scan-message">
                  {lastTask.kind === 'scan'
                    ? `Last scan: +${lastTask.added} ~${lastTask.updated} -${lastTask.removed}`
                    : isFullSetup(lastTask)
                    ? `${lastTask.kind === 'full_setup_new' ? 'Setup for new entries' : 'Full setup scan'} finished${lastTask.completed_at ? ` ${new Date(lastTask.completed_at + 'Z').toLocaleString()}` : ''}.`
                    : `${lastTask.kind === 'mb_isrc' || lastTask.kind === 'mb_mbid' ? 'MBID lookup' : lastTask.kind === 'lb_tags_all' ? 'All ListenBrainz tags' : lastTask.kind === 'lb_tags' ? 'Missing ListenBrainz tags' : isOldLastfmTask(lastTask) ? 'Last.fm tags (old)' : lastTask.kind === 'artist_info' ? 'Artist info' : 'MusicBrainz metadata'}: saved ${lastTask.kind === 'artist_info' ? 'info' : 'metadata'} for ${lastTask.matched} of ${lastTask.total} ${lastTask.kind === 'artist_info' ? 'artists' : 'tracks'} · ${taskSuccesses(lastTask)} successes · ${taskFailures(lastTask)} failures`}
              </div>
              )}
              {!task && lastTask && (lastTask.missing_held ?? 0) > 0 && (
                <div className="admin-scan-message admin-scan-error" role="alert">
                  {lastTask.note ?? `${lastTask.missing_held} tracks look missing - the folder may be unmounted or unreadable. Nothing was removed.`}{' '}
                  <button className="danger" onClick={() => removeMissing(r.id, r.display_name, lastTask.missing_held ?? 0)}>
                    Remove missing tracks
                  </button>
                </div>
              )}
              {!task && lastTask?.status === 'failed' && (
                <div className="admin-scan-message admin-scan-error">{lastTask.kind === 'scan' ? 'Scan' : 'Metadata task'} failed: {lastTask.error}</div>
              )}
              {!task && lastTask?.status === 'stopped' && (
                <div className="admin-scan-message">
                  {isFullSetup(lastTask) ? `${lastTask.kind === 'full_setup_new' ? 'Setup for new entries' : 'Full setup scan'} stopped during step ${lastTask.stage ?? '?'} - ` : 'Metadata task stopped at '}{lastTask.processed} of {lastTask.total} {isFullSetup(lastTask) ? 'done' : 'tracks'} · {taskSuccesses(lastTask)} successes · {taskFailures(lastTask)} failures
                </div>
              )}
              {r.last_scan_error && !scanMessages[r.id] && (
                <div className="admin-scan-message admin-scan-error">
                  Last scan error: {r.last_scan_error}
                </div>
              )}
              {!r.is_shared && (
                <div className="admin-access" role="group" aria-labelledby={`admin-access-label-${r.id}`}>
                <span className="admin-access-label" id={`admin-access-label-${r.id}`}>
                  Who can see it
                </span>
                <div className="admin-access-grants">
                  {users.map((u) => {
                    const hasAccess = u.accessible_root_ids.includes(r.id)
                    return (
                      <Switch key={u.id} plain checked={hasAccess} onChange={() => toggleAccess(r.id, u.id, hasAccess)}>
                        {u.username}
                      </Switch>
                    )
                  })}
                </div>
                </div>
              )}
                </>
              })()}
            </li>
          ))}
        </ul>
        <form className="admin-create-row" action={createRoot}>
          <input
            aria-label="Filesystem path"
            placeholder="Filesystem path"
            value={newRootPath}
            onChange={(e) => setNewRootPath(e.target.value)}
            autoComplete="off"
          />
          <input
            aria-label="Display name"
            placeholder="Display name"
            value={newRootName}
            onChange={(e) => setNewRootName(e.target.value)}
            autoComplete="off"
          />
          <Switch plain defaultChecked={newRootShared} onChange={setNewRootShared}>
            Shared (visible to everyone)
          </Switch>
          <SubmitButton className="primary" pendingLabel="Adding…">Add root</SubmitButton>
        </form>
        {rootError && !rootFormBusy && <p className="login-error" role="alert">{rootError}</p>}
        {rootMessage && !rootFormBusy && <p className="admin-scan-message" role="status">{rootMessage}</p>}
      </section>
      </>
      )}
      <BackgroundWorkSection />
      <ArtistSplittingSection />
      <UnrecordedArtSection />
      {/* The log loads when its card comes near the screen. While the users
          and library roots are still loading the page is short, which would
          put the card there at once; it waits for the page to have its
          real height. */}
      {loaded.status !== 'loading' || ready ? <ActivityLog /> : null}
    </div>
  )
}

/** Separators are shown quoted so a leading/trailing space (" & ") is visible. */
/** Library art whose source was not recorded: some may be Deezer images,
 * which Deezer's terms do not allow keeping. Shown only while there is any. */
function UnrecordedArtSection() {
  const confirm = useConfirm()
  const [count, setCount] = useState<number | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    api.adminUnrecordedArt().then(
      (answer) => {
        if (!cancelled) setCount(answer.count)
      },
      () => {},
    )
    return () => {
      cancelled = true
    }
  }, [])

  async function remove() {
    setError(null)
    if (!(await confirm({
      title: 'Remove older artwork',
      body: `Remove ${count} artwork${count === 1 ? '' : 's'} saved before 0.23.0? Each falls back to its default art and can be chosen again.`,
      confirmLabel: 'Remove them',
      danger: true,
    }))) return
    setBusy(true)
    try {
      const { removed } = await api.adminRemoveUnrecordedArt()
      setMessage(`Removed ${removed} artwork${removed === 1 ? '' : 's'}.`)
      setCount(0)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not remove the artwork.')
    } finally {
      setBusy(false)
    }
  }

  if (!count && !message) return null
  return (
    <section className="admin-section" aria-labelledby="admin-unrecorded-art">
      <div className="admin-section-heading">
        <h2 id="admin-unrecorded-art">Older artwork</h2>
      </div>
      {count ? (
        <div className="notice-box">
          <p>
            {count} artwork{count === 1 ? ' was' : 's were'} saved before 0.23.0 without a recorded source. Some
            may be Deezer images picked in the old art picker, which Deezer&apos;s terms don&apos;t allow keeping.
          </p>
          <button type="button" onClick={() => void remove()} disabled={busy}>
            {busy ? 'Removing…' : 'Remove them'}
          </button>
        </div>
      ) : null}
      {message && <p className="settings-hint" role="status">{message}</p>}
      {error && <p className="login-error" role="alert">{error}</p>}
    </section>
  )
}

function formatSeparators(separators: string[]): string {
  return separators.map((separator) => JSON.stringify(separator)).join(', ')
}

function parseSeparators(text: string): string[] | null {
  try {
    const parsed = JSON.parse(`[${text}]`)
    return Array.isArray(parsed) && parsed.every((v) => typeof v === 'string') ? parsed : null
  } catch {
    return null
  }
}

/** How artist tags split into separate artists - saving re-links every
 * track from its stored tags right away (no rescan). */
function ArtistSplittingSection() {
  const [separators, setSeparators] = useState('')
  const [exceptions, setExceptions] = useState('')
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // The one error that belongs to a single field (the separators box).
  const [separatorsInvalid, setSeparatorsInvalid] = useState(false)

  useEffect(() => {
    api.adminGetArtistSplitRules().then(
      (rules) => {
        setSeparators(formatSeparators(rules.separators))
        setExceptions(rules.exceptions.join('\n'))
      },
      () => setError('Could not load the artist splitting rules.'),
    )
  }, [])

  const [, save, saving] = useActionState(async () => {
    const parsed = parseSeparators(separators)
    if (!parsed) {
      setError('Separators must be quoted and comma-separated, e.g. ";", "/", " & ".')
      setSeparatorsInvalid(true)
      return
    }
    setError(null)
    setSeparatorsInvalid(false)
    setMessage(null)
    try {
      const result = await api.adminPutArtistSplitRules({
        separators: parsed,
        exceptions: exceptions.split('\n').map((name) => name.trim()).filter(Boolean),
      })
      setSeparators(formatSeparators(result.separators))
      setExceptions(result.exceptions.join('\n'))
      setMessage(`Re-linked ${result.relinked} track${result.relinked === 1 ? '' : 's'}.`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not save the artist splitting rules.')
    }
  }, undefined)

  return (
    <section className="admin-section" aria-labelledby="admin-artist-splitting">
      <div className="admin-section-heading">
        <h2 id="admin-artist-splitting">Artist splitting</h2>
        <HelpTip
          label="About artist splitting"
          text="Artist tags like “A; B” or “A feat. B” are split into separate artists, each with their own page. Multi-value ARTISTS tags (Picard, beets) are always used as-is. feat./ft./featuring always mark guests; the separators below split everything else. Names in the exception list are never split."
        />
      </div>
      <form className="admin-split-form" action={save}>
        <label>
          Separators
          <input
            value={separators}
            onChange={(e) => setSeparators(e.target.value)}
            spellCheck={false}
            aria-describedby={separatorsInvalid ? 'admin-split-separators-hint admin-split-error' : 'admin-split-separators-hint'}
            aria-invalid={separatorsInvalid ? true : undefined}
          />
        </label>
        <p className="settings-hint" id="admin-split-separators-hint">
          Quoted and comma-separated, spaces included - e.g. ";", "/", " & ".
        </p>
        <label>
          Never split (one name per line)
          <textarea value={exceptions} onChange={(e) => setExceptions(e.target.value)} rows={6} spellCheck={false} />
        </label>
        <SubmitButton className="primary" pendingLabel="Saving…">Save and re-link</SubmitButton>
        {error && !saving && <p className="login-error" id="admin-split-error" role="alert">{error}</p>}
        {message && !saving && <p className="admin-scan-message" role="status">{message}</p>}
      </form>
    </section>
  )
}
