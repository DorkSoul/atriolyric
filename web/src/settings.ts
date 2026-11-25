import { syncSystemBars } from './native/bridge'
import type { Tab } from './nav'

export type ThemeMode = 'system' | 'light' | 'dark'

// The nav tabs a user can reorder/hide - Home is always first and always
// visible (every app needs one guaranteed way back), Admin is already
// conditional on the account being an admin, and Settings always sits at
// the end, so none of the three is customizable here.
export type CustomizableTab = Exclude<
  Tab,
  'home' | 'library-info' | 'recent-played' | 'admin' | 'settings'
>

export const CUSTOMIZABLE_TABS: CustomizableTab[] = [
  'artists',
  'albums',
  'songs',
  'playlists',
  'now-playing',
  'radio',
  'videos',
  'files',
  'want-list',
]

export const TAB_LABELS: Record<CustomizableTab, string> = {
  songs: 'Songs',
  albums: 'Albums',
  artists: 'Artists',
  videos: 'Videos',
  files: 'Files',
  playlists: 'Playlists',
  'now-playing': 'Now playing',
  radio: 'Radio',
  'want-list': 'Want list',
}

// List density for track lists, album/artist list mode, file rows and the
// queue: 'compact' tightens the rows. A display preference, kept per device
// like the theme; applied as `data-density` on <html> (see applyListDensity).
export const LIST_DENSITIES = ['comfortable', 'compact'] as const
export type ListDensity = (typeof LIST_DENSITIES)[number]

export const LIST_DENSITY_LABELS: Record<ListDensity, string> = {
  comfortable: 'Comfortable',
  compact: 'Compact',
}

// The sections of Home, in their default order. Users can reorder and hide
// them (Customise on Home, and the Settings page); a hidden section is not
// rendered and does not fetch its data.
export const HOME_SECTION_IDS = ['recent', 'recommended', 'discover', 'mixes', 'radio'] as const
export type HomeSectionId = (typeof HOME_SECTION_IDS)[number]

export const HOME_SECTION_LABELS: Record<HomeSectionId, string> = {
  recent: 'Recently played',
  recommended: 'Recommended from your library',
  discover: 'Discover new artists',
  mixes: 'Mixes for you',
  radio: 'Your radio stations',
}

export interface HomeSectionsSetting {
  /** Every section id, in display order. */
  order: HomeSectionId[]
  /** Ids not shown on Home. */
  hidden: HomeSectionId[]
}

export const DEFAULT_HOME_SECTIONS: HomeSectionsSetting = { order: [...HOME_SECTION_IDS], hidden: [] }

/** A saved value made whole: unknown ids dropped, duplicates removed, and
 * sections added by a later version appended (visible) to the end. */
export function normalizeHomeSections(value: unknown): HomeSectionsSetting {
  const raw = (value && typeof value === 'object' ? value : {}) as { order?: unknown; hidden?: unknown }
  const isId = (id: unknown): id is HomeSectionId => HOME_SECTION_IDS.includes(id as HomeSectionId)
  const saved = Array.isArray(raw.order) ? raw.order.filter(isId) : []
  const order = [...new Set(saved)]
  for (const id of HOME_SECTION_IDS) if (!order.includes(id)) order.push(id)
  const hidden = Array.isArray(raw.hidden) ? [...new Set(raw.hidden.filter(isId))] : []
  return { order, hidden }
}

/** The visible Home sections, in order. */
export function visibleHomeSections(setting: HomeSectionsSetting): HomeSectionId[] {
  return setting.order.filter((id) => !setting.hidden.includes(id))
}

// Streaming quality cap in kbps - matches ALLOWED_BITRATES_KBPS on the
// server exactly, since a value outside this set is silently ignored
// server-side and would look like "picked a quality, nothing changed".
export const STREAM_QUALITIES = [96, 128, 192, 320] as const
export type StreamQuality = (typeof STREAM_QUALITIES)[number]

// Tile size for artwork collections and media lists across the app,
// including browse/video grids, Home rails, and list thumbnails - a display
// preference, so (like theme) it's kept per-device rather than synced to
// the account: a phone and a desktop reasonably want different sizes.
// (The accent color, by contrast, is account-wide - see the User model.)
//
// Expressed as "how many tiles per row on a phone", not a raw pixel size -
// a fixed pixel size either wastes space on narrow screens (doesn't divide
// the row evenly) or barely changes anything on wide ones. Each tier is
// instead a minmax() minimum chosen so it divides a ~380px-wide phone
// screen into exactly that many equal tiles with zero leftover space;
// grid auto-fill then reuses that same minimum on a wider screen (PC) and
// naturally fits proportionally more of them in per row, with no separate
// desktop setting needed.
export const GRID_SIZES = ['small', 'medium', 'large', 'xlarge'] as const
export type GridSize = (typeof GRID_SIZES)[number]

