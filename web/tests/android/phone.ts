import { execFileSync } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import {
  _android as android,
  chromium,
  expect,
  request,
  test as base,
  type AndroidDevice,
  type APIRequestContext,
  type Browser,
  type BrowserContext,
  type Locator,
  type Page,
} from 'playwright/test'
import { DATA_DIR, PASSWORD, STORAGE_STATE, USERNAME, WEB_URL } from '../real/env'
import { adb, CDP_PORT, PHONE_ORIGIN, SERIAL, SHOTS_DIR, shell } from './adb'

export { expect }

/**
 * How Chrome is started on the phone.
 * - `cdp` (default): Chrome with only the first-run screens skipped, reached
 *   through its DevTools socket (`adb forward` + connectOverCDP). It runs with
 *   Chrome's normal background behaviour, as on a real phone.
 * - `playwright`: AndroidDevice.launchBrowser(). Playwright then also passes
 *   its desktop test switches (--disable-background-timer-throttling,
 *   --disable-renderer-backgrounding, ...), which change exactly what the
 *   screen-off test measures - useful as a comparison, not as the reference.
 */
export const LAUNCH_MODE: 'cdp' | 'playwright' = process.env.ANDROID_LAUNCH === 'playwright' ? 'playwright' : 'cdp'

const CHROME = 'com.android.chrome'
// The emulator (software GPU) can crash right after Chrome sets up or tears
// down a Vulkan instance; this keeps Chrome off Vulkan. (--disable-gpu makes
// Chrome 124 on the emulator crash at start.) ANDROID_CHROME_GPU_FLAGS=''
// removes it, e.g. on a real phone.
const CHROME_GPU_FLAGS = process.env.ANDROID_CHROME_GPU_FLAGS ?? '--disable-features=Vulkan'

export interface Phone {
  context: BrowserContext
  page: Page
  /** The device id the server registered this phone as (null before). */
  deviceId: number | null
  /** Media item ids this phone reported as played (POST /api/stream/{id}/played). */
  played: number[]
  /** Tap the centre of an element with a real touch from the OS (adb input). */
  tap(locator: Locator): Promise<void>
  /** A real touch swipe between two points in CSS pixels of the page. */
  swipe(from: { x: number; y: number }, to: { x: number; y: number }, ms: number): Promise<void>
  /** Whether detach/reattach work (ANDROID_LAUNCH=cdp). */
  canDetach: boolean
  /** Drop the DevTools connection, leaving Chrome and the tab running.
   * While Playwright is attached, Chrome emulates a focused page (Playwright
   * enables Emulation.setFocusEmulationEnabled), which keeps the page
   * "visible" even with the screen off and the activity stopped. */
  detach(): Promise<void>
  /** Connect again; `page` and `context` then refer to the same tab. */
  reattach(): Promise<void>
}

