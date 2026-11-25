import type { Page } from 'playwright/test'
import { expect, test } from './fixtures'
import { answerDialog } from './dialogs'
import { type AppMockState, installApiMock } from './mockApi'

// The Android app's downloads, with a stand-in for the app's native side
// (window.Capacitor): a download asked for is on the "device" at once. What
// is checked here is the page's part: a downloaded playlist stays downloaded
// (what is added to it later is fetched too), a change of download quality
// asks before replacing what is on the device, and the app's own background
// fetches are not announced as loading.

interface FakeDownload {
  mediaId: number
  title: string
  kind: string
  quality: string
  state: string
}

/** Puts the stand-in in place before the page's scripts run. `primed`: the
 * app's daily background copy of the library counts as done. */
async function installFakeApp(
  page: Page,
  options: { primed?: boolean; downloads?: Partial<FakeDownload>[]; metered?: boolean } = {},
) {
  await page.addInitScript(
    ({ primed, downloads, metered }) => {
      if (primed) localStorage.setItem('atriolyric.offlinePrimedAt', String(Date.now()))
      const listeners: Record<string, ((data: unknown) => void)[]> = {}
      const items: Record<string, unknown>[] = downloads.map((download) => ({
        kind: 'track', artist: '', album: '', qualityLabel: 'Original', quality: 'original', duration: 0, addedAt: 1,
        state: 'completed', bytes: 1000, totalBytes: 1000, percent: 100, hasArt: false, artVersion: 0, ...download,
      }))
      // Every call the page makes, for the tests to look at.
      const calls: { method: string; options: Record<string, unknown> }[] = []
      const tell = () => (listeners.downloads ?? []).forEach((listener) => listener({ items: [...items] }))
      const fake = {
        isNativePlatform: () => true,
        nativePromise: async (_plugin: string, method: string, options: Record<string, unknown> = {}) => {
          calls.push({ method, options })
          if (method === 'downloadList') return { items: [...items] }
          if (method === 'downloadStart') {
            const at = items.findIndex((item) => item.mediaId === options.mediaId)
            // As the app does: a replace swaps the copy, anything else on the device stays.
            if (at >= 0 && options.replace === true) items.splice(at, 1)
            if (at < 0 || options.replace === true) {
              items.unshift({ ...options, addedAt: Date.now(), state: 'completed', bytes: 1000, totalBytes: 1000, percent: 100, hasArt: false, artVersion: 0 })
            }
            tell()
            return {}
          }
          if (method === 'networkInfo') return { connected: true, metered }
          if (method === 'downloadRemove') {
            const at = items.findIndex((item) => item.mediaId === options.mediaId)
            if (at >= 0) items.splice(at, 1)
            tell()
            return {}
          }
          if (method === 'downloadArt') return { dataUrl: null }
          if (method === 'shellState') return { serverUrl: location.origin, remote: true, reason: null, appVersion: '99.0.0' }
          return {}
        },
        addListener: (_plugin: string, event: string, callback: (data: unknown) => void) => {
          ;(listeners[event] ??= []).push(callback)
          return { remove: async () => {} }
        },
      }
      Object.assign(window, { Capacitor: fake, __fakeDownloads: items, __nativeCalls: calls })
    },
    { primed: options.primed ?? true, downloads: options.downloads ?? [], metered: options.metered ?? false },
  )
}

const nativeCalls = (page: Page, method: string) =>
  page.evaluate(
    (name) => (window as unknown as { __nativeCalls: { method: string; options: Record<string, unknown> }[] }).__nativeCalls
      .filter((call) => call.method === name)
      .map((call) => call.options),
    method,
  )

const deviceDownloads = (page: Page) =>
  page.evaluate(() => (window as unknown as { __fakeDownloads: FakeDownload[] }).__fakeDownloads.map((item) => ({ ...item })))