// Tiles per row this tier targets on a ~380px-wide phone screen.
export const GRID_SIZE_PHONE_COLUMNS: Record<GridSize, number> = {
  small: 4,
  medium: 3,
  large: 2,
  xlarge: 1,
}

const GRID_TILE_REMS: Record<GridSize, { browse: number; video: number }> = {
  small: { browse: 4, video: 4.2 },
  medium: { browse: 5.8, video: 5.9 },
  large: { browse: 9.3, video: 9.4 },
  xlarge: { browse: 19.9, video: 19.9 },
}

// Artwork in compact row/list views scales with the same four appearance
// tiers. These dimensions also feed the virtual-list row estimates.
export const GRID_LIST_ART_REMS: Record<GridSize, number> = {
  small: 2,
  medium: 2.5,
  large: 3.5,
  xlarge: 4.5,
}

export const ACCENT_PRESETS = [
  '#5b8def', // blue (default)
  '#8b5cf6', // violet
  '#ec4899', // pink
  '#ef4444', // red
  '#f59e0b', // amber
  '#10b981', // green
  '#14b8a6', // teal
  '#64748b', // slate
]

// What a swatch is called to a screen reader: the preset's colour name, not
// its hex value.
const ACCENT_PRESET_NAMES: Record<string, string> = {
  '#5b8def': 'blue',
  '#8b5cf6': 'violet',
  '#ec4899': 'pink',
  '#ef4444': 'red',
  '#f59e0b': 'amber',
  '#10b981': 'green',
  '#14b8a6': 'teal',
  '#64748b': 'slate',
}

export function accentColorName(hex: string): string {
  return ACCENT_PRESET_NAMES[hex.toLowerCase()] ?? `custom colour ${hex.toLowerCase()}`
}

// Matches the `.tabs`/`.tabs-mobile` breakpoint in index.css, so "PC" vs
// "mobile" for tile sizing lines up with the same point the rest of the
// layout already switches at (viewport width, not user-agent sniffing -
// that stays correct if a desktop window is just narrow, or a tablet is
// wide).
export const DESKTOP_MEDIA_QUERY = '(min-width: 640px)'

export interface AppSettings {
  theme: ThemeMode
  // Kept separate rather than one shared size: a phone and a desktop
  // reasonably want different tile densities, and forcing one choice to
  // serve both means compromising on whichever device you set it from.
  gridSizeDesktop: GridSize
  gridSizeMobile: GridSize
  maxBitrateKbps: StreamQuality | null
  adaptiveBitrateEnabled: boolean
  tabOrder: CustomizableTab[]
  hiddenTabs: CustomizableTab[]
  /** Artist pages split albums into Albums / EPs / Singles / Live /
   * Compilations; off shows one flat grid. */
  artistAlbumSections: boolean
  /** Row height of lists (see LIST_DENSITIES). */
  listDensity: ListDensity
  /** Order and visibility of Home's sections. */
  homeSections: HomeSectionsSetting
  /** Width of the desktop now-playing side panel in px, or null for the
   * stylesheet's default (which is also its minimum). */
  sidePanelWidth: number | null
  /** The desktop sidebar shows icons only (as it does under 1000px). */
  sidebarCollapsed: boolean
}

export const DEFAULT_SETTINGS: AppSettings = {
  theme: 'system',
  gridSizeDesktop: 'large',
  gridSizeMobile: 'medium',
  maxBitrateKbps: null,
  adaptiveBitrateEnabled: false,
  tabOrder: CUSTOMIZABLE_TABS,
  hiddenTabs: [],
  artistAlbumSections: true,
  listDensity: 'comfortable',
  homeSections: DEFAULT_HOME_SECTIONS,
  sidePanelWidth: null,
  sidebarCollapsed: false,
}

const STORAGE_KEY = 'atriolyric.settings'

// The settings as last read or saved: main.tsx (to theme the first frame)
// and SettingsProvider both start from them, with one read and parse.
let current: AppSettings | null = null

export function loadSettings(): AppSettings {
  return (current ??= readSettings())
}

