import {
  createContext,
  lazy,
  startTransition,
  Suspense,
  useActionState,
  useContext,
  useEffect,
  useOptimistic,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from 'react'
import { api, ApiError } from '../api'
import { useAuth } from '../AuthContext'
import { usePlayerActions } from '../PlayerContext'
import { useSettings } from '../SettingsContext'
import { useConfirm } from '../dialogs'
import {
  ACCENT_PRESETS,
  accentColorName,
  type CustomizableTab,
  type GridSize,
  GRID_SIZES,
  LIST_DENSITIES,
  LIST_DENSITY_LABELS,
  STREAM_QUALITIES,
  TAB_LABELS,
  type ThemeMode,
} from '../settings'
import type { DiscoverySettings, Integration } from '../types'
import { PasswordInput } from './PasswordInput'
import { DragHandleIcon } from './Icons'
import { useDragReorder } from '../useDragReorder'
import { SubmitButton } from './SubmitButton'
import { HelpTip } from './HelpTip'
import { SETTINGS_SECTIONS } from '../settingsSections'
import { useLazyLoad, useNearViewport } from '../useNearViewport'
import type { NavState } from '../nav'
import { PHONE_BAR_TABS } from './PhoneTabBar'
import { HomeSectionsEditor } from './HomeSectionsEditor'
import { Switch } from './Switch'
import { SelectMenu } from './SelectMenu'
import { isNativeApp } from '../native/bridge'
import { AndroidAppRow } from '../native/AndroidAppRow'
import { DownloadsSettings } from '../native/DownloadsSettings'

// The equaliser and volume levelling load when the Playback card is about to be
// seen, not with the page.
const AudioEffectsSettings = lazy(() =>
  import('./AudioEffectsSettings').then((m) => ({ default: m.AudioEffectsSettings })),
)

const HOUR_OPTIONS = Array.from({ length: 24 }, (_, h) => h)
/** A settings message: errors are announced at once and styled as errors,
 * successes are announced politely. */
type Notice = { text: string; error: boolean }

function SettingsNotice({ notice }: { notice: Notice | null }) {
  if (!notice) return null
  return notice.error ? (
    <p className="login-error" role="alert">{notice.text}</p>
  ) : (
    <p className="settings-hint" role="status">{notice.text}</p>
  )
}

function formatHour(h: number): string {
  return `${h.toString().padStart(2, '0')}:00`
}

const THEME_OPTIONS: { mode: ThemeMode; label: string }[] = [
  { mode: 'system', label: 'System' },
  { mode: 'light', label: 'Light' },
  { mode: 'dark', label: 'Dark' },
]

const GRID_SIZE_LABELS: Record<GridSize, string> = {
  small: 'Small',
  medium: 'Medium',
  large: 'Large',
  xlarge: 'Extra Large',
}

/** Where the source lives (AGPL section 13). The one place to change it. */
const SOURCE_REPOSITORY_URL = 'https://github.com/DorkSoul/atriolyric'

/** Whether the card around this component has come near the screen (or
 * received focus): cards fetch their data only then. */
const CardActiveContext = createContext(false)

/** One card of the Settings page. It watches its own place on the page and
 * tells what is inside once it is near enough to be worth loading. */
function SettingsCard({
  id,
  title,
  helpText,
  children,
}: {
  id: string
  title: string
  helpText: string
  children: ReactNode
}) {
  const ref = useRef<HTMLElement>(null)
  const [near, activate] = useNearViewport(ref)
  return (
    <section
      ref={ref}
      className="settings-section"
      id={id}
      aria-labelledby={`${id}-title`}
      tabIndex={-1}
      onFocusCapture={activate}
    >
      <div className="settings-section-heading">
        <h2 id={`${id}-title`}>{title}</h2>
        <HelpTip
          label={`About ${title.toLowerCase()} settings`}
          text={helpText}
          buttonClassName="settings-help-button"
        />
      </div>
      <CardActiveContext value={near}>{children}</CardActiveContext>
    </section>
  )
}

function SectionLoading({ what }: { what: string }) {
  return (
    <p className="settings-hint settings-loading" role="status" aria-busy="true">
      Loading {what}…
    </p>
  )
}

function SectionError({ text, onRetry }: { text: string; onRetry: () => void }) {
  return (
    <div className="settings-error" role="alert">
      <p className="login-error">{text}</p>
      <button type="button" onClick={onRetry}>Retry</button>
    </div>
  )
}

function AboutSection({ onInstall }: { onInstall?: () => void }) {
  return (
    <SettingsCard
      id="settings-about"
      title="About"
      helpText="Atriolyric is free software. If you run a modified version for other people, the licence requires you to offer them its source."
    >
      <p className="settings-hint">Version {__APP_VERSION__}</p>
      <p className="settings-hint">Licensed under AGPL-3.0-or-later.</p>
      <p className="settings-hint">
        <a href={SOURCE_REPOSITORY_URL} target="_blank" rel="noreferrer">Source code</a>
      </p>
      {onInstall && (
        <div className="settings-row settings-row-start">
          <button type="button" onClick={onInstall}>Install as app</button>
        </div>
      )}
      <AndroidAppRow />
    </SettingsCard>
  )
}

function AccountSection() {
  const { user, setUser, logout } = useAuth()
  const confirm = useConfirm()
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  // Shown where the password change says how it went.
  const [signOutMessage, setSignOutMessage] = useState<Notice | null>(null)
  const [usernamePassword, setUsernamePassword] = useState('')
  const [newUsername, setNewUsername] = useState('')

  async function signOutEverywhere() {
    if (!(await confirm({
      title: 'Sign out everywhere',
      body: 'Sign out of Atriolyric on every device, this one included? You will need to sign in again everywhere.',
      confirmLabel: 'Sign out everywhere',
      danger: true,
    }))) return
    try {
      await api.logoutEverywhere()
    } catch (e) {
      setSignOutMessage({ text: e instanceof ApiError ? e.message : 'Could not sign out everywhere.', error: true })
      return
    }
    // This session is gone too: the same path as Log out ends at the login page.
    await logout()
  }

  // Both forms' inputs are controlled, so what was typed stays after a
  // failed change (React resets a form once its action is done).
  const [usernameResult, changeUsername, changingUsername] = useActionState(async (): Promise<Notice | null> => {
    if (!usernamePassword || !newUsername.trim()) return null
    try {
      const updated = await api.changeUsername(usernamePassword, newUsername.trim())
      setUser(updated)
      setUsernamePassword('')
      setNewUsername('')
      return { text: 'Username changed.', error: false }
    } catch (e) {
      return { text: e instanceof ApiError ? e.message : 'Could not change username.', error: true }
    }
  }, null)

  const [passwordResult, changePassword, changingPassword] = useActionState(async (): Promise<Notice | null> => {
    setSignOutMessage(null)
    if (!currentPassword || !newPassword) return null
    try {
      await api.changePassword(currentPassword, newPassword)
      setCurrentPassword('')
      setNewPassword('')
      return { text: 'Password changed. Other sessions were signed out.', error: false }
    } catch (e) {
      return { text: e instanceof ApiError ? e.message : 'Could not change password.', error: true }
    }
  }, null)
  // A new attempt clears the last one's message.
  const usernameMessage = changingUsername ? null : usernameResult
  const passwordMessage = changingPassword ? null : signOutMessage ?? passwordResult

  return (
    <SettingsCard
      id="settings-account"
      title="Account"
      helpText="Change your username or password and sign out of this account."
    >
      <div className="settings-row settings-row-start">
        <p className="settings-hint settings-hint-inline">
          Signed in as <strong>{user?.username}</strong>.
        </p>
        <button onClick={logout}>Log out</button>
        <button onClick={() => void signOutEverywhere()}>Sign out everywhere</button>
      </div>
      <form className="integration-connect" action={changeUsername}>
        <input
          aria-label="New username"
          placeholder="New username"
          value={newUsername}
          onChange={(e) => setNewUsername(e.target.value)}
          // A name, not prose: a phone must not capitalise or "correct" it.
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          autoComplete="off"
        />
        <PasswordInput
          aria-label="Current password"
          autoComplete="current-password"
          placeholder="Current password"
          value={usernamePassword}
          onChange={(e) => setUsernamePassword(e.target.value)}
        />
        <SubmitButton pendingLabel="Changing…">Change username</SubmitButton>
      </form>
      <SettingsNotice notice={usernameMessage} />

      <p className="settings-hint settings-hint-spaced">Change your password and sign out other sessions.</p>
      <form className="integration-connect" action={changePassword}>
        <PasswordInput
          aria-label="Current password"
          autoComplete="current-password"
          placeholder="Current password"
          value={currentPassword}
          onChange={(e) => setCurrentPassword(e.target.value)}
        />
        <PasswordInput
          aria-label="New password"
          autoComplete="new-password"
          placeholder="New password"
          value={newPassword}
          onChange={(e) => setNewPassword(e.target.value)}
        />
        <SubmitButton pendingLabel="Changing…">Change password</SubmitButton>
      </form>
      <SettingsNotice notice={passwordMessage} />
    </SettingsCard>
  )
}

function PlaybackSection() {
  const { settings, updateSettings } = useSettings()
  return (
    <SettingsCard
      id="settings-playback"
      title="Playback"
      helpText="Choose a streaming quality limit, whether Atriolyric should adapt quality on slower connections, and how volume is levelled between tracks."
    >
      <p className="settings-hint">
        Cap streaming quality to save bandwidth. A track already at or below the cap always
        plays at its original quality - it's never upscaled to match.
      </p>
      <div className="settings-segmented settings-segmented-wide" role="group" aria-label="Streaming quality">
        <button
          className={settings.maxBitrateKbps === null ? 'active' : ''}
          aria-pressed={settings.maxBitrateKbps === null}
          onClick={() => updateSettings({ maxBitrateKbps: null })}
        >
          Original
        </button>
        {[...STREAM_QUALITIES].reverse().map((q) => (
          <button
            key={q}
            className={settings.maxBitrateKbps === q ? 'active' : ''}
            aria-pressed={settings.maxBitrateKbps === q}
            onClick={() => updateSettings({ maxBitrateKbps: q })}
          >
            {q} kbps
          </button>
        ))}
      </div>
      <p className="settings-hint settings-hint-spaced">
        Adaptive quality can step down when the connection cannot keep up, as far as 96 kbps,
        then step back up after the connection has been stable.
      </p>
      <div className="settings-row">
        <span>Adaptive quality</span>
        <Switch
          plain
          aria-label="Automatically lower quality on slow networks"
          checked={settings.adaptiveBitrateEnabled}
          onChange={(on) => updateSettings({ adaptiveBitrateEnabled: on })}
        >
          Automatically lower on slow networks
        </Switch>
      </div>
      <AudioEffects />
    </SettingsCard>
  )
}

/** Volume levelling and the equaliser: their code arrives once the card is near. */
function AudioEffects() {
  const active = useContext(CardActiveContext)
  if (!active) return <SectionLoading what="volume levelling and the equaliser" />
  return (
    <Suspense fallback={<SectionLoading what="volume levelling and the equaliser" />}>
      <AudioEffectsSettings />
    </Suspense>
  )
}

function SegmentedRow<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string
  options: { value: T; label: string }[]
  value: T
  onChange: (value: T) => void
}) {
  return (
    <div className="settings-row">
      <span>{label}</span>
      <div className="settings-segmented" role="group" aria-label={label}>
        {options.map((option) => (
          <button
            key={option.value}
            className={value === option.value ? 'active' : ''}
            aria-pressed={value === option.value}
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>
    </div>
  )
}

function AppearanceSection() {
  const { user } = useAuth()
  const { settings, updateSettings } = useSettings()
  const { setAccountAccentColor } = usePlayerActions()
  const gridOptions = GRID_SIZES.map((size) => ({ value: size, label: GRID_SIZE_LABELS[size] }))
  return (
    <SettingsCard
      id="settings-appearance"
      title="Appearance"
      helpText="Set the theme, accent colour, and artwork tile size. The accent colour is saved on your account and follows you to every device, including ones you log in to later; theme and tile size stay per-device."
    >
      <p className="settings-hint">
        Theme and artwork size for browse grids, Home rails, and list views are saved on
        this device, so a phone and a PC can each have their own. The accent colour is
        saved on your account - changing it here updates every device you're signed in on.
      </p>
      <SegmentedRow
        label="Theme"
        options={THEME_OPTIONS.map((o) => ({ value: o.mode, label: o.label }))}
        value={settings.theme}
        onChange={(theme) => updateSettings({ theme })}
      />
      <div className="settings-row settings-row-wrap">
        <span>Accent colour</span>
        <div className="accent-swatches" role="group" aria-label="Accent colour">
          {ACCENT_PRESETS.map((color) => (
            <button
              key={color}
              className={`accent-swatch ${user?.accent_color?.toLowerCase() === color.toLowerCase() ? 'accent-swatch-active' : ''}`}
              style={{ background: color }}
              onClick={() => setAccountAccentColor(color)}
              aria-label={`Use ${accentColorName(color)} as accent colour`}
              aria-pressed={user?.accent_color?.toLowerCase() === color.toLowerCase()}
            />
          ))}
        </div>
      </div>
      <SegmentedRow
        label="Tile size (PC)"
        options={gridOptions}
        value={settings.gridSizeDesktop}
        onChange={(gridSizeDesktop) => updateSettings({ gridSizeDesktop })}
      />
      <SegmentedRow
        label="Tile size (Mobile)"
        options={gridOptions}
        value={settings.gridSizeMobile}
        onChange={(gridSizeMobile) => updateSettings({ gridSizeMobile })}
      />
      <SegmentedRow
        label="List density"
        options={LIST_DENSITIES.map((d) => ({ value: d, label: LIST_DENSITY_LABELS[d] }))}
        value={settings.listDensity}
        onChange={(listDensity) => updateSettings({ listDensity })}
      />
      <SegmentedRow
        label="Artist pages"
        options={[
          { value: 'sections', label: 'Sections by type' },
          { value: 'grid', label: 'One grid' },
        ]}
        value={settings.artistAlbumSections ? 'sections' : 'grid'}
        onChange={(value) => updateSettings({ artistAlbumSections: value === 'sections' })}
      />
    </SettingsCard>
  )
}

/** The rows of the tab order, split the way a phone shows them: the first
 * three visible tabs sit beside Home in the bottom bar, the rest are under
 * More. Hidden tabs stay in the order, in the group they would join. */
function NavigationSection() {
  const { settings, updateSettings } = useSettings()

  function moveTab(tab: CustomizableTab, direction: -1 | 1) {
    const order = [...settings.tabOrder]
    const index = order.indexOf(tab)
    const swapWith = index + direction
    if (swapWith < 0 || swapWith >= order.length) return
    ;[order[index], order[swapWith]] = [order[swapWith], order[index]]
    updateSettings({ tabOrder: order })
  }

  function toggleTabHidden(tab: CustomizableTab) {
    const hidden = settings.hiddenTabs.includes(tab)
      ? settings.hiddenTabs.filter((t) => t !== tab)
      : [...settings.hiddenTabs, tab]
    updateSettings({ hiddenTabs: hidden })
  }

  // Where the phone bar ends in the order: just after its last visible tab.
  let seen = 0
  let barEnd = -1
  settings.tabOrder.forEach((t, i) => {
    if (settings.hiddenTabs.includes(t)) return
    seen += 1
    if (seen === PHONE_BAR_TABS) barEnd = i
  })
  const barRows = barEnd < 0 ? settings.tabOrder.length : barEnd + 1

  // Both groups are one order: the drag measures rows across the two lists.
  const tabListsRef = useRef<HTMLDivElement>(null)
  const { handleProps, rowProps } = useDragReorder({
    listRef: tabListsRef,
    count: settings.tabOrder.length,
    onDrop: (from, to) => {
      const order = [...settings.tabOrder]
      const [moved] = order.splice(from, 1)
      order.splice(to, 0, moved)
      updateSettings({ tabOrder: order })
    },
    orderKey: settings.tabOrder,
  })

  function renderRow(t: CustomizableTab, i: number) {
    const drag = rowProps(i)
    return (
      <li
        key={t}
        className={`tab-order-row ${drag.className}`}
        data-reorder-index={drag['data-reorder-index']}
        style={drag.style}
      >
        <button
          type="button"
          className="queue-drag-handle"
          data-tab-handle={t}
          aria-label={`Reorder ${TAB_LABELS[t]}`}
          title="Drag to reorder, or use the Up and Down arrow keys"
          {...handleProps(i)}
          onKeyDown={(e) => {
            const direction = e.key === 'ArrowUp' ? -1 : e.key === 'ArrowDown' ? 1 : 0
            if (!direction) return
            e.preventDefault()
            moveTab(t, direction)
          }}
        >
          <DragHandleIcon />
        </button>
        <span className="tab-order-label">{TAB_LABELS[t]}</span>
        <Switch
          plain
          className="tab-order-visible"
          checked={!settings.hiddenTabs.includes(t)}
          onChange={() => toggleTabHidden(t)}
        >
          Visible
        </Switch>
      </li>
    )
  }

  const barTabs = settings.tabOrder.slice(0, barRows)
  const moreTabs = settings.tabOrder.slice(barRows)

  return (
    <SettingsCard
      id="settings-navigation"
      title="Navigation"
      helpText="Reorder tabs or hide the ones you do not use. Home stays first, and hidden tabs can be restored here."
    >
      <p className="settings-hint">
        Drag tabs to reorder them, or hide them. Home always stays first. On a phone, the top three visible tabs sit
        beside Home in the bottom bar and everything else goes under More: move a tab up into the
        top three to put it on the bar.
      </p>
      <h3 className="settings-subhead" id="settings-nav-bar">Phone bottom bar</h3>
      <div ref={tabListsRef}>
      <ul className="tab-order-list" aria-labelledby="settings-nav-bar">
        {barTabs.map((t, i) => renderRow(t, i))}
      </ul>
      {moreTabs.length > 0 && (
        <>
          <h3 className="settings-subhead" id="settings-nav-more">Under More on a phone</h3>
          <ul className="tab-order-list" aria-labelledby="settings-nav-more">
            {moreTabs.map((t, i) => renderRow(t, barRows + i))}
          </ul>
        </>
      )}
      </div>
    </SettingsCard>
  )
}

function DiscoveryBody() {
  const active = useContext(CardActiveContext)
  const [savedDiscoverySettings, setDiscoverySettings] = useState<DiscoverySettings | null>(null)
  const [discoverySettings, showDiscoverySettings] = useOptimistic(savedDiscoverySettings)
  const [discoverySettingsMessage, setDiscoverySettingsMessage] = useState<Notice | null>(null)
  const [savedOnlineRecommendations, setOnlineRecommendations] = useState<boolean | null>(null)
  const [onlineRecommendations, showOnlineRecommendations] = useOptimistic(savedOnlineRecommendations)
  const [privacyMessage, setPrivacyMessage] = useState<Notice | null>(null)

  const discovery = useLazyLoad(active, () => api.getDiscoverySettings().then(setDiscoverySettings))
  const privacy = useLazyLoad(active, () =>
    api.getPrivacySettings().then((value) => setOnlineRecommendations(value.online_recommendations)),
  )

  // A switch shows the new value at once and only while it is being saved:
  // once the server has taken it the real state has it, and if the server
  // refuses, the switch goes back by itself, so it never says something the
  // account is not set to.
  function saveDiscoverySettings(next: DiscoverySettings) {
    setDiscoverySettingsMessage(null)
    startTransition(async () => {
      showDiscoverySettings(next)
      try {
        await api.updateDiscoverySettings(next)
        startTransition(() => setDiscoverySettings(next))
      } catch (e) {
        setDiscoverySettingsMessage({ text: e instanceof ApiError ? e.message : 'Could not save.', error: true })
      }
    })
  }

  function saveOnlineRecommendations(next: boolean) {
    setPrivacyMessage(null)
    startTransition(async () => {
      showOnlineRecommendations(next)
      try {
        const saved = await api.setPrivacySettings({ online_recommendations: next })
        startTransition(() => setOnlineRecommendations(saved.online_recommendations))
      } catch (e) {
        setPrivacyMessage({ text: e instanceof ApiError ? e.message : 'Could not save.', error: true })
      }
    })
  }

  return (
    <>
      <p className="settings-hint">
        "Recommended" and "Discover new artists" on Home stay put until refreshed - manually,
        or automatically once a day at the time below.
      </p>
      {discovery.status !== 'ready' && discovery.status !== 'error' && <SectionLoading what="discovery settings" />}
      {discovery.status === 'error' && (
        <SectionError text="Could not load the discovery settings." onRetry={discovery.retry} />
      )}
      {discoverySettings && (
        <>
          <div className="settings-row">
            <span>Auto-refresh daily</span>
            <Switch
              plain
              checked={discoverySettings.auto_refresh_enabled}
              onChange={(on) => saveDiscoverySettings({ ...discoverySettings, auto_refresh_enabled: on })}
            >
              Enabled
            </Switch>
          </div>
          <div className="settings-row">
            <span id="settings-refresh-time-label">Refresh time</span>
            <SelectMenu
              label="Refresh time"
              labelledBy="settings-refresh-time-label"
              value={discoverySettings.auto_refresh_hour}
              disabled={!discoverySettings.auto_refresh_enabled}
              options={HOUR_OPTIONS.map((h) => ({ value: h, label: formatHour(h) }))}
              onChange={(hour) => saveDiscoverySettings({ ...discoverySettings, auto_refresh_hour: hour })}
            />
          </div>
        </>
      )}
      <SettingsNotice notice={discoverySettingsMessage} />
      {privacy.status === 'error' && (
        <SectionError text="Could not load the ListenBrainz recommendations setting." onRetry={privacy.retry} />
      )}
      {onlineRecommendations != null && (
        <>
          <div className="settings-row">
            <span id="settings-online-recommendations-label">ListenBrainz recommendations</span>
            <Switch
              plain
              aria-labelledby="settings-online-recommendations-label"
              aria-describedby="settings-online-recommendations-hint"
              checked={onlineRecommendations}
              onChange={saveOnlineRecommendations}
            >
              Use ListenBrainz to find similar music for what I play
            </Switch>
          </div>
          <p className="settings-hint" id="settings-online-recommendations-hint">
            This asks ListenBrainz about the tracks and artists you play (by their MusicBrainz IDs). Turned off,
            nothing from ListenBrainz is used for you: no similar artists or songs in mixes and Discovery, no
            Explore suggestions, and no listener counts, similar artists or top tracks looked up in info pages.
            Info pages and the art picker still use MusicBrainz, Wikipedia and Wikimedia Commons.
          </p>
          <SettingsNotice notice={privacyMessage} />
        </>
      )}
      <HomeSectionsSetting />
    </>
  )
}

/** Which of Home's sections show, and in what order (the editor Home's own
 * Customise dialog uses). Tucked away until asked for. */
function HomeSectionsSetting() {
  const { settings, updateSettings } = useSettings()
  return (
    <details className="settings-disclosure">
      <summary>Customise Home sections</summary>
      <HomeSectionsEditor
        value={settings.homeSections}
        onChange={(homeSections) => updateSettings({ homeSections })}
      />
    </details>
  )
}

// A ListenBrainz user token is a UUID; the server refuses anything else.
const LISTENBRAINZ_TOKEN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

function ScrobblingBody() {
  const active = useContext(CardActiveContext)
  const confirm = useConfirm()
  const { user, setUser } = useAuth()
  const [integrations, setIntegrations] = useState<Integration[]>([])
  const [token, setToken] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [connecting, setConnecting] = useState(false)
  // Where focus goes once the row has swapped controls (Connect -> Disconnect
  // and back): the swapped-out control is gone, so focus would fall to the page.
  const focusAfter = useRef<'disconnect' | 'token' | null>(null)
  const disconnectRef = useRef<HTMLButtonElement>(null)
  const tokenRef = useRef<HTMLInputElement>(null)
  const { status, retry } = useLazyLoad(active, () => api.getIntegrations().then(setIntegrations))
  const notices = user?.dismissed_notices ?? []
  const showLastfmNotice = notices.includes('lastfm_was_linked') && !notices.includes('lastfm_removed')

  useEffect(() => {
    if (focusAfter.current === 'disconnect') disconnectRef.current?.focus()
    if (focusAfter.current === 'token') tokenRef.current?.focus()
    focusAfter.current = null
  }, [integrations])

  async function dismissLastfmNotice() {
    try {
      setUser(await api.dismissNotice('lastfm_removed'))
    } catch {
      setError('Could not hide the notice.')
    }
  }

  async function refresh() {
    try {
      setIntegrations(await api.getIntegrations())
    } catch {
      setError('Could not refresh the ListenBrainz status.')
    }
  }

  async function connect() {
    setError(null)
    const value = token.trim()
    if (!value) {
      setError('Paste your ListenBrainz user token.')
      return
    }
    if (!LISTENBRAINZ_TOKEN.test(value)) {
      setError('That is not a ListenBrainz user token. Paste the user token from listenbrainz.org/settings - it looks like 1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d.')
      return
    }
    setConnecting(true)
    try {
      await api.connectListenBrainz(value)
      setToken('')
      focusAfter.current = 'disconnect'
      await refresh()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not connect to ListenBrainz.')
    } finally {
      setConnecting(false)
    }
  }

  async function disconnect() {
    setError(null)
    if (!(await confirm({
      title: 'Disconnect ListenBrainz',
      body: 'Disconnect your ListenBrainz account?',
      confirmLabel: 'Disconnect',
      danger: true,
    }))) return
    try {
      await api.disconnectListenBrainz()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not disconnect ListenBrainz.')
      return
    }
    focusAfter.current = 'token'
    await refresh()
  }

  const listenbrainz = integrations.find((i) => i.provider === 'listenbrainz')

  return (
    <>
      <p className="settings-hint">
        Link your own account to have plays pushed there automatically.
      </p>
      {showLastfmNotice && (
        <div className="notice-box" role="note">
          <p>
            Last.fm was removed (its API now needs a paid subscription). Scrobbling now goes to ListenBrainz -
            link it below.
          </p>
          <button type="button" onClick={() => void dismissLastfmNotice()}>Got it</button>
        </div>
      )}
      {status !== 'ready' && status !== 'error' && <SectionLoading what="ListenBrainz status" />}
      {status === 'error' && <SectionError text="Could not load the ListenBrainz status." onRetry={retry} />}
      {status === 'ready' && (
        <div className="integration-row" data-provider="listenbrainz">
          <div role="status" aria-live="polite">
            <strong>ListenBrainz</strong>
            {listenbrainz?.linked && (
              <span className="badge">
                connected{listenbrainz.external_username ? ` as ${listenbrainz.external_username}` : ''}
              </span>
            )}
          </div>
          {listenbrainz?.linked ? (
            <button ref={disconnectRef} onClick={disconnect}>Disconnect</button>
          ) : (
            <form
              className="integration-connect"
              onSubmit={(e) => {
                e.preventDefault()
                void connect()
              }}
            >
              {listenbrainz?.needs_reconnect && (
                <p className="settings-hint" role="status">
                  ListenBrainz no longer accepts your token - paste a new one.
                </p>
              )}
              <p className="settings-hint">
                Copy your user token from{' '}
                <a href="https://listenbrainz.org/settings/" target="_blank" rel="noreferrer">listenbrainz.org/settings</a>
                {' '}and paste it here. It is stored encrypted and only used to send your plays and to fetch artists' top tracks, which ListenBrainz no longer shares without a token.
              </p>
              <PasswordInput
                ref={tokenRef}
                secretName="token"
                aria-label="ListenBrainz user token"
                placeholder="ListenBrainz user token"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                // Not the account password: keep password managers from
                // filling or saving it.
                autoComplete="new-password"
                spellCheck={false}
                autoCapitalize="off"
                autoCorrect="off"
                data-1p-ignore=""
                data-lpignore="true"
              />
              <button type="submit" disabled={connecting}>
                {connecting ? 'Connecting…' : 'Connect ListenBrainz'}
              </button>
            </form>
          )}
        </div>
      )}
      {error && <p className="login-error" role="alert">{error}</p>}
    </>
  )
}

/** Moves to a section: the page scrolls to it and focus goes to it, so the
 * next Tab starts inside it and a screen reader says where it landed. */
function goToSection(root: RefObject<HTMLElement | null>, id: string) {
  const el = root.current?.querySelector<HTMLElement>(`#${id}`)
  if (!el) return
  el.scrollIntoView({ block: 'start' })
  el.focus({ preventScroll: true })
}

/** The Settings page: every section as a card in one column, with a row of
 * chips at the top that jump to them. `#/settings/<section>` opens the page
 * already scrolled to that section. */
export function SettingsPanel({
  navState,
  onSectionChange,
  onInstall,
}: {
  navState: NavState
  /** Called with a section id when a chip is chosen (the address follows). */
  onSectionChange: (id: string) => void
  /** Present while the app can still be installed. */
  onInstall?: () => void
}) {
  const rootRef = useRef<HTMLDivElement>(null)
  const handledNav = useRef<NavState | null>(null)
  const [current, setCurrent] = useState(navState.settingsSection ?? SETTINGS_SECTIONS[0].id)

  // A new address with a section (the gear menu, a chip, a link) scrolls to
  // it - once per address, so showing the tab again does not jump back. Waits
  // a frame because the shell resets the scroll position after a tab change.
  useEffect(() => {
    if (handledNav.current === navState) return
    const id = navState.settingsSection
    const frame = requestAnimationFrame(() => {
      handledNav.current = navState
      if (!id) return
      setCurrent(id)
      goToSection(rootRef, id)
    })
    return () => cancelAnimationFrame(frame)
  }, [navState])

  return (
    <div className="settings-page" ref={rootRef}>
      <nav className="settings-chips" aria-label="Settings sections">
        {SETTINGS_SECTIONS.map((section) => (
          <button
            key={section.id}
            type="button"
            className="chip"
            aria-current={current === section.id ? 'true' : undefined}
            onClick={() => {
              setCurrent(section.id)
              onSectionChange(section.id)
              goToSection(rootRef, section.id)
            }}
          >
            {section.label}
          </button>
        ))}
      </nav>
      <div className="settings-column">
        <AccountSection />
        <PlaybackSection />
        {isNativeApp() && (
          <SettingsCard
            id="settings-downloads"
            title="Downloads"
            helpText="Songs and videos saved to this device play with no connection. Choose the quality they are saved at, and remove what you no longer need."
          >
            <DownloadsSettings />
          </SettingsCard>
        )}
        <AppearanceSection />
        <NavigationSection />
        <SettingsCard
          id="settings-discovery"
      title="Discovery"
      helpText="Control whether Home recommendations and artist discovery refresh automatically each day, and choose the refresh time."
        >
          <DiscoveryBody />
        </SettingsCard>
        <SettingsCard
          id="settings-scrobbling"
      title="Scrobbling"
      helpText="Connect or disconnect ListenBrainz. When connected, eligible plays are sent to your account automatically."
        >
          <ScrobblingBody />
        </SettingsCard>
        <AboutSection onInstall={onInstall} />
      </div>
    </div>
  )
}