/** What a playlist holds, as the server lists it for the app. */
async function mockIds(page: Page, state: AppMockState) {
  await page.route(/\/api\/download\/playlist\/\d+\/ids$/, async (route) => {
    const id = Number(/playlist\/(\d+)\//.exec(route.request().url())?.[1])
    const detail = state.playlistDetails[id]
    if (!detail) return route.fulfill({ status: 404, json: { detail: 'Playlist not found' } })
    await route.fulfill({ json: [...new Set(detail.items.map((item) => item.id))] })
  })
  // A downloaded song's lyrics are kept with it.
  await page.route(/\/api\/library\/media\/\d+\/lyrics$/, (route) => route.fulfill({ json: { pending: false, lines: [] } }))
}

const onDevice = async (page: Page) => (await deviceDownloads(page)).map((d) => d.mediaId).sort((a, b) => a - b)

async function playlistMenu(page: Page, name: string, item: string) {
  await page.getByRole('button', { name: `More actions for ${name}`, exact: true }).click()
  await page.getByRole('menuitem', { name: item, exact: true }).click()
}

async function addToPlaylist(page: Page, song: string, playlist: string) {
  await page.getByRole('button', { name: `More actions for ${song}`, exact: true }).click()
  await page.getByRole('menuitem', { name: 'Add to playlist' }).click()
  await page.getByRole('menuitem', { name: playlist, exact: true }).click()
  await expect(page.locator('.toast').filter({ hasText: `to "${playlist}"` })).toBeVisible()
}

test('a downloaded playlist is marked, and a song added to it later is downloaded by itself', async ({ page }) => {
  const state = await installApiMock(page)
  await mockIds(page, state)
  await installFakeApp(page)
  await page.goto('/#/playlists')

  const card = page.locator('.browse-card').filter({ hasText: 'Workout' })
  await expect(card).toBeVisible()
  await expect(card.getByTestId('download-mark')).toHaveCount(0)
  await playlistMenu(page, 'Workout', 'Download to this device')
  await expect(page.locator('.toast').filter({ hasText: 'Anything added to it later will download automatically.' })).toBeVisible()
  await expect(card.getByTestId('download-mark')).toHaveAttribute('data-status', 'downloaded')
  const inPlaylist = state.playlistDetails[2].items.map((item) => item.id).sort((a, b) => a - b)
  await expect.poll(() => onDevice(page)).toEqual(inPlaylist)

  // Downloaded: offered for removal, not for a second download.
  await page.getByRole('button', { name: 'More actions for Workout', exact: true }).click()
  await expect(page.getByRole('menuitem', { name: 'Remove download' })).toBeVisible()
  await expect(page.getByRole('menuitem', { name: 'Download to this device' })).toHaveCount(0)
  await page.keyboard.press('Escape')

  // A song added to the playlist later reaches the device with nothing more asked.
  const added = state.tracks.find((track) => track.kind === 'track' && !inPlaylist.includes(track.id))!
  await page.goto('/#/songs')
  await addToPlaylist(page, added.title, 'Workout')
  await expect.poll(() => onDevice(page), { timeout: 15_000 }).toContain(added.id)

  // Removing the playlist's download takes all of it off the device and unmarks it.
  await page.goto('/#/playlists')
  await playlistMenu(page, 'Workout', 'Remove download')
  await expect.poll(async () => (await deviceDownloads(page)).length).toBe(0)
  await expect(card.getByTestId('download-mark')).toHaveCount(0)
})

test('a song that leaves a downloaded playlist leaves the device, unless something else downloaded holds it', async ({ page }) => {
  const state = await installApiMock(page)
  await mockIds(page, state)
  await installFakeApp(page)
  // Two playlists that share a song: Workout holds [a, b], Second holds [b, c].
  const [a, b] = state.playlistDetails[2].items.map((item) => item.id)
  const c = state.tracks.find((track) => track.kind === 'track' && track.id !== a && track.id !== b)!
  const single = { ...c, id: 900, title: 'Loose Song' }
  state.tracks.push(single)
  const title = (id: number) => state.tracks.find((track) => track.id === id)!.title
  state.playlists.push({ id: 50, name: 'Second', created_at: '2026-02-01T00:00:00Z', item_count: 2, is_auto_generated: false, auto_kind: null, preview_media_item_ids: [] })
  state.playlistDetails[50] = { id: 50, name: 'Second', created_at: '2026-02-01T00:00:00Z', items: [{ ...state.playlistDetails[2].items[1], playlist_item_id: 501 }, { ...c, playlist_item_id: 502 }] }

  await page.goto('/#/playlists')
  await playlistMenu(page, 'Workout', 'Download to this device')
  await expect.poll(() => onDevice(page)).toEqual([a, b].sort((x, y) => x - y))
  await playlistMenu(page, 'Second', 'Download to this device')
  // The shared song is on the device once.
  await expect.poll(() => onDevice(page)).toEqual([a, b, c.id].sort((x, y) => x - y))
  // A song downloaded on its own, which Workout will also hold for a while.
  await page.goto('/#/songs')
  await page.getByRole('button', { name: `More actions for ${single.title}`, exact: true }).click()
  await page.getByRole('menuitem', { name: 'Download to this device' }).click()
  await expect.poll(() => onDevice(page)).toContain(single.id)
  await addToPlaylist(page, single.title, 'Workout')

  // Everything leaves Workout (as another device, or the server, would do it);
  // the next change made here has the app look again.
  state.playlistDetails[2].items = []
  await addToPlaylist(page, title(c.id), 'Second')
  // a was only in Workout: gone. b is still in Second, and the single
  // download was asked for by itself: both stay.
  await expect.poll(() => onDevice(page), { timeout: 15_000 }).toEqual([b, c.id, single.id].sort((x, y) => x - y))

  // Second's download removed: its songs go, the single download stays.
  await page.goto('/#/playlists')
  await playlistMenu(page, 'Second', 'Remove download')
  await expect.poll(() => onDevice(page)).toEqual([single.id])
})

test('a generated mix is not downloaded: it is saved as a playlist first', async ({ page }) => {
  const state = await installApiMock(page)
  await mockIds(page, state)
  await installFakeApp(page)
  await page.goto('/#/playlists')
  await playlistMenu(page, 'Rock Mix', 'Download to this device')
  await expect(page.locator('.toast').filter({ hasText: 'Save it as a playlist, then download the playlist.' })).toBeVisible()
  await page.waitForTimeout(500)
  expect(await deviceDownloads(page)).toEqual([])
  await expect(page.locator('.browse-card').filter({ hasText: 'Rock Mix' }).getByTestId('download-mark')).toHaveCount(0)
})

test('changing the download quality asks first, then replaces what is on the device', async ({ page }) => {
  const state = await installApiMock(page)
  await mockIds(page, state)
  const song = state.tracks.find((track) => track.kind === 'track')!
  await installFakeApp(page, { downloads: [{ mediaId: song.id, title: song.title }] })
  await page.goto('/#/settings/downloads')

  const music = page.getByRole('group', { name: 'Music download quality' })
  await expect(page.getByTestId('download-row')).toHaveCount(1)
  await music.getByRole('button', { name: '192 kbps' }).click()
  const question = await answerDialog(page, 'dismiss')
  expect(question).toContain('1 song on this device was saved at another quality')
  expect(question).toContain('deleted and downloaded again at 192 kbps')
  // Not agreed to: nothing changed.
  await expect(music.getByRole('button', { name: 'Original' })).toHaveAttribute('aria-pressed', 'true')
  expect(await deviceDownloads(page)).toMatchObject([{ mediaId: song.id, quality: 'original' }])

  await music.getByRole('button', { name: '192 kbps' }).click()
  await answerDialog(page, 'accept')
  await expect(music.getByRole('button', { name: '192 kbps' })).toHaveAttribute('aria-pressed', 'true')
  await expect.poll(async () => deviceDownloads(page), { timeout: 15_000 }).toMatchObject([{ mediaId: song.id, quality: '192' }])
  // Swapped in one step by the app (which finishes it after a restart), not
  // removed first and fetched again by the page.
  expect(await nativeCalls(page, 'downloadRemove')).toEqual([])
  expect(await nativeCalls(page, 'downloadStart')).toMatchObject([{ mediaId: song.id, quality: '192', replace: true }])

  // Back to the quality it now has, with nothing at another one: no question.
  await music.getByRole('button', { name: '192 kbps' }).click()
  await expect(page.locator('.confirm-dialog')).toHaveCount(0)
})

test('Wi-Fi only is sent to the app, and on mobile data a downloaded playlist waits to fetch what it gains', async ({ page }) => {
  const state = await installApiMock(page)
  await mockIds(page, state)
  await installFakeApp(page, { metered: true })
  await page.goto('/#/settings/downloads')
  const wifi = page.getByRole('switch', { name: 'Download over Wi-Fi only' })
  await expect(wifi).not.toBeChecked()
  await wifi.click()
  await expect(wifi).toBeChecked()
  await expect.poll(() => nativeCalls(page, 'downloadSetRequirements')).toContainEqual({ wifiOnly: true })

  // Asked for by hand: queued with the app (which holds it until Wi-Fi).
  await page.goto('/#/playlists')
  await playlistMenu(page, 'Workout', 'Download to this device')
  const inPlaylist = state.playlistDetails[2].items.map((item) => item.id).sort((a, b) => a - b)
  await expect.poll(() => onDevice(page)).toEqual(inPlaylist)
  // Gained later, on mobile data: the background sync waits for Wi-Fi.
  const added = state.tracks.find((track) => track.kind === 'track' && !inPlaylist.includes(track.id))!
  await page.goto('/#/songs')
  await addToPlaylist(page, added.title, 'Workout')
  await page.waitForTimeout(5000)
  expect(await onDevice(page)).not.toContain(added.id)
})

test("the app's background copy of the library is not announced as loading", async ({ page }) => {
  await installApiMock(page)
  await installFakeApp(page, { primed: false })
  // The copy waits half a minute after the start (the playback connection goes first).
  test.setTimeout(90_000)
  await page.route(/\/api\/library\/labels\/all$/, (route) => route.fulfill({ json: { genres: {}, tags: {} } }))
  // The copy's first page of songs takes its time, then the copy stops there.
  let asked = 0
  await page.route(/\/api\/library\/tracks\?limit=500&offset=0$/, async (route) => {
    asked += 1
    await new Promise((resolve) => setTimeout(resolve, 6000))
    await route.fulfill({ status: 401, json: { detail: 'Not signed in' } })
  })
  await page.goto('/#/settings')
  await expect.poll(() => asked, { timeout: 50_000 }).toBe(1)
  // Well past the moment a slow request the user waits for is named (and
  // while this one is still unanswered).
  await page.waitForTimeout(2500)
  // Counted once, now: an assertion that waits would outlast the slow request.
  expect(await page.locator('.loading-status').count()).toBe(0)
  await expect(page.getByRole('heading', { name: 'Settings' }).first()).toBeVisible()
})

/** Counts the app's checks of its server (GET /api/version) and answers
 * them as `answers` says, check by check; the last entry goes on counting
 * for every later check. */
async function countChecks(page: Page, answers: boolean[]) {
  const seen = { checks: 0 }
  await page.route(/\/api\/version$/, async (route) => {
    const answered = answers[Math.min(seen.checks, answers.length - 1)]
    seen.checks += 1
    if (answered) return route.fulfill({ json: { version: null } })
    await route.abort('internetdisconnected').catch(() => {})
  })
  return seen
}

test('one unanswered check of the server does not take the app offline', async ({ page }) => {
  await installApiMock(page)
  await installFakeApp(page)
  const seen = await countChecks(page, [false, true])
  // Whatever else answers stays out of it: only the second check says the server is there.
  await page.addInitScript(() => {
    Object.assign(window, { __offlineNoticeSeen: false })
    new MutationObserver(() => {
      if (document.querySelector('[data-testid="offline-notice"]')) Object.assign(window, { __offlineNoticeSeen: true })
    }).observe(document, { childList: true, subtree: true })
  })
  // One request with no answer starts the check.
  let dropped = false
  await page.route(/\/api\/playlists$/, async (route) => {
    if (dropped) return route.fallback()
    dropped = true
    await route.abort('internetdisconnected').catch(() => {})
  })
  await page.goto('/#/settings')
  await expect.poll(() => seen.checks, { timeout: 15_000 }).toBeGreaterThanOrEqual(2)
  await page.waitForTimeout(1000)
  expect(await page.evaluate(() => (window as unknown as { __offlineNoticeSeen: boolean }).__offlineNoticeSeen)).toBe(false)
})

test('a server that answers nothing, twice over, takes the app offline', async ({ page }) => {
  await installApiMock(page)
  await installFakeApp(page)
  await page.goto('/#/settings')
  await expect(page.getByRole('heading', { name: 'Settings' }).first()).toBeVisible()
  // From here nothing is answered: not the checks, not anything else.
  const seen = await countChecks(page, [false])
  await page.route(/\/api\/(?!version$)/, (route) => route.abort('internetdisconnected').catch(() => {}))
  await page.evaluate(() => { location.hash = '#/albums' })
  await expect(page.getByTestId('offline-notice')).toBeVisible({ timeout: 15_000 })
  expect(seen.checks).toBeGreaterThanOrEqual(2)
})