function readSettings(): AppSettings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return DEFAULT_SETTINGS
    const parsed = JSON.parse(raw)
    // Reconcile against the current tab list - a version upgrade that adds
    // a tab shouldn't hide it forever just because it's missing from an
    // older saved order.
    const savedOrder: CustomizableTab[] = Array.isArray(parsed.tabOrder) ? parsed.tabOrder : []
    const missing = CUSTOMIZABLE_TABS.filter((t) => !savedOrder.includes(t))
    const tabOrder = [...savedOrder.filter((t) => CUSTOMIZABLE_TABS.includes(t))]
    for (const t of missing) {
      // Now playing joins an older saved order after Playlists, as by default.
      const after = t === 'now-playing' ? tabOrder.indexOf('playlists') : -1
      if (after >= 0) tabOrder.splice(after + 1, 0, t)
      else tabOrder.push(t)
    }
    // A saved value with one `gridSize` for both PC and mobile - carry that
    // forward as the starting point for both instead of silently resetting
    // an existing user back to the (different) defaults.
    const legacySize = GRID_SIZES.includes(parsed.gridSize) ? parsed.gridSize : null
    return {
      theme: parsed.theme === 'light' || parsed.theme === 'dark' ? parsed.theme : 'system',
      gridSizeDesktop: GRID_SIZES.includes(parsed.gridSizeDesktop)
        ? parsed.gridSizeDesktop
        : (legacySize ?? DEFAULT_SETTINGS.gridSizeDesktop),
      gridSizeMobile: GRID_SIZES.includes(parsed.gridSizeMobile)
        ? parsed.gridSizeMobile
        : (legacySize ?? DEFAULT_SETTINGS.gridSizeMobile),
      maxBitrateKbps: STREAM_QUALITIES.includes(parsed.maxBitrateKbps) ? parsed.maxBitrateKbps : null,
      adaptiveBitrateEnabled:
        typeof parsed.adaptiveBitrateEnabled === 'boolean' ? parsed.adaptiveBitrateEnabled : false,
      tabOrder,
      hiddenTabs: Array.isArray(parsed.hiddenTabs)
        ? parsed.hiddenTabs.filter((t: unknown) => CUSTOMIZABLE_TABS.includes(t as CustomizableTab))
        : [],
      artistAlbumSections:
        typeof parsed.artistAlbumSections === 'boolean' ? parsed.artistAlbumSections : true,
      listDensity: LIST_DENSITIES.includes(parsed.listDensity) ? parsed.listDensity : 'comfortable',
      homeSections: normalizeHomeSections(parsed.homeSections),
      // Clamped again by the stylesheet against the window; this only drops
      // garbage.
      sidePanelWidth:
        typeof parsed.sidePanelWidth === 'number' && Number.isFinite(parsed.sidePanelWidth) && parsed.sidePanelWidth > 0
          ? Math.round(parsed.sidePanelWidth)
          : null,
      sidebarCollapsed: typeof parsed.sidebarCollapsed === 'boolean' ? parsed.sidebarCollapsed : false,
    }
  } catch {
    return DEFAULT_SETTINGS
  }
}

export function saveSettings(settings: AppSettings) {
  current = settings
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(settings))
  } catch {
    // Storage can be unavailable (private browsing, quota) - settings just
    // won't persist across reloads, not worth surfacing an error for.
  }
}

function clamp255(n: number): number {
  return Math.max(0, Math.min(255, n))
}

function hexToRgb(hex: string): [number, number, number] {
  const clean = hex.replace('#', '')
  const full = clean.length === 3 ? clean.split('').map((c) => c + c).join('') : clean
  const n = Number.parseInt(full, 16)
  if (Number.isNaN(n) || full.length !== 6) return [91, 141, 239] // fallback to default accent
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}

function rgbToHex(r: number, g: number, b: number): string {
  return `#${[r, g, b].map((c) => clamp255(Math.round(c)).toString(16).padStart(2, '0')).join('')}`
}

export function lighten(hex: string, amount: number): string {
  const [r, g, b] = hexToRgb(hex)
  return rgbToHex(r + (255 - r) * amount, g + (255 - g) * amount, b + (255 - b) * amount)
}

