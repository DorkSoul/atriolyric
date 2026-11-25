// Helpers shared by the app-*.spec.ts browser flows.

import { expect } from './fixtures'
import type { Locator, Page } from 'playwright/test'
import { type AppMockState, installApiMock } from './mockApi'

export async function openApp(page: Parameters<typeof installApiMock>[0]): Promise<AppMockState> {
  const mock = await installApiMock(page)
  await page.goto('/#/home')
  await expect(page.locator('.home-view')).toBeVisible()
  await expect(page.getByText('Recently played', { exact: true })).toBeVisible()
  return mock
}

/**
 * Opens a tab the way a user would at the current width: its sidebar button
 * (desktop) or bottom-bar button (phone), or - for a tab a phone keeps under
 * More - through the More sheet.
 */
export async function openTab(page: Page, name: string): Promise<void> {
  const direct = page.getByRole('button', { name, exact: true })
  const bar = page.getByRole('navigation', { name: 'Quick tab navigation' })
  if (await bar.isVisible()) {
    const onBar = bar.getByRole('button', { name, exact: true })
    if (await onBar.count()) {
      await onBar.click()
      return
    }
    await bar.getByRole('button', { name: 'More', exact: true }).click()
    await page.getByRole('dialog', { name: 'More' }).getByRole('button', { name, exact: true }).click()
    return
  }
  await direct.click()
}

/**
 * Opens a section of the Settings page the way a user does: the gear menu,
 * then the section's item. Returns the section's card. The page scrolls to it
 * and moves focus there, which this waits for.
 */
export async function openSettings(page: Page, section = 'Account') {
  await page.getByRole('button', { name: 'Settings', exact: true }).click()
  await page.getByRole('menuitem', { name: section, exact: true }).click()
  const card = page.locator(`#settings-${section.toLowerCase()}`)
  await expect(card).toBeFocused()
  return card
}

/**
 * The player surface on screen: the player bar below 1200px wide, the desktop
 * side panel's player block (`.side-panel-player`) at 1200px and wider. Only
 * one of them is ever rendered, and both carry the same accessible names
 * ("Open now playing", Previous / Play / Pause / Next, Seek, Volume, the
 * heart) and show the playing song's title, artist and remote-device note.
 * The side panel's queue is NOT inside it, so text assertions about the
 * playing song are not fooled by a queued one.
 */
export function player(page: Page) {
  return page.locator('.player-bar, .side-panel-player')
}

/**
 * The "Choose playback device: …" button wherever it is at this width: in the
 * player bar from 640 to 1199px, in the top bar on a phone and at 1200px and
 * wider.
 */
export function devicePickerToggle(page: Page) {
  return page.getByRole('button', { name: /^Choose playback device/ })
}

/**
 * Lets the app handle what has already been delivered to the page (a socket
 * message, a dispatched media event): two frames, so React has rendered and
 * committed its effects. Use it before asserting that something did NOT happen.
 */
export async function settle(page: Page): Promise<void> {
  await page.evaluate(
    () => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))),
  )
}

/**
 * Closes the playback socket from the server side and returns once the app
 * has seen the close (it logs `websocket_closed`) and re-rendered, which is
 * still well before its 2 s reconnect.
 */
export async function dropPlaybackSocket(page: Page, mock: AppMockState): Promise<void> {
  const seen = page.waitForEvent('console', (message) => message.text().includes('websocket_closed'))
  await mock.playbackSocket?.close()
  await seen
  await settle(page)
}

/** Plays "Episode One" and opens its full-bleed Now Playing view. */
export async function openVideoNowPlaying(page: Page) {
  // The mock's empty stream makes some engines report a playback error, and
  // its toast (which these tests would otherwise have to wait out, on a clock
  // they have stopped) covers the picture they tap. Let clicks pass through it.
  await page.addStyleTag({ content: '.toast-stack, .toast-stack * { pointer-events: none !important; }' })
  await page.getByRole('button', { name: 'Videos', exact: true }).click()
  await page.locator('.browse-card').filter({ hasText: 'Shows' }).click()
  await page.locator('.video-card').filter({ hasText: 'Episode One' }).click()
  await expect(player(page)).toContainText('Episode One')
  await player(page).getByRole('button', { name: 'Open now playing' }).click()
  const overlay = page.locator('.nowplaying-overlay.video-mode')
  await expect(overlay).toBeVisible()
  return overlay
}

/** `seconds` of 8 kHz mono 16-bit silence. */
export function silentWav(seconds = 1): Buffer {
  const samples = 8000 * seconds
  const buffer = Buffer.alloc(44 + samples * 2)
  buffer.write('RIFF', 0)
  buffer.writeUInt32LE(36 + samples * 2, 4)
  buffer.write('WAVEfmt ', 8)
  buffer.writeUInt32LE(16, 16)
  buffer.writeUInt16LE(1, 20)
  buffer.writeUInt16LE(1, 22)
  buffer.writeUInt32LE(8000, 24)
  buffer.writeUInt32LE(16000, 28)
  buffer.writeUInt16LE(2, 32)
  buffer.writeUInt16LE(16, 34)
  buffer.write('data', 36)
  buffer.writeUInt32LE(samples * 2, 40)
  return buffer
}

