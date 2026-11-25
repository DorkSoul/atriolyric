import { Activity, Fragment, Suspense, useEffect, useId, useRef, useState, type ComponentProps, type ReactNode } from 'react'
import { api, onLibraryChange } from './api'
import { AuthProvider, useAuth } from './AuthContext'
import { useAlert } from './dialogs'
import { ConfirmProvider } from './components/ConfirmDialog'
import { ErrorBoundary } from './components/ErrorBoundary'
import { OverlayBoundary } from './components/OverlayBoundary'
import { LibraryInfoIcon, SettingsIcon } from './components/Icons'
import { HomeView } from './components/HomeView'
import { LoginPage } from './components/LoginPage'
import { InfoModalHost } from './components/InfoModalHost'
import { LoadingStatus } from './components/LoadingStatus'
import { LoadingState } from './components/ErrorState'
import { lazyView, type LazyView } from './lazyView'
import { onIdle } from './idle'
import { Menu, MenuItem, MenuSeparator } from './components/Menu'
import { ArtPickerHost } from './artPicker'
import { setShowMissing } from './showMissing'
import { PlayerBar } from './components/PlayerBar'
import { SidePanel } from './components/SidePanel'
import { TopBarDevicePicker } from './components/DevicePicker'
import { SetupPage } from './components/SetupPage'
import { SETTINGS_SECTIONS } from './settingsSections'
import { ArtScrollRootProvider } from './artScrollRoot'
import { ViewActiveContext } from './viewActive'
import { HeaderActionsPortalProvider, HeaderControlsPortalProvider, TopBarTitlePortalProvider } from './headerControlsPortal'
import { Sidebar } from './components/Sidebar'
import { TopBar } from './components/TopBar'
import { PHONE_BAR_TABS, PhoneTabBar } from './components/PhoneTabBar'
import { MoreSheet } from './components/MoreSheet'
import { useIsDesktop } from './useIsDesktop'
import { formatLastScan } from './libraryInfoFormat'
import { menuTriggerProps } from './menuTrigger'
import { type NavState, type Tab, hashToNav, navToHash } from './nav'
import { CLOSE_NOW_PLAYING_EVENT, NOW_PLAYING_HASH, PlayerProvider, usePlayer } from './PlayerContext'
import { NOW_PLAYING_PAGE_HOST_ID, useNowPlayingAsPage } from './components/nowPlayingLazy'
import { RenderProbe } from './RenderProbe'
import { SettingsProvider, useSettings } from './SettingsContext'
import { debugLog } from './clientDebugLog'
import { ToastProvider, useRegisterPlaylistOpener, useToast } from './ToastContext'
import UpdatePrompt from './components/UpdatePrompt'
import AppUpdatePrompt from './native/AppUpdatePrompt'
import OfflineNotice from './native/OfflineNotice'
import { isNativeApp } from './native/bridge'
import type { LibraryRootSummary, LibraryStats, Playlist, PlaySource } from './types'

// Every tab but Home loads its code on demand, so the first download is the
// shell, the player and Home. Each view is shown through a <Suspense> inside
// the page area with the views' usual loading line while its file arrives:
// the top bar, the sidebar and the player stay as they are.
const AdminPanel = lazyView(() => import('./components/AdminPanel').then((m) => m.AdminPanel))
const SettingsPanel = lazyView(() => import('./components/SettingsPanel').then((m) => m.SettingsPanel))
const WantListView = lazyView(() => import('./components/WantListView').then((m) => m.WantListView))
const LibraryInfoView = lazyView(() => import('./components/LibraryInfoView').then((m) => m.LibraryInfoView))
const AlbumsView = lazyView(() => import('./components/AlbumsView').then((m) => m.AlbumsView))
const ArtistsView = lazyView(() => import('./components/ArtistsView').then((m) => m.ArtistsView))
const FilesView = lazyView(() => import('./components/FilesView').then((m) => m.FilesView))
const PlaylistsView = lazyView(() => import('./components/PlaylistsView').then((m) => m.PlaylistsView))
const RadioView = lazyView(() => import('./components/RadioView').then((m) => m.RadioView))
const RecentTracksView = lazyView(() => import('./components/RecentTracksView').then((m) => m.RecentTracksView))
const SongsView = lazyView(() => import('./components/SongsView').then((m) => m.SongsView))
const VideoFoldersView = lazyView(() => import('./components/VideoFoldersView').then((m) => m.VideoFoldersView))

/** Each lazily loaded tab's view, to fetch its code ahead of a visit. */
const TAB_VIEWS: Partial<Record<Tab, LazyView<never>>> = {
  songs: SongsView as LazyView<never>,
  albums: AlbumsView as LazyView<never>,
  artists: ArtistsView as LazyView<never>,
  'library-info': LibraryInfoView as LazyView<never>,
  'recent-played': RecentTracksView as LazyView<never>,
  videos: VideoFoldersView as LazyView<never>,
  files: FilesView as LazyView<never>,
  radio: RadioView as LazyView<never>,
  playlists: PlaylistsView as LazyView<never>,
  'want-list': WantListView as LazyView<never>,
  admin: AdminPanel as LazyView<never>,
  settings: SettingsPanel as LazyView<never>,
}

// The tab the page opens on starts downloading now, alongside the sign-in
// check, rather than after it.
TAB_VIEWS[hashToNav(window.location.hash).tab]?.preload()

const TAB_LABELS: Record<Tab, string> = {
  home: 'Home',
  songs: 'Songs',
  albums: 'Albums',
  artists: 'Artists',
  'library-info': 'Library info',
  'recent-played': 'Recently played',
  videos: 'Videos',
  files: 'Files',
  radio: 'Radio',
  playlists: 'Playlists',
  'now-playing': 'Now playing',
  'want-list': 'Want list',
  admin: 'Admin',
  settings: 'Settings',
}

// Tabs that stay mounted (hidden, effects disconnected) once visited, so their
// lists, scroll position and open detail survive a visit elsewhere. Home,
// Albums and Artists are left out on purpose: they unmount when hidden,
// because the browser tests (app-*.spec.ts) read the page with
// class selectors that would also match those views' hidden copies. Their
// scroll position is restored by the settle loop below instead.
const KEEP_MOUNTED = new Set<Tab>([
  'songs',
  'recent-played',
  'playlists',
  'radio',
  'videos',
  'files',
  'want-list',
  'admin',
])

