import { useEffect, useMemo, useRef, useState } from 'react'
import { api, type ActivityFilters } from '../api'
import type { ActivityEvent } from '../types'
import { useNearViewport } from '../useNearViewport'
import { AdminHelpTip } from './AdminHelpTip'
import { SelectMenu } from './SelectMenu'

// Admin -> Server activity: what the server has been doing, newest first.
// Nothing here is requested until the card is about to be seen (or the Admin
// tab has sat open a few seconds), and the page is only refreshed while the
// card is on screen: a hidden Admin tab's effects are disconnected, so it
// stops by itself, like the rest of the panel.

const PAGE_SIZE = 100
const POLL_MS = 30_000
/** Admin has been open this long without the card coming into view: load it. */
const IDLE_LOAD_MS = 4000
/** At most this many kind groups get a chip of their own; the rest share "Other". */
const MAX_GROUP_CHIPS = 7

/** Chips come in this order when present; other prefixes follow alphabetically. */
const GROUP_ORDER = ['auth', 'task', 'request', 'log', 'root', 'media', 'user', 'server']
const GROUP_LABELS: Record<string, string> = {
  auth: 'Sign-ins',
  task: 'Tasks',
  root: 'Library paths',
  library: 'Library',
  media: 'Media',
  user: 'Users',
  server: 'Server errors',
  // The application log's own lines (app.log_mirror): background work and
  // warnings by area, and requests that failed or were slow.
  log: 'Server log',
  request: 'Requests',
  client: 'App',
  lastfm: 'Last.fm',
  listenbrainz: 'ListenBrainz',
  privacy: 'Privacy',
  artist: 'Artists',
}

function groupOf(kind: string): string {
  return kind.split('.')[0]
}

function groupLabel(group: string): string {
  return GROUP_LABELS[group] ?? group.charAt(0).toUpperCase() + group.slice(1)
}

interface Group {
  id: string
  label: string
  kinds: string[]
}

/** The chips for the kinds present: one per prefix, at most a handful, the
 * remainder together as "Other". */
function buildGroups(kinds: string[]): Group[] {
  const byPrefix = new Map<string, string[]>()
  for (const kind of kinds) {
    const prefix = groupOf(kind)
    byPrefix.set(prefix, [...(byPrefix.get(prefix) ?? []), kind])
  }
  const prefixes = [...byPrefix.keys()].sort((a, b) => {
    const ra = GROUP_ORDER.indexOf(a)
    const rb = GROUP_ORDER.indexOf(b)
    if (ra !== rb) return (ra === -1 ? 99 : ra) - (rb === -1 ? 99 : rb)
    return a.localeCompare(b)
  })
  const shown = prefixes.slice(0, MAX_GROUP_CHIPS)
  const rest = prefixes.slice(MAX_GROUP_CHIPS)
  const groups: Group[] = shown.map((prefix) => ({
    id: prefix,
    label: groupLabel(prefix),
    kinds: byPrefix.get(prefix) ?? [],
  }))
  if (rest.length > 0) {
    groups.push({ id: 'other', label: 'Other', kinds: rest.flatMap((prefix) => byPrefix.get(prefix) ?? []) })
  }
  return groups
}

type DayChoice = 'today' | 'yesterday' | 'all' | 'date'

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