async function waitFor<T>(what: string, probe: () => T | undefined | null, timeoutMs = 15_000): Promise<T> {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const value = probe()
    if (value != null) return value
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`)
    await new Promise((r) => setTimeout(r, 250))
  }
}

/**
 * One Chrome on the phone for the whole run. Chrome is never killed between
 * tests: on the emulator, ending Chrome's GPU process makes the emulator's
 * GPU host crash (SIGSEGV in gfxstream's cleanupProcGLObjects). Each test gets
 * a new tab instead, with the app's origin data
 * cleared. A Chrome that is already running with its DevTools socket open
 * (from an earlier run) is reused as it is.
 */
export class PhoneChrome {
  context!: BrowserContext
  private browser: Browser | null = null
  private device: AndroidDevice | null = null

  static async start(): Promise<PhoneChrome> {
    const chrome = new PhoneChrome()
    if (LAUNCH_MODE === 'playwright') {
      // launchBrowser force-stops Chrome first: risky on the emulator (see above).
      const [device] = await android.devices({ omitDriverInstall: true })
      if (!device) throw new Error('no Android device found by Playwright')
      chrome.device = device
      chrome.context = await device.launchBrowser({ hasTouch: true, isMobile: true })
      return chrome
    }
    const running = shell(`pidof ${CHROME} || true`).trim() !== ''
    const hasSocket = () => /@(chrome_devtools_remote)\b/.exec(shell('cat /proc/net/unix'))?.[1]
    if (running && !hasSocket()) {
      console.warn('[android] Chrome runs without its DevTools socket; restarting it (may upset the emulator)')
      shell(`am force-stop ${CHROME}`)
    }
    if (!running || !hasSocket()) {
      // Not running: a fresh profile (no restored tabs or data from earlier
      // runs). The notification permission goes with it and is granted again.
      shell(`pm clear ${CHROME}`)
      shell(`pm grant ${CHROME} android.permission.POST_NOTIFICATIONS`)
      // What Chrome reads at start on a debuggable build: skip the first-run
      // and default-browser screens; keep it off Vulkan.
      shell(`echo "_ --disable-fre --no-default-browser-check --no-first-run ${CHROME_GPU_FLAGS}" > /data/local/tmp/chrome-command-line`)
      shell(`am start -a android.intent.action.VIEW -d about:blank ${CHROME}`)
      await waitFor('Chrome DevTools socket', hasSocket, 30_000)
    }
    adb('forward', `tcp:${CDP_PORT}`, 'localabstract:chrome_devtools_remote')
    await chrome.connect()
    shell('rm -f /data/local/tmp/chrome-command-line')
    return chrome
  }

  get connected(): boolean {
    return this.device != null || (this.browser?.isConnected() ?? false)
  }

  get canDetach(): boolean {
    return LAUNCH_MODE === 'cdp'
  }

  async connect(): Promise<void> {
    const deadline = Date.now() + 20_000
    for (;;) {
      try {
        this.browser = await chromium.connectOverCDP(`http://127.0.0.1:${CDP_PORT}`)
        break
      } catch (error) {
        if (Date.now() > deadline) throw error
        await new Promise((r) => setTimeout(r, 500))
      }
    }
    this.context = this.browser.contexts()[0]
    this.context.setDefaultTimeout(15_000)
  }

  /** Drop the DevTools connection (a connectOverCDP browser's close() only
   * disconnects); Chrome and its tabs carry on. */
  async disconnect(): Promise<void> {
    await this.browser?.close()
    this.browser = null
  }

  /** A new foreground tab; every other tab closed. */
  async freshTab(): Promise<Page> {
    const old = this.context.pages()
    const page = await this.context.newPage()
    for (const p of old) await p.close().catch((error: unknown) => console.warn('[android] closing an old tab failed:', error))
    await page.bringToFront()
    return page
  }

  async close(): Promise<void> {
    if (this.device) {
      await this.context.close().catch(() => undefined)
      await this.device.close().catch(() => undefined)
      return
    }
    // Leave Chrome running on a blank tab (nothing playing).
    await this.freshTab().catch(() => undefined)
    await this.disconnect().catch(() => undefined)
  }
}

/** A new tab on the phone with no data for the app's origin (no login, no
 * client token, no service worker), on the app. */