const ANDROID_PULL_REFRESH = /Android/i.test(navigator.userAgent)
const PULL_REFRESH_TRIGGER_PX = 80
// How long (ms) a restored scroll position is held while a view lays out.
const SCROLL_SETTLE_MS = 1500

type InstallPromptEvent = Event & {
  prompt: () => Promise<void>
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>
}

/** The page title: the view (and the album or artist open in it); while a
 * track plays here it leads with that, so a background tab says what is on.
 * Its own component, so a player change re-renders this and not the shell. */
function DocumentTitle({ viewName }: { viewName: string }) {
  const { current: currentTrack, isPlaying, isActiveDevice } = usePlayer()
  const nowPlayingTitle =
    currentTrack && isPlaying && isActiveDevice
      ? `▶ ${currentTrack.title}${currentTrack.artist ? ` – ${currentTrack.artist}` : ''} · Atriolyric`
      : null
  const documentTitle = nowPlayingTitle ?? `${viewName} – Atriolyric`
  useEffect(() => {
    document.title = documentTitle
  }, [documentTitle])
  return null
}

/** The top bar, titled "Now playing" while that page is open on a desktop.
 * Its own component, so a player change re-renders this and not the shell. */
function ShellTopBar(props: ComponentProps<typeof TopBar>) {
  const asPage = useNowPlayingAsPage()
  return asPage ? <TopBar {...props} title="Now playing" count={undefined} titleHidden={false} /> : <TopBar {...props} />
}

/** The Now playing tab is Now Playing itself (PlayerContext): picking it
 * opens it, and it is the current tab while open. Their own components, so a
 * player change re-renders these and not the shell. */
function useNowPlayingTab(current: Tab, onSelect: (tab: Tab) => void) {
  const { nowPlayingOpen, openNowPlaying } = usePlayer()
  return {
    current: nowPlayingOpen ? ('now-playing' as Tab) : current,
    onSelect: (tab: Tab) => (tab === 'now-playing' ? openNowPlaying() : onSelect(tab)),
    nowPlayingOpen,
  }
}

function ShellSidebar(props: ComponentProps<typeof Sidebar>) {
  const { current, onSelect, nowPlayingOpen } = useNowPlayingTab(props.current, props.onSelect)
  const extraItems = props.extraItems?.map((item) => ({ ...item, current: item.current && !nowPlayingOpen }))
  return <Sidebar {...props} current={current} onSelect={onSelect} extraItems={extraItems} />
}

function ShellPhoneTabBar(props: ComponentProps<typeof PhoneTabBar>) {
  const { current, onSelect } = useNowPlayingTab(props.current, props.onSelect)
  return (
    <PhoneTabBar {...props} current={current} onSelect={onSelect} currentInMore={!props.tabs.includes(current)} />
  )
}

function ShellMoreSheet(props: ComponentProps<typeof MoreSheet>) {
  const { current, onSelect } = useNowPlayingTab(props.current, props.onSelect)
  return <MoreSheet {...props} current={current} onSelect={onSelect} />
}

/** The player bar with a boundary of its own: a failure in it (or in Now
 * Playing, which it hosts) must not reach the root boundary, which would
 * replace the app and stop the music. It is drawn again at the next track. */
function PlayerBarBoundary({ children }: { children: ReactNode }) {
  const { current } = usePlayer()
  return (
    <OverlayBoundary what="the player controls" resetKey={String(current?.id ?? '')}>
      {children}
    </OverlayBoundary>
  )
}

/** What a page's history entry carries (see `navigate`). */
interface PageEntryState {
  /** The address of the entry before this one, when the app pushed it. */
  prev?: string
  playlistBackLabel?: string
}

/** The current entry's state, if it is a page's (not a dialog's or Now
 * Playing's, and not an entry from before the app wrote any). */
function pageEntryState(): PageEntryState {
  const state: unknown = window.history.state
  if (typeof state !== 'object' || state === null) return {}
  const { prev, playlistBackLabel } = state as Record<string, unknown>
  return {
    prev: typeof prev === 'string' ? prev : undefined,
    playlistBackLabel: typeof playlistBackLabel === 'string' ? playlistBackLabel : undefined,
  }
}