export function hexToRgba(hex: string, alpha: number): string {
  const [r, g, b] = hexToRgb(hex)
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

export function resolveTheme(mode: ThemeMode): 'light' | 'dark' {
  if (mode === 'light' || mode === 'dark') return mode
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
}

export function applyTheme(mode: ThemeMode) {
  const resolved = resolveTheme(mode)
  if (resolved === 'light') {
    document.documentElement.setAttribute('data-theme', 'light')
  } else {
    document.documentElement.removeAttribute('data-theme')
  }
  syncSystemBars()
  // --accent-text depends on the palette, so a theme change re-derives it.
  if (appliedAccent) applyAccentColor(appliedAccent)
}

// WCAG 2.x contrast. Text needs 4.5:1, so the derived colours below aim for
// that against every surface the accent text can sit on.
const TEXT_CONTRAST = 4.5

function srgbToLinear(channel: number): number {
  const c = channel / 255
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
}

export function relativeLuminance(hex: string): number {
  const [r, g, b] = hexToRgb(hex)
  return 0.2126 * srgbToLinear(r) + 0.7152 * srgbToLinear(g) + 0.0722 * srgbToLinear(b)
}

export function contrastRatio(a: string, b: string): number {
  const la = relativeLuminance(a)
  const lb = relativeLuminance(b)
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}

/** Text or icon colour for something filled with the accent: black or white,
 * whichever contrasts more. One of them always reaches 4.58:1 or better. */
export function onAccentColor(accent: string): '#000000' | '#ffffff' {
  return contrastRatio(accent, '#ffffff') >= contrastRatio(accent, '#000000') ? '#ffffff' : '#000000'
}

function mixHex(from: string, to: string, amount: number): string {
  const [r1, g1, b1] = hexToRgb(from)
  const [r2, g2, b2] = hexToRgb(to)
  return rgbToHex(r1 + (r2 - r1) * amount, g1 + (g2 - g1) * amount, b1 + (b2 - b1) * amount)
}

// The page surfaces text can sit on, copied from the palettes in
// styles/base.css (and the dark copy on .nowplaying-overlay in
// styles/now-playing.css) - keep all three in step. --surface-3 is left out:
// nothing sets running text on it.
const PALETTE_SURFACES = {
  dark: { bg: '#0d1017', surface: '#141925', surface2: '#1c2231' },
  light: { bg: '#f4f6fa', surface: '#ffffff', surface2: '#edf0f6' },
}

// Share of the accent in --accent-muted (the selected-tab and active-segment
// fill), which accent text also has to read on.
const ACCENT_MUTED_ALPHA = 0.16

/** The accent as a text/link colour on a theme's surfaces: unchanged when it
 * already reaches 4.5:1 on all of them, otherwise mixed towards black (light
 * theme) or white (dark theme) in 1% steps until it does - hue is kept, only
 * lightness moves. */
export function accentTextColor(accent: string, theme: 'light' | 'dark'): string {
  const { bg, surface, surface2 } = PALETTE_SURFACES[theme]
  const backgrounds = [
    bg,
    surface,
    surface2,
    mixHex(surface, accent, ACCENT_MUTED_ALPHA),
    mixHex(surface2, accent, ACCENT_MUTED_ALPHA),
  ]
  const towards = theme === 'light' ? '#000000' : '#ffffff'
  for (let step = 0; step <= 100; step++) {
    const candidate = step === 0 ? accent : mixHex(accent, towards, step / 100)
    if (backgrounds.every((background) => contrastRatio(candidate, background) >= TEXT_CONTRAST)) {
      return candidate
    }
  }
  return towards
}

/** Hover colour for accent text: further from the background than the resting
 * colour, so it never loses contrast. */
export function accentTextHoverColor(accentText: string, theme: 'light' | 'dark'): string {
  return mixHex(accentText, theme === 'light' ? '#000000' : '#ffffff', 0.18)
}

// Remembered so a theme change can re-derive --accent-text without every
// caller of applyTheme having to know the accent.
let appliedAccent: string | null = null

export function applyAccentColor(hex: string) {
  appliedAccent = hex
  const root = document.documentElement.style
  const theme = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark'
  root.setProperty('--accent', hex)
  root.setProperty('--accent-hover', lighten(hex, 0.25))
  root.setProperty('--accent-muted', hexToRgba(hex, ACCENT_MUTED_ALPHA))
  root.setProperty('--on-accent', onAccentColor(hex))
  const text = accentTextColor(hex, theme)
  root.setProperty('--accent-text', text)
  root.setProperty('--accent-text-hover', accentTextHoverColor(text, theme))
  // Now Playing keeps the dark palette whatever the theme is (see
  // .nowplaying-overlay in index.css), so it reads the dark variants.
  const darkText = accentTextColor(hex, 'dark')
  root.setProperty('--accent-text-dark', darkText)
  root.setProperty('--accent-text-hover-dark', accentTextHoverColor(darkText, 'dark'))
}

export function applyGridSize(size: GridSize) {
  const { browse, video } = GRID_TILE_REMS[size]
  const root = document.documentElement.style
  root.setProperty('--browse-tile-size', `${browse}rem`)
  root.setProperty('--video-tile-size', `${video}rem`)
  root.setProperty('--rail-tile-size', `${browse}rem`)
  root.setProperty('--list-art-size', `${GRID_LIST_ART_REMS[size]}rem`)
}

/** The side panel's chosen width, as `--side-panel-width` on <html>
 * (side-panel.css never lets it go under the default width). */
export function applySidePanelWidth(width: number | null) {
  const root = document.documentElement.style
  if (width == null) root.removeProperty('--side-panel-width')
  else root.setProperty('--side-panel-width', `${width}px`)
}

/** Applies the list density: `data-density="compact"` on <html> (CSS tightens
 * rows under it) - absent for the comfortable default. */
export function applyListDensity(density: ListDensity) {
  if (density === 'compact') document.documentElement.setAttribute('data-density', 'compact')
  else document.documentElement.removeAttribute('data-density')
}

/** Whether compact density is applied right now (outside React; components
 * read settings.listDensity). */
export function isCompactDensity(): boolean {
  return document.documentElement.getAttribute('data-density') === 'compact'
}
