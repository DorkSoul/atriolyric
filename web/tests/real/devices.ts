import { expect, type Browser, type BrowserContext, type Locator, type Page, type WebSocketRoute } from 'playwright/test'
import { spawnSync } from 'node:child_process'
import { join } from 'node:path'
import { DATA_DIR, STORAGE_STATE } from './env'

export const ALBUM_HASH = '#/albums/Real%20Artist/Real%20Album'

const CHROME_UA =
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'

export interface Device {
  /** The name the server lists this device under (read back from the UI:
   * the server de-duplicates names of devices it already knows). */
  name: string
  /** The device id the server registered this device as (null before). */
  id: number | null
  context: BrowserContext
  page: Page
  /** Every page-side playback socket this device opened, oldest first. */
  sockets: WebSocketRoute[]
  /** The matching connections to the real server. */
  serverSockets: WebSocketRoute[]
  /** Media item ids this device told the server it started playing. */
  played: number[]
  /** The `client_diagnostic` reports this device sent the server (media
   * errors, hls.js errors fatal or not), oldest first. */
  diagnostics: Array<Record<string, unknown>>
}

/** One browser context = one device: the same login (storage state) but its
 * own localStorage, so its client token and therefore its device id differ.
 * The app names a device from the user agent; "Edg" gives the second one a
 * different name from the first. */
export async function openDevice(
  browser: Browser,
  browserName: 'Chrome' | 'Edge',
  storageState: string = STORAGE_STATE,
): Promise<Device> {
  const context = await browser.newContext({
    storageState,
    userAgent: browserName === 'Edge' ? `${CHROME_UA} Edg/130.0.0.0` : CHROME_UA,
  })
  const page = await context.newPage()
  const device: Device = { name: browserName, id: null, context, page, sockets: [], serverSockets: [], played: [], diagnostics: [] }
  // A pass-through proxy for the playback socket: the real server is on the
  // other end (messages are forwarded unchanged), but the test can drop the
  // page's side of the connection to look like a lost network connection.
  await page.routeWebSocket(/\/ws\/playback$/, (ws) => {
    const server = ws.connectToServer()
    device.sockets.push(ws)
    device.serverSockets.push(server)
    ws.onMessage((message) => {
      if (typeof message === 'string' && message.includes('"client_diagnostic"')) {
        try {
          device.diagnostics.push(JSON.parse(message) as Record<string, unknown>)
        } catch {
          // Not JSON: not ours to interpret.
        }
      }
      server.send(message)
    })
    ws.onClose((code, reason) => void server.close({ code, reason }).catch(() => undefined))
    server.onMessage((message) => {
      // Read the device id the server hands out, then pass the frame on.
      if (typeof message === 'string') {
        try {
          const parsed = JSON.parse(message) as { type?: string; device_id?: number }
          if (parsed.type === 'registered' && typeof parsed.device_id === 'number') device.id = parsed.device_id
        } catch {
          // Not JSON: not ours to interpret.
        }
      }
      ws.send(message)
    })
    server.onClose((code, reason) => void ws.close({ code, reason }).catch(() => undefined))
  })
  page.on('request', (request) => {
    const match = /\/api\/stream\/(\d+)\/played$/.exec(new URL(request.url()).pathname)
    if (match && request.method() === 'POST') device.played.push(Number(match[1]))
  })
  await page.goto('/')
  await expect(page.getByRole('navigation').first()).toBeVisible()
  // Registered with the server (and so listed as a device) before any test
  // step relies on it.
  await expect.poll(() => device.sockets.length).toBeGreaterThan(0)
  await expect.poll(() => device.id).not.toBeNull()
  const listedName = async (): Promise<string | null> => {
    const state = (await (await context.request.get('/api/playback/state')).json()) as {
      devices: Array<{ id: number; name: string }>
    }
    return state.devices.find((d) => d.id === device.id)?.name ?? null
  }
  await expect.poll(listedName).not.toBeNull()
  device.name = (await listedName()) ?? browserName
  return device
}

/** The player surface at this width: the player bar below 1200px, the side
 * panel's player block at 1200px and wider (the real suite's 1280px desktop). */
export function playerBar(page: Page): Locator {
  return page.locator('.player-bar, .side-panel-player')
}

export function trackTitle(page: Page): Locator {
  return playerBar(page).locator('.track-title').first()
}

/** "playing on ..." in the player bar (phones and narrow windows; the wide
 * side panel leaves it out). */