export async function openPhone(chrome: PhoneChrome): Promise<Phone> {
  const page = await chrome.freshTab()
  const context = chrome.context
  const cdp = await context.newCDPSession(page)
  await cdp.send('Storage.clearDataForOrigin', { origin: PHONE_ORIGIN, storageTypes: 'all' })
  await context.clearCookies()
  await cdp.detach()

  let calibration: { dpr: number; offX: number; offY: number } | null = null
  const phone: Phone = {
    context,
    page,
    deviceId: null,
    played: [],
    async tap(locator) {
      // Calibrate first: it closes the on-screen keyboard, which moves the
      // layout. Then tap where the element is once it has stopped moving.
      await hideKeyboard(phone.page)
      const c = calibration ?? (calibration = await calibrate(phone.page))
      await locator.scrollIntoViewIfNeeded()
      let box = await locator.boundingBox()
      for (let i = 0; i < 20; i += 1) {
        await new Promise((r) => setTimeout(r, 150))
        const again = await locator.boundingBox()
        const same = box && again && Math.abs(box.x - again.x) < 1 && Math.abs(box.y - again.y) < 1
        box = again
        if (same) break
      }
      if (!box) throw new Error(`no box to tap for ${String(locator)}`)
      const vv = await visualOffset(phone.page)
      const x = Math.round((box.x + box.width / 2 - vv.left) * c.dpr + c.offX)
      const y = Math.round((box.y + box.height / 2 - vv.top) * c.dpr + c.offY)
      shell(`input tap ${x} ${y}`)
    },
    async swipe(from, to, ms) {
      await hideKeyboard(phone.page)
      const c = calibration ?? (calibration = await calibrate(phone.page))
      const vv = await visualOffset(phone.page)
      const px = (p: { x: number; y: number }) => `${Math.round((p.x - vv.left) * c.dpr + c.offX)} ${Math.round((p.y - vv.top) * c.dpr + c.offY)}`
      shell(`input swipe ${px(from)} ${px(to)} ${ms}`)
    },
    canDetach: chrome.canDetach,
    async detach() {
      if (!chrome.canDetach) throw new Error('detach needs ANDROID_LAUNCH=cdp')
      await chrome.disconnect()
    },
    async reattach() {
      if (!chrome.canDetach) throw new Error('reattach needs ANDROID_LAUNCH=cdp')
      await chrome.connect()
      const again = chrome.context.pages().find((p) => p.url().startsWith(PHONE_ORIGIN))
      if (!again) throw new Error(`the app's tab is gone after reattaching: ${chrome.context.pages().map((p) => p.url()).join(', ')}`)
      phone.context = chrome.context
      phone.page = again
      listen(again)
    },
  }
  listen(page)
  await page.goto(`${PHONE_ORIGIN}/`)
  return phone

  function listen(page: Page): void {
  // The device id the server hands this phone (read from the real socket).
  page.on('websocket', (ws) => {
    if (!ws.url().endsWith('/ws/playback')) return
    ws.on('framereceived', ({ payload }) => {
      if (typeof payload !== 'string') return
      try {
        const parsed = JSON.parse(payload) as { type?: string; device_id?: number }
        if (parsed.type === 'registered' && typeof parsed.device_id === 'number') phone.deviceId = parsed.device_id
      } catch {
        // Not JSON.
      }
    })
  })
  page.on('request', (req) => {
    const match = /\/api\/stream\/(\d+)\/played$/.exec(new URL(req.url()).pathname)
    if (match && req.method() === 'POST') phone.played.push(Number(match[1]))
  })
  }
}

/** Where the visual viewport (what is on screen) sits in the layout
 * viewport: Chrome pans it, e.g. to keep a focused field above the keyboard.
 * Bounding boxes and touch coordinates are layout-viewport based. */
async function visualOffset(page: Page): Promise<{ left: number; top: number }> {
  return page.evaluate(() => ({ left: window.visualViewport?.offsetLeft ?? 0, top: window.visualViewport?.offsetTop ?? 0 }))
}

/** Close the on-screen keyboard (it moves and covers the layout). Back only
 * when the keyboard is shown: otherwise Back would navigate. */
async function hideKeyboard(page: Page): Promise<void> {
  await page.evaluate(() => { if (document.activeElement instanceof HTMLElement) document.activeElement.blur() })
  const shown = () => /mInputShown=true/.test(shell('dumpsys input_method'))
  for (let i = 0; i < 10 && shown(); i += 1) {
    if (i === 3) shell('input keyevent KEYCODE_BACK')
    await new Promise((r) => setTimeout(r, 300))
  }
}

/** Where on the screen (device pixels) the page's CSS pixel (0,0) is: a
 * transparent full-page layer records where a real tap at a known screen
 * point lands, then goes away. Nothing in the app sees that tap. */