function localDay(daysAgo: number): string {
  const d = new Date()
  d.setDate(d.getDate() - daysAgo)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** The event's time on the viewer's clock (the server stores UTC, without a Z). */
function formatTime(createdAt: string, withDate: boolean): string {
  const d = new Date(`${createdAt}Z`)
  if (Number.isNaN(d.getTime())) return createdAt
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  return withDate ? `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${time}` : time
}

interface PageState {
  key: string
  events: ActivityEvent[]
  next: number | null
  failed: boolean
}

export function ActivityLog() {
  const cardRef = useRef<HTMLElement>(null)
  const [active] = useNearViewport(cardRef, { idleAfterMs: IDLE_LOAD_MS })
  const [view, setView] = useState<'all' | 'problems' | 'errors' | string>('all')
  const [kindChoice, setKindChoice] = useState('')
  const [dayChoice, setDayChoice] = useState<DayChoice>('today')
  const [pickedDay, setPickedDay] = useState(() => localDay(0))
  const [kinds, setKinds] = useState<string[]>([])
  const [page, setPage] = useState<PageState | null>(null)
  const [attempt, setAttempt] = useState(0)
  const [loadingMore, setLoadingMore] = useState(false)
  const [moreFailed, setMoreFailed] = useState(false)
  const [onScreen, setOnScreen] = useState(false)
  const [tzOffsetMinutes] = useState(() => new Date().getTimezoneOffset())

  const groups = useMemo(() => buildGroups(kinds), [kinds])
  const group = groups.find((g) => g.id === view)
  // A group shows all its kinds (the server takes the prefix "task." for
  // that) until one is picked. "Other" has no prefix of its own, so it
  // shows its first kind.
  const groupPrefix = group && group.id !== 'other' && group.kinds.length > 1 ? `${group.id}.` : undefined
  const kind = group
    ? group.kinds.includes(kindChoice) ? kindChoice : (groupPrefix ?? group.kinds[0])
    : undefined
  const day =
    dayChoice === 'today' ? localDay(0) : dayChoice === 'yesterday' ? localDay(1) : dayChoice === 'date' ? pickedDay : undefined
  const filters: ActivityFilters = {
    kind,
    level: view === 'errors' ? 'error' : view === 'problems' ? 'problems' : undefined,
    day: day || undefined,
    tzOffsetMinutes,
  }
  const filterKey = JSON.stringify(filters)
  const requestKey = `${filterKey}#${attempt}`
  const current = page?.key === requestKey ? page : null
  const status = !active ? 'idle' : current ? (current.failed ? 'error' : 'ready') : 'loading'

  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    const parsed = JSON.parse(filterKey) as ActivityFilters
    api.adminActivity(parsed, { limit: PAGE_SIZE }, controller.signal).then(
      (result) => {
        setKinds(result.kinds)
        setMoreFailed(false)
        setPage({ key: requestKey, events: result.events, next: result.next_before_id, failed: false })
      },
      () => {
        if (!controller.signal.aborted) setPage({ key: requestKey, events: [], next: null, failed: true })
      },
    )
    return () => controller.abort()
  }, [active, filterKey, requestKey])

  // Is the card on screen right now? (Not the sticky "near" above.)
  useEffect(() => {
    const el = cardRef.current
    if (!el || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(
      (entries) => setOnScreen(entries.some((entry) => entry.isIntersecting)),
      { root: el.closest('.app-main') },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  // While the card is on screen and the page visible, the newest events are
  // fetched every half minute and put above what is shown.
  useEffect(() => {
    if (!active || !onScreen) return
    const parsed = JSON.parse(filterKey) as ActivityFilters
    let cancelled = false
    const id = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return
      api.adminActivity(parsed, { limit: PAGE_SIZE }).then(
        (result) => {
          if (cancelled) return
          setKinds(result.kinds)
          setPage((prev) => {
            if (!prev || prev.key !== requestKey || prev.failed) return prev
            const newest = prev.events[0]?.id ?? 0
            const fresh = result.events.filter((event) => event.id > newest)
            return fresh.length === 0 ? prev : { ...prev, events: [...fresh, ...prev.events] }
          })
        },
        () => {},
      )
    }, POLL_MS)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [active, onScreen, filterKey, requestKey])

  async function loadOlder() {
    if (!current || current.next == null) return
    setLoadingMore(true)
    setMoreFailed(false)
    try {
      const result = await api.adminActivity(filters, { beforeId: current.next, limit: PAGE_SIZE })
      setPage((prev) =>
        prev && prev.key === requestKey
          ? { ...prev, events: [...prev.events, ...result.events], next: result.next_before_id }
          : prev,
      )
    } catch {
      setMoreFailed(true)
    } finally {
      setLoadingMore(false)
    }
  }

  function chooseView(next: string) {
    setView(next)
    setKindChoice('')
  }

  const events = current?.events ?? []
  const showDate = dayChoice === 'all'

  return (
    <section ref={cardRef} className="admin-section admin-activity" aria-labelledby="admin-activity-title">
      <div className="admin-section-heading">
        <h2 id="admin-activity-title">Server activity</h2>
        <AdminHelpTip
          label="About server activity"
          text="What the server has been doing, newest first: sign-ins, scans and other tasks, changes made by admins, requests that failed or were slow, and the server log's warnings, errors and background work. Problems shows only warnings and errors. Times are in your local time. Passwords and other secrets are never recorded."
        />
        <a
          className="admin-link-button admin-activity-download"
          href={api.adminActivityDownloadUrl(filters)}
          download
        >
          Download log
        </a>
      </div>
      <div className="admin-activity-filters">
        <div className="admin-activity-chips" role="group" aria-label="Show events">
          <button type="button" className="chip" aria-pressed={view === 'all'} onClick={() => chooseView('all')}>
            All
          </button>
          <button type="button" className="chip" aria-pressed={view === 'problems'} onClick={() => chooseView('problems')}>
            Problems
          </button>
          <button type="button" className="chip" aria-pressed={view === 'errors'} onClick={() => chooseView('errors')}>
            Errors
          </button>
          {groups.map((g) => (
            <button
              key={g.id}
              type="button"
              className="chip"
              aria-pressed={view === g.id}
              onClick={() => chooseView(g.id)}
            >
              {g.label}
            </button>
          ))}
        </div>
        <div className="admin-activity-selects">
          {group && group.kinds.length > 1 && (
            <SelectMenu<string>
              label="Event type"
              value={kind ?? ''}
              options={[
                ...(groupPrefix ? [{ value: groupPrefix, label: `All ${group.label.toLowerCase()}` }] : []),
                ...group.kinds.map((k) => ({ value: k, label: k })),
              ]}
              onChange={(k) => setKindChoice(k === groupPrefix ? '' : k)}
            />
          )}
          <SelectMenu<DayChoice>
            label="Day"
            value={dayChoice}
            options={[
              { value: 'today', label: 'Today' },
              { value: 'yesterday', label: 'Yesterday' },
              { value: 'date', label: 'Pick a date…' },
              { value: 'all', label: 'All days' },
            ]}
            onChange={setDayChoice}
          />
          {dayChoice === 'date' && (
            <input
              type="date"
              aria-label="Date"
              value={pickedDay}
              max={localDay(0)}
              onChange={(e) => e.target.value && setPickedDay(e.target.value)}
            />
          )}
        </div>
      </div>
      {status === 'error' ? (
        <div className="admin-activity-state" role="alert">
          <p className="login-error">Could not load the activity log.</p>
          <button type="button" onClick={() => setAttempt((n) => n + 1)}>Retry</button>
        </div>
      ) : status !== 'ready' ? (
        <p className="settings-hint admin-activity-state" aria-busy={status === 'loading'}>
          {status === 'idle' ? 'The log loads when you scroll here.' : 'Loading the activity log…'}
        </p>
      ) : events.length === 0 ? (
        <p className="settings-hint admin-activity-state">No activity matches these filters.</p>
      ) : (
        // A scrolling region has to be reachable by keyboard.
        // oxlint-disable-next-line jsx-a11y/no-noninteractive-tabindex
        <div className="logbox" role="log" aria-label="Server activity log" tabIndex={0}>
          {events.map((event) => (
            <div key={event.id} className={`log-line log-${event.level}`}>
              <b>{formatTime(event.created_at, showDate)}</b>{' '}
              <span className="log-kind">{event.kind}</span>{' '}
              {event.level !== 'info' && <span className="visually-hidden">{event.level}: </span>}
              {event.summary}
            </div>
          ))}
        </div>
      )}
      {status === 'ready' && current?.next != null && (
        <div className="admin-activity-more">
          <button type="button" onClick={() => void loadOlder()} disabled={loadingMore}>
            {loadingMore ? 'Loading…' : 'Load older'}
          </button>
          {moreFailed && <span className="login-error" role="alert">Could not load older events.</span>}
        </div>
      )}
    </section>
  )
}