export function remoteNote(page: Page): Locator {
  return playerBar(page).locator('.player-remote-note')
}

/** The header's device picker, which names the device that is playing (the
 * side panel does not repeat "playing on ..."). */
export function activeDeviceButton(page: Page): Locator {
  return page.getByRole('button', { name: /^Choose playback device/ })
}

interface AudioInfo {
  paused: boolean
  src: string
  time: number
}

async function audioElements(page: Page): Promise<AudioInfo[]> {
  return page.evaluate(() =>
    [...document.querySelectorAll('audio, video')].map((el) => {
      const media = el as HTMLMediaElement
      return { paused: media.paused, src: media.currentSrc || media.src, time: media.currentTime }
    }),
  )
}

/** currentTime of the element that is actually producing sound, else null. */
export async function playingTime(page: Page): Promise<number | null> {
  const playing = (await audioElements(page)).find((a) => !a.paused && a.src !== '')
  return playing ? playing.time : null
}

/** True when no media element on the page is playing. */
export async function isSilent(page: Page): Promise<boolean> {
  return (await audioElements(page)).every((a) => a.paused)
}

/** The player bar's seek slider value (seconds), as the UI shows it. */
export async function shownPosition(page: Page): Promise<number> {
  return Number((await playerBar(page).getByRole('slider', { name: 'Seek' }).getAttribute('aria-valuenow')) ?? Number.NaN)
}

/** Open the album page and press Play: this device becomes the active one. */
export async function playAlbum(device: Device): Promise<void> {
  const { page } = device
  await page.goto(`/${ALBUM_HASH}`)
  await page.locator('.hero-play-btn[aria-label="Play"]').click()
  await expect(trackTitle(page)).toContainText('One')
  await expect.poll(() => playingTime(page)).not.toBeNull()
}

/** Drop the page's end of the playback socket (the server's end stays open,
 * like a connection that died without telling the server), then wait for the
 * client to open a replacement and the server to hand it a device id. */
export async function dropSocket(device: Device): Promise<void> {
  const before = device.sockets.length
  await device.sockets[before - 1].close({ code: 3001, reason: 'test: connection lost' })
  await expect.poll(() => device.sockets.length, { timeout: 30_000 }).toBeGreaterThan(before)
  await expect(device.page.getByText('Reconnecting…')).toHaveCount(0)
}

export async function openDevicePicker(page: Page): Promise<Locator> {
  const menu = page.getByRole('menu', { name: 'Playback devices' })
  // The toggle can be replaced by a re-render (the device list arrives, the
  // player bar changes) between the click and the menu opening: retry the
  // whole open, which is idempotent once the menu is shown.
  await expect(async () => {
    if (!(await menu.isVisible())) await page.getByRole('button', { name: /^Choose playback device/ }).click({ timeout: 2_000 })
    await expect(menu).toBeVisible({ timeout: 2_000 })
  }).toPass({ timeout: 20_000 })
  return menu
}

/** Matches a device's name exactly where the UI prints it. */
export function nameOf(device: Device): RegExp {
  return new RegExp(device.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
}

export interface ListenRow {
  id: number
  title: string
  end_reason: string
  start_kind: string | null
  source_kind: string | null
  listened_seconds: number
}

// Read straight from the server's database (read-only): there is no API that
// lists listen outcomes. Python's sqlite3, from the server's environment.
const LISTEN_ROWS_SCRIPT = `
import json, sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
rows = con.execute(
    "SELECT l.id, m.title, l.end_reason, l.start_kind, l.source_kind, l.listened_seconds "
    "FROM listen_events l JOIN media_items m ON m.id = l.media_item_id ORDER BY l.id"
).fetchall()
keys = ("id", "title", "end_reason", "start_kind", "source_kind", "listened_seconds")
print(json.dumps([dict(zip(keys, row)) for row in rows]))
`

/** Every listen outcome the server has stored (listen_events), oldest first. */
export function listenRows(): ListenRow[] {
  const result = spawnSync('uv', ['run', '--frozen', 'python', '-c', LISTEN_ROWS_SCRIPT, join(DATA_DIR, 'atriolyric.db')], {
    cwd: join(process.cwd(), '..', 'server'),
    encoding: 'utf8',
    timeout: 60_000,
  })
  if (result.status !== 0) throw new Error(`reading listen_events failed: ${result.stderr}`)
  return JSON.parse(result.stdout) as ListenRow[]
}