async function calibrate(page: Page): Promise<{ dpr: number; offX: number; offY: number }> {
  const { dpr, w, h } = await page.evaluate(() => {
    if (document.activeElement instanceof HTMLElement) document.activeElement.blur()
    const layer = document.createElement('div')
    layer.id = '__tap_calibration'
    layer.style.cssText = 'position:fixed;inset:0;z-index:2147483647;background:transparent;touch-action:none'
    const store = window as unknown as { __tapAt?: { x: number; y: number } }
    delete store.__tapAt
    layer.addEventListener('touchstart', (e) => {
      e.preventDefault()
      e.stopPropagation()
      store.__tapAt = { x: e.touches[0].clientX, y: e.touches[0].clientY }
    }, { passive: false })
    layer.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation() })
    document.body.appendChild(layer)
    return { dpr: window.devicePixelRatio, w: window.innerWidth, h: window.innerHeight }
  })
  // A point in the upper part of the page area of the screen (the lower part
  // may be under the on-screen keyboard).
  const sx = Math.round((w / 2) * dpr)
  const sy = Math.round(h * 0.3 * dpr) + 250
  shell(`input tap ${sx} ${sy}`)
  await expect.poll(() => page.evaluate(() => (window as unknown as { __tapAt?: unknown }).__tapAt ?? null)).not.toBeNull()
  const at = await page.evaluate(() => {
    document.getElementById('__tap_calibration')?.remove()
    return (window as unknown as { __tapAt: { x: number; y: number } }).__tapAt
  })
  const vv = await visualOffset(page)
  return { dpr, offX: sx - (at.x - vv.left) * dpr, offY: sy - (at.y - vv.top) * dpr }
}

/** Sign in through the login form (typing, then a real tap on Sign in). */
export async function signIn(phone: Phone): Promise<void> {
  const { page } = phone
  const nav = page.getByRole('navigation').first()
  const username = page.getByPlaceholder('Username')
  try {
    await expect(username.or(nav)).toBeVisible({ timeout: 20_000 })
  } catch {
    // Right after the radios come back on (previous test), the dev server's
    // hundreds of module requests can meet a network change and leave the
    // page blank. One reload, logged, so it is not hidden.
    console.warn('[android] the app did not render within 20 s; reloading once')
    await page.reload()
    await expect(username.or(nav)).toBeVisible({ timeout: 30_000 })
  }
  if (await username.isVisible()) {
    await username.fill(USERNAME)
    await page.getByPlaceholder('Password').fill(PASSWORD)
    await phone.tap(page.getByRole('button', { name: 'Sign in' }))
  }
  await expect(nav).toBeVisible({ timeout: 30_000 })
  await expect.poll(() => phone.deviceId, { timeout: 30_000 }).not.toBeNull()
}

// --- The phone's media elements --------------------------------------------

export interface MediaInfo {
  role: string
  paused: boolean
  time: number
  src: string
}

/** The music/video elements (not the silent keep-alive or radio element). */
export async function musicElements(page: Page): Promise<MediaInfo[]> {
  return page.evaluate(() =>
    [...document.querySelectorAll<HTMLMediaElement>('[data-player-element]')].map((el) => ({
      role: el.dataset.playerElement ?? '',
      paused: el.paused,
      time: el.currentTime,
      src: el.currentSrc || el.src,
    })),
  )
}

export async function playingTime(page: Page): Promise<number | null> {
  const playing = (await musicElements(page)).find((m) => !m.paused && m.src !== '')
  return playing ? playing.time : null
}

export async function isSilent(page: Page): Promise<boolean> {
  return (await musicElements(page)).every((m) => m.paused)
}

export function playerBar(page: Page): Locator {
  return page.locator('.player-bar')
}

export function trackTitle(page: Page): Locator {
  return playerBar(page).locator('.track-title').first()
}

export function albumHash(artist: string, album: string): string {
  return `#/albums/${encodeURIComponent(artist)}/${encodeURIComponent(album)}`
}

/** Open an album and tap its Play button; waits for sound on the phone. */
export async function tapPlayAlbum(phone: Phone, artist: string, album: string, firstTitle: string): Promise<void> {
  const { page } = phone
  await page.goto(`${PHONE_ORIGIN}/${albumHash(artist, album)}`)
  const play = page.locator('.hero-play-btn[aria-label="Play"]')
  await expect(play).toBeVisible()
  await phone.tap(play)
  await expect(trackTitle(page)).toContainText(firstTitle)
  await expect.poll(() => playingTime(page), { timeout: 20_000 }).not.toBeNull()
}