// A stand-in for hls.js, which the app loads on demand for a video served as
// HLS. The mock serves no real stream, so the test drives it: every instance
// is listed in `window.__fakeHls`, records the calls the app makes on it and
// lets the test emit hls.js events at it.
export const FAKE_HLS_MODULE = `
const Events = {
  ERROR: 'hlsError',
  MANIFEST_PARSED: 'hlsManifestParsed',
  FRAG_LOADED: 'hlsFragLoaded',
  DESTROYING: 'hlsDestroying',
}
export default class Hls {
  static isSupported() { return true }
  static get Events() { return Events }
  constructor(config) {
    this.config = config
    this.handlers = {}
    this.calls = []
    ;(window.__fakeHls = window.__fakeHls || []).push(this)
  }
  on(event, handler) { (this.handlers[event] = this.handlers[event] || []).push(handler) }
  emit(event, data) { for (const handler of this.handlers[event] || []) handler(event, data) }
  loadSource(url) { this.calls.push('loadSource ' + url) }
  attachMedia() { this.calls.push('attachMedia') }
  startLoad() { this.calls.push('startLoad') }
  recoverMediaError() { this.calls.push('recoverMediaError') }
  destroy() { this.emit(Events.DESTROYING, {}); this.calls.push('destroy') }
}
`

export type FakeHls = {
  calls: string[]
  emit: (event: string, data: Record<string, unknown>) => void
}

/** Serves media item `id` as HLS through the hls.js stand-in above. */
export async function installFakeHls(page: Page, id: number): Promise<void> {
  await page.route(`**/api/stream/${id}/playback`, (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ mode: 'hls', url: `/api/stream/${id}/hls/index.m3u8` }),
  }))
  // Vite serves the pre-bundled dependency from `<cacheDir>/deps/` in
  // development (node_modules/.vite by default).
  await page.route(/\/deps\/hls__js\.js(\?|$)/, (route) =>
    route.fulfill({ status: 200, contentType: 'text/javascript', body: FAKE_HLS_MODULE }))
}

export function fakeHlsCount(page: Page): Promise<number> {
  return page.evaluate(() => (window as unknown as { __fakeHls?: FakeHls[] }).__fakeHls?.length ?? 0)
}

export function fakeHlsCalls(page: Page, index: number, call: string): Promise<number> {
  return page.evaluate(
    ({ index, call }) => (window as unknown as { __fakeHls: FakeHls[] }).__fakeHls[index].calls
      .filter((made) => made.startsWith(call)).length,
    { index, call },
  )
}

export function emitFakeHls(page: Page, index: number, event: string, data: Record<string, unknown> = {}): Promise<void> {
  return page.evaluate(
    ({ index, event, data }) => (window as unknown as { __fakeHls: FakeHls[] }).__fakeHls[index].emit(event, data),
    { index, event, data },
  )
}

/** Points the mock server's playback at another device (nothing is sent). */
export function markActive(mock: AppMockState, deviceId: number): void {
  mock.playback.active_device_id = deviceId
  for (const device of mock.playback.devices) device.is_active = device.id === deviceId
}

export function sawCall(mock: AppMockState, path: string, method?: string): boolean {
  return mock.calls.some((call) => call.path === path && (!method || call.method === method))
}

export type MediaSessionStubState = {
  title: string | null
  playbackState: string
  handlers: Record<string, boolean>
}

// Playwright's three engines disagree about Media Session support, so the
// media-session assertions run against an explicit stub installed before the
// app bundle loads. It records what the app did to the session (metadata,
// playbackState and action handlers) instead of relying on the real one.
export async function installMediaSessionStub(page: Parameters<typeof installApiMock>[0]): Promise<void> {
  await page.addInitScript(() => {
    const testWindow = window as Window & {
      __mediaSession?: {
        metadata: { title?: string } | null
        playbackState: string
        handlers: Record<string, unknown>
      }
    }
    const stub = {
      metadata: null as { title?: string } | null,
      playbackState: 'none',
      handlers: {} as Record<string, unknown>,
      setActionHandler(action: string, handler: unknown) {
        stub.handlers[action] = handler
      },
      setPositionState() {},
    }
    testWindow.__mediaSession = stub
    Object.defineProperty(navigator, 'mediaSession', { configurable: true, get: () => stub })
    const globalWindow = window as Window & { MediaMetadata?: unknown }
    if (typeof globalWindow.MediaMetadata !== 'function') {
      globalWindow.MediaMetadata = class {
        constructor(init: Record<string, unknown>) {
          Object.assign(this, init)
        }
      }
    }
  })
}

export async function readMediaSession(page: Parameters<typeof installApiMock>[0]): Promise<MediaSessionStubState> {
  return page.evaluate(() => {
    const testWindow = window as Window & {
      __mediaSession?: {
        metadata: { title?: string } | null
        playbackState: string
        handlers: Record<string, unknown>
      }
    }
    const stub = testWindow.__mediaSession
    if (!stub) throw new Error('media session stub was never installed')
    return {
      title: stub.metadata?.title ?? null,
      playbackState: stub.playbackState,
      handlers: Object.fromEntries(
        Object.entries(stub.handlers).map(([action, handler]) => [action, typeof handler === 'function']),
      ),
    }
  })
}

/** The app's drop-down (SelectMenu): the chip named "<label>: <choice>" (or by
 * a visible label), and the radio item to pick from the menu it opens. */
export function selectMenu(scope: Page | Locator, name: string | RegExp): Locator {
  return scope.getByRole('button', { name: typeof name === 'string' ? new RegExp(`^${name}\\b`) : name })
}

export async function chooseOption(page: Page, toggle: Locator, option: string | RegExp): Promise<void> {
  await toggle.click()
  await page.getByRole('menuitemradio', { name: option, exact: typeof option === 'string' }).click()
}

/** Now Playing, wherever it is: a page in the content area on a desktop or
 * tablet (a region), a full-screen dialog named by the song on a phone (and
 * for a video). */
export function nowPlayingView(page: Page, name = 'Bohemian Rhapsody') {
  return page.getByRole('dialog', { name }).or(page.getByRole('region', { name: 'Now playing', exact: true }))
}