function AppInner() {
  const { user, logout } = useAuth()
  const alert = useAlert()
  const { showToast } = useToast()
  const { settings } = useSettings()
  // Now playing opens over the page under it (Home after a refresh there).
  const [nav, setNav] = useState<NavState>(() => {
    const initial = hashToNav(window.location.hash)
    return initial.tab === 'now-playing' ? { tab: 'home' } : initial
  })
  const [search, setSearch] = useState('')
  const [prefillSearch, setPrefillSearch] = useState('')
  const [genre, setGenre] = useState('')
  const [tag, setTag] = useState('')
  const [genres, setGenres] = useState<string[]>([])
  const [playlists, setPlaylists] = useState<Playlist[]>([])
  const [accessibleRoots, setAccessibleRoots] = useState<LibraryRootSummary[]>([])
  const [settingsMenuOpen, setSettingsMenuOpen] = useState(false)
  const [installPrompt, setInstallPrompt] = useState<InstallPromptEvent | null>(null)
  const [isPwaInstalled, setIsPwaInstalled] = useState(() =>
    window.matchMedia('(display-mode: standalone)').matches ||
    (window.navigator as Navigator & { standalone?: boolean }).standalone === true,
  )
  const [playlistBackLabel, setPlaylistBackLabel] = useState(
    () => window.history.state?.playlistBackLabel ?? 'Playlists',
  )
  const settingsMenuToggleRef = useRef<HTMLButtonElement>(null)
  const settingsMenuId = useId()
  // Mount point the active Albums/Artists view portals its sort/view-mode
  // icons into (see headerControlsPortal.tsx) - lives here so those
  // controls always render in the same header spot regardless of which
  // view owns their state.
  const [headerControlsNode, setHeaderControlsNode] = useState<HTMLDivElement | null>(null)
  // The top bar's title slot: Home's search, a detail page's back link.
  const [topBarTitleNode, setTopBarTitleNode] = useState<HTMLDivElement | null>(null)
  // The header actions slot, left of the device picker: Home's Customise.
  const [headerActionsNode, setHeaderActionsNode] = useState<HTMLDivElement | null>(null)
  const topBarHeadingRef = useRef<HTMLHeadingElement>(null)
  const [moreOpen, setMoreOpen] = useState(false)
  // The sidebar counts (and the top bar's count): the light nav-counts call,
  // asked once the page is idle and again after anything changes the
  // library; the Library info screen hands over its fresher copy when it
  // loads (with the last scan time for its top bar). A failed request just
  // leaves the counts out.
  const [navCounts, setNavCounts] = useState<LibraryStats['nav_counts'] | null>(null)
  const [libraryStats, setLibraryStats] = useState<LibraryStats | null>(null)
  const isDesktop = useIsDesktop()
  const mainScrollRef = useRef<HTMLElement>(null)
  const pullIndicatorRef = useRef<HTMLDivElement>(null)
  const tab = nav.tab
  // The tabs opened so far (mounted from then on), the nav state each last
  // had, and a counter that remounts one on purpose (a prefilled search).
  // Derived while rendering, so the view for a new tab is in this very render.
  const [visitedTabs, setVisitedTabs] = useState<Tab[]>(() => (KEEP_MOUNTED.has(tab) ? [tab] : []))
  const [tabNavs, setTabNavs] = useState<Partial<Record<Tab, NavState>>>(() => ({ [tab]: nav }))
  const [viewKeys, setViewKeys] = useState<Partial<Record<Tab, number>>>({})
  if (KEEP_MOUNTED.has(tab) && !visitedTabs.includes(tab)) setVisitedTabs([...visitedTabs, tab])
  if (tabNavs[tab] !== nav) setTabNavs({ ...tabNavs, [tab]: nav })
  // Tabs outside KEEP_MOUNTED are mounted only while showing.
  const mountedTabs = visitedTabs.includes(tab) ? visitedTabs : [...visitedTabs, tab]


  // Pull to refresh (Android browsers only). Native listeners on the <main>
  // element, with touchmove non-passive so the pull can be claimed: React
  // bubbles synthetic touch events up through portals, so handlers on the
  // element would also hear a drag inside a modal or menu opened from a view
  // and reload the page under it. A native listener only hears touches whose
  // target is really inside <main>. The indicator is moved by hand, not by
  // state, so a drag does not re-render the app on every touchmove.
  useEffect(() => {
    const main = mainScrollRef.current
    const indicator = pullIndicatorRef.current
    if (!ANDROID_PULL_REFRESH || !main || !indicator) return
    const mainEl: HTMLElement = main
    const indicatorEl: HTMLElement = indicator
    let startY: number | null = null
    let shown = 0

    function show(distance: number) {
      shown = distance
      if (distance <= 0) {
        indicatorEl.hidden = true
        return
      }
      indicatorEl.hidden = false
      indicatorEl.style.transform = `translate(-50%, ${distance}px)`
      const text = distance >= PULL_REFRESH_TRIGGER_PX * 0.5 ? 'Release to refresh' : 'Pull to refresh'
      if (indicatorEl.textContent !== text) indicatorEl.textContent = text
    }

    /** True when the touch started inside something that scrolls and is not at
     * its top: a drag there scrolls that, it is not a pull. */
    function insideScrolledScroller(target: EventTarget | null): boolean {
      for (let el = target instanceof Element ? target : null; el && el !== mainEl; el = el.parentElement) {
        const overflowY = getComputedStyle(el).overflowY
        if ((overflowY === 'auto' || overflowY === 'scroll') && el.scrollHeight > el.clientHeight && el.scrollTop > 0) {
          return true
        }
      }
      return false
    }

    function onStart(event: TouchEvent) {
      startY = null
      if (event.touches.length !== 1 || mainEl.scrollTop > 0) return
      if (!(event.target instanceof Node) || !mainEl.contains(event.target)) return
      if (insideScrolledScroller(event.target)) return
      startY = event.touches[0].clientY
    }
    function onMove(event: TouchEvent) {
      if (startY == null || event.touches.length !== 1) return
      const distance = event.touches[0].clientY - startY
      if (distance <= 0) {
        show(0)
        return
      }
      // Claim the pull, so the browser does not scroll or overscroll as well.
      if (event.cancelable) event.preventDefault()
      show(Math.min(distance * 0.5, 60))
    }
    function onEnd() {
      const shouldRefresh = startY != null && shown >= PULL_REFRESH_TRIGGER_PX * 0.5
      const distance = shown
      startY = null
      show(0)
      if (shouldRefresh) {
        debugLog('life.pull_refresh_reload', { distance })
        window.location.reload()
      }
    }
    mainEl.addEventListener('touchstart', onStart, { passive: true })
    mainEl.addEventListener('touchmove', onMove, { passive: false })
    mainEl.addEventListener('touchend', onEnd)
    mainEl.addEventListener('touchcancel', onEnd)
    return () => {
      mainEl.removeEventListener('touchstart', onStart)
      mainEl.removeEventListener('touchmove', onMove)
      mainEl.removeEventListener('touchend', onEnd)
      mainEl.removeEventListener('touchcancel', onEnd)
    }
  }, [])

  useEffect(() => {
    if (isPwaInstalled) return

    function handleBeforeInstallPrompt(event: Event) {
      event.preventDefault()
      setInstallPrompt(event as InstallPromptEvent)
    }
    function handleAppInstalled() {
      setInstallPrompt(null)
      setIsPwaInstalled(true)
    }
    window.addEventListener('beforeinstallprompt', handleBeforeInstallPrompt)
    window.addEventListener('appinstalled', handleAppInstalled)
    return () => {
      window.removeEventListener('beforeinstallprompt', handleBeforeInstallPrompt)
      window.removeEventListener('appinstalled', handleAppInstalled)
    }
  }, [isPwaInstalled])

  async function installPwa() {
    if (!installPrompt) {
      // Asked of the browser rather than read from its name: only browsers
      // with the install prompt (Chromium's) have the event at all.
      const browser = !window.isSecureContext
        ? 'PWA installation requires an HTTPS connection (or localhost).'
        : !('onbeforeinstallprompt' in window)
          ? 'This browser does not support the automatic install prompt; use the browser menu instead.'
          : 'This browser does not provide the automatic install prompt; use its browser menu instead.'
      await alert({
        title: 'Install as PWA',
        body: `Atriolyric could not open the install dialog. ${browser}`,
      })
      return
    }
    await installPrompt.prompt()
    const choice = await installPrompt.userChoice
    setInstallPrompt(null)
    if (choice.outcome === 'dismissed') {
      await alert({
        title: 'Install as PWA',
        body: 'Atriolyric was not installed. You can try again from the settings menu.',
      })
    }
  }

  // Scroll-position memory: keyed by hash. Landing on a hash that's been
  // visited before (browser back/forward, an in-app "<- Albums"-style back
  // button, or a breadcrumb click up to an ancestor folder) restores
  // exactly where that list was scrolled to. Landing on a hash for the
  // first time starts at the top. The one exception is picking a tab
  // directly from the tab bar/dropdown, which always starts at the top
  // even if that tab was visited before - that's forced via `forceTop`.
  const scrollPositionsRef = useRef<Map<string, number>>(new Map())
  const navRef = useRef(nav)
  const forceTopRef = useRef(false)
  // Set by a replace-only navigate: the next scroll restore is skipped.
  const keepScrollRef = useRef(false)
  // Set with it: a same-screen URL change (the user typing in Home's search
  // box) must not pull focus out of what they are typing in.
  const skipFocusRef = useRef(false)
  const firstNavRef = useRef(true)
  // The position the last restore aimed for. A view that fills in
  // asynchronously (Home) is too short to scroll that far when the restore
  // runs, so it calls restorePendingScroll once its content is there.
  const pendingScrollRef = useRef(0)
  useEffect(() => {
    navRef.current = nav
  }, [nav])

  // Settings is not a tab: it opens from the icon next to the username.
  const visibleNavTabs: Tab[] = [
    'home',
    ...settings.tabOrder.filter((t) => !settings.hiddenTabs.includes(t)),
    ...(user?.is_admin ? (['admin'] as Tab[]) : []),
  ]
  // The same without Admin, which the sidebar and the More sheet list after a
  // divider. A phone's bottom bar has Home and the next PHONE_BAR_TABS of
  // them; the rest are under More.
  const navTabs: Tab[] = visibleNavTabs.filter((t) => t !== 'admin')
  const phoneBarTabs = navTabs.slice(0, PHONE_BAR_TABS + 1)
  const moreTabs = navTabs.slice(PHONE_BAR_TABS + 1)

  function navigate(
    next: NavState,
    options?: { forceTop?: boolean; playlistBackLabel?: string; replace?: boolean; replaceEntry?: boolean },
  ) {
    if (options?.replace) {
      // Same screen, new URL (Home's search text): no history entry, and
      // the scroll position stays where it is.
      keepScrollRef.current = true
      skipFocusRef.current = true
      setNav(next)
      window.history.replaceState(window.history.state, '', navToHash(next))
      return
    }
    setPrefillSearch('')
    scrollPositionsRef.current.set(navToHash(navRef.current), mainScrollRef.current?.scrollTop ?? 0)
    forceTopRef.current = options?.forceTop ?? false
    if (next.tab === 'playlists' && next.playlistId != null) {
      setPlaylistBackLabel(options?.playlistBackLabel ?? 'Playlists')
    }
    setNav(next)
    // Leaving the desktop Now Playing page (the sidebar stays usable beside
    // it): the new page takes Now Playing's history entry, so Back returns to
    // the page that was under it rather than to Now Playing's empty entry.
    const fromNowPlaying = window.history.state?.nowPlaying === true
    if (fromNowPlaying) window.dispatchEvent(new Event(CLOSE_NOW_PLAYING_EVENT))
    // `prev` is the address of the entry before this one, which goUp reads to
    // tell whether Back leads to the parent page. A pushed entry sits on the
    // one showing now; one that takes Now Playing's place sits on the page
    // Now Playing was opened over; one replaced in place (goUp) keeps what
    // it had.
    const state: PageEntryState = {
      prev: options?.replaceEntry
        ? pageEntryState().prev
        : fromNowPlaying
          ? navToHash(navRef.current)
          : window.location.hash,
    }
    if (options?.playlistBackLabel) state.playlistBackLabel = options.playlistBackLabel
    window.history[fromNowPlaying || options?.replaceEntry ? 'replaceState' : 'pushState'](state, '', navToHash(next))
  }

  /** A detail page's own back link ("← Albums"): up to its parent page.
   * When the entry before this one is the parent (the usual case: the page
   * was opened from it), this is the browser's Back, so the history is not
   * grown by a second copy of the parent - the phone's Back gesture then
   * leaves the parent instead of returning into the page just left. When it
   * is not (the page was opened by a link, a reload, from another tab), the
   * parent takes this entry's place, and the app is never left.
   * `isParent` widens what counts as the parent (the Want list with a search). */
  function goUp(parent: NavState, isParent?: (previous: NavState) => boolean) {
    const prev = pageEntryState().prev
    if (prev != null && (isParent ? isParent(hashToNav(prev)) : prev === navToHash(parent))) {
      window.history.back()
      return
    }
    navigate(parent, { replaceEntry: true })
  }

  /** A playlist's back link is named after where it was opened from (Home,
   * Now Playing's "Back"), and that page is the entry before it; otherwise,
   * and when there is no such entry (a link, a new tab), up to Playlists. */
  function leavePlaylist() {
    const state = pageEntryState()
    if (state.playlistBackLabel && state.prev != null) window.history.back()
    else goUp({ tab: 'playlists' })
  }

  function switchTab(next: Tab) {
    if (next === 'now-playing') return // ShellNav opens it (see below)
    if (next === 'files' && accessibleRoots.length === 1) {
      // With a single library path, open it directly. Resolve it
      // synchronously so entering Files is a single history step. With
      // several, the bare 'files' tab shows the list of library paths.
      navigate({ tab: 'files', filesRootId: accessibleRoots[0].id, filesPath: '' }, { forceTop: true })
    } else {
      navigate({ tab: next }, { forceTop: true })
    }
  }

  /** The gear menu's items land on a section of the Settings page. The
   * address gets its own history entry, so Back returns to where you were. */
  function openSettingsSection(sectionId: string) {
    setSettingsMenuOpen(false)
    navigate({ tab: 'settings', settingsSection: sectionId }, { forceTop: true })
  }

  function openMix(playlistId: number) {
    // Mixes are real playlists (see refresh_all_genre_mixes) - landing
    // on the actual mix's detail view (same nav.playlistId plumbing used
    // elsewhere) gives real visible feedback for what's now playing,
    // rather than a generic genre-filtered Songs tab that doesn't
    // necessarily match the mix's own (smaller, curated) track list.
    navigate({ tab: 'playlists', playlistId }, { playlistBackLabel: 'Home' })
  }

  useRegisterPlaylistOpener((playlistId) =>
    navigate({ tab: 'playlists', playlistId }, { forceTop: true }),
  )

  /** A genre or tag chip takes you to the Songs tab filtered to it. */
  function openSongsFilter(label: { genre?: string; tag?: string }) {
    setSearch('')
    setGenre(label.genre ?? '')
    setTag(label.tag ?? '')
    switchTab('songs')
  }

  /** Home's "View full search" link: open the tab with the search applied
   * (shown there as a removable "Search: …" chip - the tabs have no search
   * box of their own). Songs keeps its search in App state; the other tabs
   * mount fresh and read it as their initial value. */
  function viewFullSearch(target: 'artists' | 'albums' | 'songs' | 'playlists', q: string) {
    if (target === 'songs') {
      setSearch(q)
      setGenre('')
      setTag('')
      switchTab('songs')
      return
    }
    switchTab(target)
    setPrefillSearch(q)
    // That view may already be mounted (and keeps its search box); start it over
    // so it opens on this search.
    setViewKeys((keys) => ({ ...keys, [target]: (keys[target] ?? 0) + 1 }))
  }

  function openArtist(artist: string) {
    navigate({ tab: 'artists', browseArtist: artist }, { forceTop: true })
  }

  function openCatalogAlbum(releaseGroupMbid: string, artist: string) {
    navigate({ tab: 'want-list', want: { page: 'album', mbid: releaseGroupMbid, artist } }, { forceTop: true })
  }

  function openAlbum(artist: string | null, album: string) {
    navigate({ tab: 'albums', browseAlbum: { artist, album } }, { forceTop: true })
  }

  /** A "Recently played" card on Home takes you to the thing that was
   * played (the Songs tab with its filters restored, an artist, an album,
   * a playlist, or the Radio tab) rather than starting playback right
   * away - browsing there and pressing play is one extra click, but
   * landing straight in music you didn't ask to hear yet is worse.
   * Now Playing's "Playing from" link opens the queue's source the same
   * way (`backLabel` names where a playlist's Back returns to). */
  function openPlaySource(entry: PlaySource, backLabel = 'Home') {
    if (entry.kind === 'library') {
      const filters = (entry.payload.filters as { q?: string; genre?: string; tag?: string }) ?? {}
      setSearch(filters.q ?? '')
      setGenre(filters.genre ?? '')
      setTag(filters.tag ?? '')
      switchTab('songs')
    } else if (entry.kind === 'album') {
      const { artist, album } = entry.payload as { artist: string | null; album: string }
      navigate({ tab: 'albums', browseAlbum: { artist: artist ?? null, album } }, { forceTop: true })
    } else if (entry.kind === 'artist') {
      const { artist } = entry.payload as { artist: string }
      navigate({ tab: 'artists', browseArtist: artist }, { forceTop: true })
    } else if (entry.kind === 'playlist') {
      navigate(
        { tab: 'playlists', playlistId: entry.payload.playlist_id as number },
        { forceTop: true, playlistBackLabel: backLabel },
      )
    } else if (entry.kind === 'radio') {
      switchTab('radio')
    }
  }

  // The last successful playlists request: Home's Discovery look-up reads
  // it instead of asking again.
  const playlistsRequestRef = useRef<Promise<Playlist[]> | null>(null)

  async function refreshPlaylists() {
    // Shared with any other caller asking at the same moment (api.ts).
    const pending = api.listPlaylists()
    try {
      const list = await pending
      playlistsRequestRef.current = pending
      setPlaylists(list)
    } catch {
      // Callers do not handle a rejection (and Home awaits this before
      // navigating), so say it here rather than leave an empty list unexplained.
      showToast('Could not load your playlists.', 'error')
    }
  }

  /** App's playlists for Home: the answer already in, or the request on its
   * way (one request for both), or a new one if there is neither. */
  function loadPlaylists(): Promise<Playlist[]> {
    return playlistsRequestRef.current ?? api.listPlaylists()
  }

  // Start-up lookups: the playlists (shown on several tabs) at once, with a
  // message if they fail; the rest once the page is idle, with none.
  useEffect(() => {
    let live = true
    void refreshPlaylists()
    // The library paths only matter for opening Files (one path opens
    // directly), and the counts are decoration: neither competes with what
    // the first screen needs.
    const cancelRoots = onIdle(() => {
      api.listAccessibleRoots().then((value) => live && setAccessibleRoots(value), () => {})
    })
    const cancelCounts = onIdle(() => {
      api.navCounts().then((value) => live && setNavCounts(value), () => {})
    })
    return () => {
      live = false
      cancelRoots()
      cancelCounts()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Anything that changes the library (a scan, an edit, a playlist or want
  // list change, a radio station) changes the counts: ask again when idle.
  useEffect(() => {
    let live = true
    let cancel: (() => void) | null = null
    const unsubscribe = onLibraryChange(() => {
      cancel?.()
      cancel = onIdle(() => {
        cancel = null
        api.navCounts().then((value) => live && setNavCounts(value), () => {})
      })
    })
    return () => {
      live = false
      cancel?.()
      unsubscribe()
    }
  }, [])

  // The genre filter is on Songs only: its list is asked for the first time
  // Songs is shown (Albums and Artists ask for their own).
  const songsVisited = tab === 'songs' || visitedTabs.includes('songs')
  useEffect(() => {
    if (!songsVisited) return
    let live = true
    api.listGenres().then((value) => live && setGenres(value), () => {})
    return () => {
      live = false
    }
  }, [songsVisited])

  // After the first screen is up, fetch the other tabs' code while the page
  // is idle, in the order the sidebar lists them, one at a time - so a tab
  // switch rarely waits for a download.
  const preloadOrderRef = useRef<Tab[]>([])
  useEffect(() => {
    preloadOrderRef.current = [...navTabs, 'library-info', 'settings', ...(user?.is_admin ? (['admin'] as Tab[]) : [])]
  })
  useEffect(() => {
    let index = 0
    let cancel = onIdle(next, 4000)
    function next() {
      const order = preloadOrderRef.current
      while (index < order.length && !TAB_VIEWS[order[index]]) index += 1
      if (index >= order.length) return
      TAB_VIEWS[order[index]]?.preload()
      index += 1
      cancel = onIdle(next, 4000)
    }
    return () => cancel()
  }, [])

  useEffect(() => {
    if (window.location.hash.startsWith(NOW_PLAYING_HASH)) {
      // A refresh on Now playing: put the page it opens over (Home) under
      // its entry, so Back and its own Back button land there.
      window.history.replaceState(null, '', navToHash(nav))
      window.history.pushState({ nowPlaying: true }, '', NOW_PLAYING_HASH)
    } else if (!window.location.hash || window.location.hash.startsWith('#/recent-added')) {
      // No address yet, or a Recently added address (part of Library info):
      // replace it, so Back does not return to a page that redirects again.
      window.history.replaceState(window.history.state, '', navToHash(nav))
    }
    function onPopState() {
      // Forward onto Now Playing's entry: it opens over the current page.
      if (window.location.hash.startsWith(NOW_PLAYING_HASH)) return
      scrollPositionsRef.current.set(
        navToHash(navRef.current),
        mainScrollRef.current?.scrollTop ?? 0,
      )
      forceTopRef.current = false
      setPlaylistBackLabel(window.history.state?.playlistBackLabel ?? 'Playlists')
      const next = hashToNav(window.location.hash)
      setNav(next)
      if (window.location.hash.startsWith('#/recent-added')) {
        window.history.replaceState(window.history.state, '', navToHash(next))
      }
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Runs after every nav change (and after the resulting view has
  // committed): restores the remembered scroll position for this hash if
  // it's been visited before, unless this specific change was flagged to
  // always start at the top (a direct tab-bar/dropdown pick).
  //
  // The tab views stay mounted, so a tab that is shown again already has its
  // full height and the position sticks at once. A view that is still laying
  // out (a list that measures its real row height after the first paint) can
  // clamp it, so the target is re-applied for a few frames - until it holds,
  // or the user scrolls, whichever is first.
  useEffect(() => {
    const el = mainScrollRef.current
    const keepScroll = keepScrollRef.current
    keepScrollRef.current = false
    if (!el) return
    let frame = 0
    let stop = () => {}
    if (!keepScroll) {
      const target = forceTopRef.current ? 0 : scrollPositionsRef.current.get(navToHash(nav)) ?? 0
      el.scrollTop = target
      pendingScrollRef.current = target
      if (target > 0) {
        // A window of time, not a count of frames: how many frames a slow
        // engine draws while a long list swaps in varies, and the target has
        // to be re-applied after the list has grown tall enough to hold it.
        const startedAt = performance.now()
        const settle = () => {
          if (Math.abs(el.scrollTop - target) > 1) el.scrollTop = target
          if (performance.now() - startedAt < SCROLL_SETTLE_MS) frame = requestAnimationFrame(settle)
        }
        frame = requestAnimationFrame(settle)
        const events = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const
        stop = () => {
          cancelAnimationFrame(frame)
          for (const name of events) el.removeEventListener(name, stop)
        }
        for (const name of events) el.addEventListener(name, stop, { once: true, passive: true })
      }
    }
    forceTopRef.current = false
    return () => stop()
  }, [nav])

  // The More sheet is a phone control: widening the window past the phone
  // layout closes it.
  useEffect(() => {
    if (isDesktop) setMoreOpen(false)
  }, [isDesktop])

  // The page title names the view (and the album or artist open in it); see
  // DocumentTitle.
  const viewName =
    tab === 'albums' && nav.browseAlbum
      ? nav.browseAlbum.album
      : tab === 'artists' && nav.browseArtist
        ? nav.browseArtist
        : TAB_LABELS[tab]
  // A page inside a tab (an album, an artist, a playlist, a video folder, a
  // Want list artist or album) shows a back link where the title would be.
  const isDetailPage =
    (tab === 'albums' && nav.browseAlbum != null) ||
    (tab === 'artists' && nav.browseArtist != null) ||
    (tab === 'playlists' && nav.playlistId != null) ||
    (tab === 'videos' && Boolean(nav.browseVideoFolder)) ||
    (tab === 'want-list' && nav.want != null && nav.want.page !== 'home')
  // On a view change focus moves to the page title (the top bar's h1), or on a
  // detail page to its heading (the album or artist name), so a keyboard or
  // screen-reader user is not left on a control that has gone, and hears where
  // they are. Not on first load, and not for a replace-only change.
  useEffect(() => {
    if (firstNavRef.current) {
      firstNavRef.current = false
      return
    }
    if (skipFocusRef.current) {
      skipFocusRef.current = false
      return
    }
    const main = mainScrollRef.current
    if (!main) return
    // Hidden tabs are still in the DOM: take the first heading that shows.
    const detailHeading = isDetailPage
      ? [...main.querySelectorAll<HTMLElement>('h2')].find((el) => el.offsetParent !== null)
      : undefined
    const target = detailHeading ?? topBarHeadingRef.current ?? main
    // Focusable by script only, never by Tab, and with no ring on a click-less focus.
    if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1')
    target.focus({ preventScroll: true })
    // Keyed on the address, not the nav object: Back landing on the screen
    // already showing (a closed dialog's history entry) makes a new object
    // for the same place, and must not pull focus off what the dialog
    // handed it back to.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navToHash(nav)])

  function restorePendingScroll() {
    const el = mainScrollRef.current
    if (el && pendingScrollRef.current > 0) el.scrollTop = pendingScrollRef.current
    pendingScrollRef.current = 0
  }

  /** One tab's view, for the nav state that tab last had (a tab that is not
   * showing keeps its own place: the album or artist it had open, its folder). */
  function renderView(t: Tab, n: NavState): ReactNode {
    return (
  t === 'home' ? (
      <HomeView
        onOpenMix={openMix}
        onOpenPlaySource={openPlaySource}
        playlists={playlists}
        onPlaylistsChanged={refreshPlaylists}
        loadPlaylists={loadPlaylists}
        onOpenArtist={openArtist}
        onOpenAlbum={openAlbum}
        onLoaded={restorePendingScroll}
        query={n.homeQuery ?? ''}
        onQueryChange={(q) => navigate({ tab: 'home', homeQuery: q || undefined }, { replace: true })}
        onViewFullSearch={viewFullSearch}
        onOpenTab={switchTab}
      />
    ) : t === 'admin' ? (
      <AdminPanel />
    ) : t === 'settings' ? (
      <SettingsPanel
        navState={n}
        onSectionChange={(id) => navigate({ tab: 'settings', settingsSection: id }, { replace: true })}
        onInstall={isPwaInstalled ? undefined : () => void installPwa()}
      />
    ) : t === 'playlists' ? (
      <PlaylistsView
        playlists={playlists}
        onChanged={refreshPlaylists}
        openId={n.playlistId ?? null}
        onOpenChange={(id) => (id == null ? goUp({ tab: 'playlists' }) : navigate({ tab: 'playlists', playlistId: id }))}
        onBack={leavePlaylist}
        backLabel={playlistBackLabel}
        initialSearch={prefillSearch}
        onOpenArtist={openArtist}
        onOpenAlbum={openAlbum}
        scrollContainerRef={mainScrollRef}
      />
    ) : t === 'radio' ? (
      <RadioView />
    ) : t === 'albums' ? (
      <AlbumsView
        playlists={playlists}
        selectedAlbum={n.browseAlbum ?? null}
        initialSearch={prefillSearch}
        onSelectAlbum={(album) => (album ? navigate({ tab: 'albums', browseAlbum: album }) : goUp({ tab: 'albums' }))}
        onPlaylistsChanged={refreshPlaylists}
        onOpenArtist={openArtist}
        onOpenAlbum={openAlbum}
        onOpenLabel={openSongsFilter}
        scrollContainerRef={mainScrollRef}
      />
    ) : t === 'artists' ? (
      <ArtistsView
        playlists={playlists}
        selectedArtist={n.browseArtist ?? null}
        initialSearch={prefillSearch}
        onSelectArtist={(artist) => (artist ? navigate({ tab: 'artists', browseArtist: artist }) : goUp({ tab: 'artists' }))}
        selectedAlbum={n.browseAlbum ?? null}
        onSelectAlbum={(album) =>
          album
            ? navigate({ tab: 'artists', browseArtist: n.browseArtist, browseAlbum: album })
            : goUp({ tab: 'artists', browseArtist: n.browseArtist })
        }
        onPlaylistsChanged={refreshPlaylists}
        onOpenLabel={openSongsFilter}
        scrollContainerRef={mainScrollRef}
        onOpenCatalogAlbum={openCatalogAlbum}
      />
    ) : t === 'want-list' ? (
      <WantListView
        view={n.want ?? { page: 'home' }}
        onNavigate={(want, options) => {
          if (!options?.up) {
            navigate({ tab: 'want-list', want }, { forceTop: !options?.replace, replace: options?.replace })
          } else if (want.page === 'home') {
            // The Want list with a search in it is still the Want list: Back
            // returns to those results.
            goUp({ tab: 'want-list', want }, (previous) => previous.tab === 'want-list' && (previous.want?.page ?? 'home') === 'home')
          } else {
            goUp({ tab: 'want-list', want })
          }
        }}
        onOpenArtist={(artist, options) => {
          if (options?.showMissing) setShowMissing(true)
          openArtist(artist)
        }}
        onOpenAlbum={openAlbum}
      />
    ) : t === 'files' ? (
      <FilesView
        rootId={n.filesRootId ?? null}
        path={n.filesPath ?? ''}
        canManageFolderExclusions={Boolean(user?.is_admin)}
        onNavigate={(rootId, path) => navigate({ tab: 'files', filesRootId: rootId, filesPath: path || undefined })}
        onShowLibraries={() => navigate({ tab: 'files' })}
      />
    ) : t === 'library-info' ? (
      <LibraryInfoView
        onOpenAlbum={openAlbum}
        onStats={(stats) => {
          setLibraryStats(stats)
          setNavCounts(stats.nav_counts)
        }}
      />
    ) : t === 'recent-played' ? (
      <RecentTracksView
        kind={t}
        playlists={playlists}
        scrollContainerRef={mainScrollRef}
        onPlaylistsChanged={refreshPlaylists}
        onOpenArtist={openArtist}
        onOpenAlbum={openAlbum}
      />
    ) : t === 'videos' ? (
      <VideoFoldersView
        selectedFolder={n.browseVideoFolder ?? null}
        onSelectFolder={(folder) => navigate({ tab: 'videos', browseVideoFolder: folder ?? undefined })}
        onUp={(folder) => goUp({ tab: 'videos', browseVideoFolder: folder ?? undefined })}
        onOpenArtist={openArtist}
        onOpenAlbum={openAlbum}
      />
    ) : (
      <SongsView
        playlists={playlists}
        search={search}
        onClearSearch={() => setSearch('')}
        genre={genre}
        onGenreChange={setGenre}
        genres={genres}
        tag={tag}
        onClearTag={() => setTag('')}
        scrollContainerRef={mainScrollRef}
        onPlaylistsChanged={refreshPlaylists}
        onOpenArtist={openArtist}
        onOpenAlbum={openAlbum}
      />
    )
    )
  }

  const formatCount = (n: number) => n.toLocaleString()
  const sidebarCounts: Partial<Record<Tab, string>> = navCounts
    ? {
        artists: formatCount(navCounts.artists),
        albums: formatCount(navCounts.albums),
        songs: formatCount(navCounts.songs),
        videos: formatCount(navCounts.videos),
        playlists: formatCount(navCounts.playlists),
        radio: formatCount(navCounts.radio_stations),
        'want-list': formatCount(navCounts.want_list),
      }
    : {}
  const topBarCount =
    tab === 'library-info'
      ? libraryStats?.totals.last_scanned_at
        ? `Last scan ${formatLastScan(libraryStats.totals.last_scanned_at)}`
        : undefined
      : sidebarCounts[tab]

  const userActions = (
    <div className="app-header-user">
      <div ref={setHeaderActionsNode} className="app-header-actions-slot" />
      {/* The playback device, where the player bar does not carry it (a
          phone, and wide windows where the side panel replaces the bar). */}
      <TopBarDevicePicker />
      <span className="app-header-username">{user?.username}</span>
      <div className="settings-menu-popover">
        <button
          ref={settingsMenuToggleRef}
          className="settings-menu-toggle"
          onClick={() => setSettingsMenuOpen((o) => !o)}
          aria-label="Settings"
          {...menuTriggerProps(settingsMenuOpen, settingsMenuId)}
        >
          <SettingsIcon />
        </button>
        {settingsMenuOpen && (
          <Menu
            id={settingsMenuId}
            className="settings-menu-panel"
            label="Settings"
            triggerRef={settingsMenuToggleRef}
            onClose={() => setSettingsMenuOpen(false)}
          >
            {SETTINGS_SECTIONS.map((s) => (
              <MenuItem key={s.id} onSelect={() => openSettingsSection(s.id)}>
                {s.label}
              </MenuItem>
            ))}
            {!isPwaInstalled && (
              <MenuItem
                className="install-pwa-menu-btn"
                onSelect={() => {
                  // Close first, so the dialog it raises gives focus back to the gear.
                  setSettingsMenuOpen(false)
                  void installPwa()
                }}
              >
                Install as PWA
              </MenuItem>
            )}
            <MenuSeparator className="settings-menu-divider" />
            <MenuItem onSelect={logout}>Log out</MenuItem>
          </Menu>
        )}
      </div>
    </div>
  )

  // The shell (styles/app-shell.css): a grid of the sidebar (desktop), the
  // page column (top bar, the Songs filters, the scrolling <main>), a column
  // kept free for a desktop side panel, then the player bar and - on a
  // phone - the bottom tab bar as rows below.
  return (
    <HeaderControlsPortalProvider value={headerControlsNode}>
    <TopBarTitlePortalProvider value={topBarTitleNode}>
    <HeaderActionsPortalProvider value={headerActionsNode}>
    <ArtScrollRootProvider value={mainScrollRef}>
    <div className="app-shell">
      <DocumentTitle viewName={viewName} />
      <ShellSidebar
        tabs={navTabs}
        current={tab}
        labels={TAB_LABELS}
        onSelect={switchTab}
        showAdmin={Boolean(user?.is_admin)}
        counts={sidebarCounts}
        extraItems={[
          {
            id: 'library-info',
            label: TAB_LABELS['library-info'],
            icon: <LibraryInfoIcon width="100%" height="100%" />,
            current: tab === 'library-info',
            onSelect: () => switchTab('library-info'),
          },
        ]}
      />

      <div className="app-content">
      <ShellTopBar
        title={TAB_LABELS[tab]}
        count={topBarCount}
        titleHidden={tab === 'home' || isDetailPage}
        headingRef={topBarHeadingRef}
        titleSlotRef={setTopBarTitleNode}
        controlsRef={setHeaderControlsNode}
        actions={userActions}
      />

      <div
        ref={pullIndicatorRef}
        className="pull-refresh-indicator"
        hidden
        aria-live="polite"
      />
      {/* The desktop Now Playing page is drawn over <main> (which keeps its
          layout, and so its scroll position, underneath). */}
      <div className="app-main-wrap">
      <main
        className="app-main"
        ref={mainScrollRef}
        tabIndex={-1}
      >
        {/* Every tab visited stays mounted inside <Activity>: hidden, it keeps its
            state (lists, scroll, an open album) but its effects are
            disconnected - no polling, timers or fetches - until it is shown
            again. Each view has its own error boundary, so one that throws
            shows a fallback here while the header, the tabs and the player
            (above this, and in the provider outside AppInner) keep working;
            navigating elsewhere clears it. */}
        <RenderProbe id="views">
        {mountedTabs.map((t) => {
          const tabNav = t === tab ? nav : (tabNavs[t] ?? { tab: t })
          return (
            <Activity key={t} mode={t === tab ? 'visible' : 'hidden'}>
              <ViewActiveContext value={t === tab}>
                <ErrorBoundary variant="view" resetKey={navToHash(tabNav)}>
                  <Suspense fallback={<LoadingState />}>
                    <Fragment key={viewKeys[t] ?? 0}>{renderView(t, tabNav)}</Fragment>
                  </Suspense>
                </ErrorBoundary>
              </ViewActiveContext>
            </Activity>
          )
        })}
        </RenderProbe>
      </main>
      <div id={NOW_PLAYING_PAGE_HOST_ID} className="nowplaying-page-host" />
      </div>
      </div>

      {/* The desktop side panel (1200px and wider, in place of the player
          bar), in the grid's "panel" column. Mounted here once, so it keeps
          its scroll position across tab changes. */}
      <RenderProbe id="player">
        <SidePanel
          playlists={playlists}
          onPlaylistsChanged={refreshPlaylists}
          onOpenArtist={openArtist}
          onOpenAlbum={openAlbum}
        />
      </RenderProbe>

      <LoadingStatus />
      <ArtPickerHost />
      <InfoModalHost onOpenArtist={openArtist} />

      <RenderProbe id="player">
        <PlayerBarBoundary>
        <PlayerBar
          playlists={playlists}
          onPlaylistsChanged={refreshPlaylists}
          onOpenArtist={openArtist}
          onOpenAlbum={openAlbum}
          onOpenLabel={openSongsFilter}
          onOpenPlaySource={(source) => openPlaySource(source, 'Back')}
        />
        </PlayerBarBoundary>
      </RenderProbe>

      <ShellPhoneTabBar
        tabs={phoneBarTabs}
        current={tab}
        labels={TAB_LABELS}
        onSelect={switchTab}
        currentInMore={!phoneBarTabs.includes(tab)}
        moreOpen={moreOpen}
        onOpenMore={() => setMoreOpen(true)}
      />
      {moreOpen && (
        <ShellMoreSheet
          tabs={moreTabs}
          current={tab}
          labels={TAB_LABELS}
          showAdmin={Boolean(user?.is_admin)}
          onSelect={switchTab}
          onClose={() => setMoreOpen(false)}
        />
      )}
    </div>
    </ArtScrollRootProvider>
    </HeaderActionsPortalProvider>
    </TopBarTitlePortalProvider>
    </HeaderControlsPortalProvider>
  )
}

function AuthGate() {
  const { user, loading } = useAuth()
  // Only asked while signed out: a fresh install with no accounts shows the
  // setup screen instead of a login nobody can pass.
  const [needsSetup, setNeedsSetup] = useState<boolean | null>(null)
  useEffect(() => {
    if (loading || user) return
    let cancelled = false
    // The first-run check boot.js started alongside the sign-in check.
    api.setupStatus({ initial: true }).then(
      (status) => !cancelled && setNeedsSetup(status.needs_setup),
      () => !cancelled && setNeedsSetup(false),
    )
    return () => {
      cancelled = true
    }
  }, [loading, user])
  if (loading || (!user && needsSetup === null)) return <div className="loading">Loading…</div>
  if (!user) return needsSetup ? <SetupPage onComplete={() => setNeedsSetup(false)} /> : <LoginPage />
  return (
    <PlayerProvider>
      <AppInner />
    </PlayerProvider>
  )
}

export default function App() {
  return (
    <ToastProvider>
      <ConfirmProvider>
        <SettingsProvider>
          <AuthProvider>
            <AuthGate />
          </AuthProvider>
        </SettingsProvider>
        <UpdatePrompt />
        {isNativeApp() && <AppUpdatePrompt />}
        {isNativeApp() && <OfflineNotice />}
      </ConfirmProvider>
    </ToastProvider>
  )
}