// --- The server ------------------------------------------------------------

export interface ServerState {
  media_item_id: number | null
  is_playing: boolean
  active_device_id: number | null
  queue_index: number
  queue_media_item_ids: number[]
  position_seconds: number
  devices: Array<{ id: number; name: string; is_active: boolean; is_connected: boolean }>
}

export class Server {
  readonly api: APIRequestContext

  constructor(api: APIRequestContext) {
    this.api = api
  }

  static async open(): Promise<Server> {
    return new Server(await request.newContext({ baseURL: WEB_URL, storageState: STORAGE_STATE }))
  }

  async state(): Promise<ServerState> {
    return (await (await this.api.get('/api/playback/state')).json()) as ServerState
  }

  async trackIds(): Promise<Record<string, number>> {
    const rows = (await (await this.api.get('/api/library/recently-added?limit=50')).json()) as Array<{ id: number; title: string }>
    return Object.fromEntries(rows.map((r) => [r.title, r.id]))
  }

  async recentlyPlayedTitles(): Promise<string[]> {
    const rows = (await (await this.api.get('/api/library/recently-played?limit=50')).json()) as Array<{ title: string }>
    return rows.map((r) => r.title)
  }

  /** Rows in the play history for these media items (read from the
   * database: recently-played lists each track once, however often played). */
  playCounts(ids: number[]): Record<number, number> {
    const out: Record<number, number> = Object.fromEntries(ids.map((id) => [id, 0]))
    if (ids.length === 0) return out
    const rows = execFileSync(
      'sqlite3',
      [join(DATA_DIR, 'atriolyric.db'), `SELECT media_item_id, COUNT(*) FROM play_history WHERE media_item_id IN (${ids.map(Number).join(',')}) GROUP BY media_item_id;`],
      { encoding: 'utf8' },
    )
    for (const line of rows.trim().split('\n').filter(Boolean)) {
      const [id, count] = line.split('|').map(Number)
      out[id] = count
    }
    return out
  }
}

export function shotPath(name: string): string {
  const dir = join(SHOTS_DIR, LAUNCH_MODE)
  mkdirSync(dir, { recursive: true })
  return join(dir, `${name}.png`)
}

/** Per-test phone (a new tab), one Chrome and one server client per worker. */
// (Playwright requires the destructuring pattern even when no fixture is
// used; the callback is named `provide` so it is not mistaken for React's
// `use` by the hooks lint rule.)
export const test = base.extend<{ phone: Phone }, { server: Server; chrome: PhoneChrome }>({
  server: [
    // oxlint-disable-next-line no-empty-pattern
    async ({}, provide) => {
      const server = await Server.open()
      await provide(server)
      await server.api.dispose()
    },
    { scope: 'worker' },
  ],
  chrome: [
    // oxlint-disable-next-line no-empty-pattern
    async ({}, provide) => {
      const chrome = await PhoneChrome.start()
      await provide(chrome)
      await chrome.close()
    },
    { scope: 'worker' },
  ],
  phone: async ({ chrome }, provide, testInfo) => {
    const phone = await openPhone(chrome)
    try {
      await provide(phone)
    } finally {
      if (testInfo.status !== testInfo.expectedStatus) {
        // The whole phone screen (system dialogs, keyboard), kept on disk.
        try {
          const png = execFileSync('adb', ['-s', SERIAL, 'exec-out', 'screencap', '-p'], { timeout: 30_000 })
          const file = shotPath(`failed-${testInfo.title.replace(/[^a-z0-9]+/gi, '-').slice(0, 60)}`)
          writeFileSync(file, png)
          console.log(`[android] phone screen at failure: ${file}`)
        } catch (error) {
          console.warn('[android] screencap failed:', error)
        }
      }
      // A test that detached and failed before reattaching: connect again.
      if (chrome.canDetach && !chrome.connected) await chrome.connect()
    }
  },
})
