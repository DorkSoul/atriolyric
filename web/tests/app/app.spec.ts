import { copyFileSync, mkdirSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { expect, test, type Page } from 'playwright/test'
import { ALBUM_HASH, shownPosition, trackTitle } from '../real/devices'
import { VIDEO_TITLE } from '../real/make-video'
import { PASSWORD, STORAGE_STATE, USERNAME, WEB_URL } from '../real/env'
import { adb, PHONE_PORT, screenOff, screenOn, shell, wakefulness } from '../android/adb'
import { Server } from '../android/phone'
import {
  activeSlot,
  APK,
  APP_VIDEO_TITLE,
  attach,
  detach,
  deviceDownloads,
  hasMediaNotification,
  mediaKey,
  mediaSession,
  native,
  OFFERED_APK,
  PACKAGE,
  RADIO_STATION,
  setAeroplaneMode,
  SHELL_ORIGIN,
  slots,
  type Slot,
  startApp,
  stopApp,
  swipeAway,
  topActivity,
} from './app'

// The Android app on the emulator, in order: each test builds on the state
// the one before left (signed in, downloads on the device).
test.describe.configure({ mode: 'serial' })

const onServer = (url: string) => url.startsWith(WEB_URL)
const onShell = (url: string) => url.startsWith(SHELL_ORIGIN)

let server: Server
let ids: Record<string, number>

function note(label: string, value: unknown) {
  console.log(`[app] ${label}: ${typeof value === 'string' ? value : JSON.stringify(value)}`)
}

async function position(page: Page): Promise<number> {
  return (await activeSlot(page))?.position ?? -1
}

test.beforeAll(async () => {
  server = await Server.open()
  ids = await server.trackIds()
  note('WebView', shell('dumpsys package com.google.android.webview').match(/versionName=(\S+)/)?.[1] ?? 'unknown')
})

test.afterAll(async () => {
  await detach()
  await server.api.dispose()
})

test('1 first run: the app asks for the server, then loads it with the native bridge', async () => {
  startApp()
  const shellPage = await attach(onShell)
  await expect(shellPage.getByRole('heading', { name: 'Connect to your server' })).toBeVisible()
  await shellPage.getByLabel('Server address').fill(WEB_URL)
  await shellPage.getByRole('button', { name: 'Connect' }).click()

  // The app swaps the bundled page for the server's own.
  const page = await attach(onServer, 60_000)
  await page.getByPlaceholder('Username').fill(USERNAME)
  await page.getByPlaceholder('Password').fill(PASSWORD)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('navigation').first()).toBeVisible({ timeout: 30_000 })
  const state = await native<{ serverUrl: string; remote: boolean }>(page, 'shellState')
  expect(state).toMatchObject({ serverUrl: WEB_URL, remote: true })
  note('shell state', state)
})

test('2 a queue plays through the native service and advances with the screen off; notification and lock-screen controls work', async () => {
  let page = await attach(onServer)
  await page.goto(`${WEB_URL}/${ALBUM_HASH}`)
  await page.locator('.hero-play-btn[aria-label="Play"]').click()
  await expect(trackTitle(page)).toContainText('One')

  // The sound comes from the native player, not from an element in the page.
  await expect.poll(async () => (await activeSlot(page))?.playing, { timeout: 30_000 }).toBe(true)
  const first = await activeSlot(page)
  expect(first?.mediaId).toBe(ids.One)
  const t0 = await position(page)
  await expect.poll(() => position(page)).toBeGreaterThan(t0 + 1)
  const pageAudio = await page.evaluate(() => [...document.querySelectorAll('audio')].map((a) => a.getAttribute('src') ?? ''))
  // Neither music element was ever given a source of its own to load.
  expect(await page.evaluate(() => [...document.querySelectorAll('audio[data-player-element]')].every((a) => !a.hasAttribute('src')))).toBe(true)

  // Android has a playing media session for the app, with the track's details, and its notification.
  await expect.poll(() => mediaSession().state).toBe('PLAYING')
  expect(mediaSession().description).toContain('One')
  await expect.poll(() => hasMediaNotification()).toBe(true)
  note('playing', { slot: first, session: mediaSession(), notification: hasMediaNotification(), pageAudio })

  // Screen off and locked, with nothing attached to the page. "One" (20 s)
  // ends, "Two" (3 s) plays and ends, "Three" starts: two advances.
  await detach()
  screenOff()
  await expect.poll(() => wakefulness(), { timeout: 10_000 }).toBe('Asleep')
  await expect.poll(() => mediaSession().description, { timeout: 45_000, intervals: [500] }).toContain('Two')
  note('screen off, second track', mediaSession())
  await expect.poll(() => mediaSession().description, { timeout: 30_000, intervals: [500] }).toContain('Three')
  await expect.poll(() => mediaSession().state).toBe('PLAYING')
  note('screen off, third track', { ...mediaSession(), wakefulness: wakefulness() })

  // The controls a lock screen or a headset sends, still with the screen off.
  mediaKey('pause')
  await expect.poll(() => mediaSession().state).toBe('PAUSED')
  mediaKey('play')
  await expect.poll(() => mediaSession().state).toBe('PLAYING')
  mediaKey('next')
  await expect.poll(() => mediaSession().description, { timeout: 30_000 }).toContain('Four')
  await expect.poll(() => mediaSession().state).toBe('PLAYING')
  expect(wakefulness()).toBe('Asleep')
  note('lock-screen controls', mediaSession())

  screenOn()
  page = await attach(onServer)
  await expect(trackTitle(page)).toContainText('Four')
  // The server followed every step: it is on the fourth track, and the two
  // tracks that played to their end are in the play history.
  await expect.poll(async () => (await server.state()).media_item_id).toBe(ids.Four)
  await expect.poll(() => server.playCounts([ids.One, ids.Two])).toEqual({ [ids.One]: 1, [ids.Two]: 1 })
  expect((await activeSlot(page))?.mediaId).toBe(ids.Four)
  await page.locator('.player-bar, .side-panel-player').getByRole('button', { name: 'Pause', exact: true }).first().click()
  await expect.poll(() => mediaSession().state).toBe('PAUSED')
})

test('2a a short loss of signal does not stop streamed music, and once the signal is back the queue carries on with the server', async () => {
  // Nothing in the album is downloaded yet (test 3 does that). As in a
  // browser, the tracks ahead are kept: the playing and next ones in the two
  // native players, the ones after on disk. "One" is 20 s, "Two" 3 s, "Three" and "Four" 20 s each.
  const page = await attach(onServer)
  await page.goto(`${WEB_URL}/${ALBUM_HASH}`)
  await page.locator('.hero-play-btn[aria-label="Play"]').click()
  await expect(trackTitle(page)).toContainText('One')
  await expect.poll(async () => (await activeSlot(page))?.playing, { timeout: 30_000 }).toBe(true)
  await expect.poll(async () => (await native<{ mediaIds: number[] }>(page, 'streamHeld')).mediaIds.sort(), { timeout: 15_000 })
    .toEqual([ids.Three, ids.Four].sort())
  note('held ahead', { held: await native(page, 'streamHeld'), slots: await slots(page) })

  // Disconnection: no radios and no tunnel to this machine, the server is gone.
  setAeroplaneMode(true)
  adb('reverse', '--remove-all')
  let offline = true
  try {
    await expect(page.getByTestId('offline-notice')).toBeVisible({ timeout: 30_000 })
    note('offline while playing', { slot: await activeSlot(page), session: mediaSession() })
    await expect.poll(() => mediaSession().state).toBe('PLAYING')
    // The playing track plays to its end, the next one (in the standby
    // player) after it, then the one after that from the copy on disk.
    await expect.poll(() => mediaSession().description, { timeout: 30_000, intervals: [500] }).toContain('Two')
    await expect.poll(() => mediaSession().description, { timeout: 15_000, intervals: [500] }).toContain('Three')
    await expect.poll(() => mediaSession().state).toBe('PLAYING')
    const t0 = await position(page)
    await expect.poll(() => position(page), { timeout: 10_000 }).toBeGreaterThan(t0 + 2)
    expect(await activeSlot(page)).toMatchObject({ mediaId: ids.Three, error: null })
    expect(await page.getByTestId('offline-notice').isVisible()).toBe(true)
    note('offline, third track from disk', { slot: await activeSlot(page), session: mediaSession() })

    // Reconnection, while "Three" still plays: the tunnel first, so the
    // app's check when the radios come back already finds the server.
    adb('reverse', `tcp:${PHONE_PORT}`, `tcp:${PHONE_PORT}`)
    setAeroplaneMode(false)
    offline = false
    // Watched every 250 ms until 3 s after the app is back: the music must
    // not stop, restart or jump back for the change-over. (What Android's
    // media session reports can read PAUSED for a moment while the server
    // takes the queue back, with the player itself playing on: the native
    // player is what is checked.)
    const samples: { slot?: Slot; music: string; session: string | null; notice: number }[] = []
    let backAt = 0
    for (const deadline = Date.now() + 60_000; Date.now() < deadline && (!backAt || Date.now() < backAt + 3_000);) {
      const all = await slots(page)
      const notice = await page.getByTestId('offline-notice').count()
      if (!notice && !backAt) backAt = Date.now()
      const music = all.filter((x) => x.slot === 'a' || x.slot === 'b').map((x) => `${x.slot}:${x.mediaId}${x.playing ? ' playing' : ''}`).join(', ')
      samples.push({ slot: all.find((x) => x.active), music, session: mediaSession().state, notice })
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
    note('reconnecting', samples.map((x) => `${x.slot?.slot}/${x.slot?.loadId} m=${x.slot?.mediaId} ${x.slot?.position.toFixed(1)}s [${x.music}] session=${x.session} notice=${x.notice}`))
    expect(backAt).toBeGreaterThan(0)
    expect(samples.filter((x) => !x.slot?.playing || x.slot.error)).toEqual([])
    for (let i = 1; i < samples.length; i += 1) {
      const [was, now] = [samples[i - 1].slot!, samples[i].slot!]
      if (now.mediaId !== was.mediaId) continue
      expect(now.loadId).toBe(was.loadId)
      expect(now.position).toBeGreaterThanOrEqual(was.position - 0.3)
    }
  } finally {
    if (offline) {
      adb('reverse', `tcp:${PHONE_PORT}`, `tcp:${PHONE_PORT}`)
      setAeroplaneMode(false)
    }
  }
  note('back online', { slot: await activeSlot(page), session: mediaSession() })
  // The server takes the device's place in the queue and follows it on:
  // "Four" plays with the server connected, and the server says so.
  await expect.poll(async () => {
    const state = await server.state()
    return [state.media_item_id, state.is_playing]
  }, { timeout: 45_000 }).toEqual([ids.Four, true])
  await expect.poll(() => mediaSession().description, { timeout: 30_000 }).toContain('Four')
  const f0 = await position(page)
  await expect.poll(() => position(page), { timeout: 10_000 }).toBeGreaterThan(f0 + 1)
  expect(await activeSlot(page)).toMatchObject({ mediaId: ids.Four, playing: true, error: null })
  note('online again, fourth track', { server: await server.state().then((s) => [s.media_item_id, s.is_playing, s.position_seconds]), slot: await activeSlot(page) })
  await page.locator('.player-bar, .side-panel-player').getByRole('button', { name: 'Pause', exact: true }).first().click()
  await expect.poll(() => mediaSession().state).toBe('PAUSED')
})

test('2b with the screen off for well over a minute the queue still advances and Next from a headset still works: the page is not frozen', async () => {
  // The music plays natively, so to Chromium the hidden page is silent; a
  // newer WebView can freeze it a minute after it is hidden, which must not
  // stop the queue at the end of the track playing. The emulator's WebView
  // 124 does not freeze it, so here this only guards the behaviour. The album runs 63 s; with Repeat all it goes on to "One" and
  // "Two" again after that.
  let page = await attach(onServer)
  await page.goto(`${WEB_URL}/${ALBUM_HASH}`)
  await page.locator('.hero-play-btn[aria-label="Play"]').click()
  await expect(trackTitle(page)).toContainText('One')
  await expect.poll(async () => (await activeSlot(page))?.playing, { timeout: 30_000 }).toBe(true)
  await page.getByRole('button', { name: 'Open now playing' }).first().click()
  const nowPlaying = page.locator(':is(.nowplaying-overlay, .nowplaying-page)')
  await nowPlaying.getByRole('button', { name: 'Repeat off' }).click()
  await expect(nowPlaying.getByRole('button', { name: 'Repeat all' })).toBeVisible()

  await detach()
  screenOff()
  await expect.poll(() => wakefulness(), { timeout: 10_000 }).toBe('Asleep')
  const asleepAt = Date.now()
  await expect.poll(() => mediaSession().description, { timeout: 90_000, intervals: [1000] }).toContain('Four')
  // Past the minute: "Four" ends and the queue starts over.
  await expect.poll(() => mediaSession().description, { timeout: 45_000, intervals: [1000] }).toContain('One')
  note('screen off, after the minute', { seconds: (Date.now() - asleepAt) / 1000, session: mediaSession() })
  await expect.poll(() => mediaSession().description, { timeout: 45_000, intervals: [1000] }).toContain('Two')
  await expect.poll(() => mediaSession().state).toBe('PLAYING')
  note('screen off, still advancing', { seconds: (Date.now() - asleepAt) / 1000, session: mediaSession(), wakefulness: wakefulness() })

  // Paused with the screen off for over a minute (now the page may be
  // frozen), then Next from a headset: it is carried out at once.
  mediaKey('pause')
  await expect.poll(() => mediaSession().state).toBe('PAUSED')
  await new Promise((resolve) => setTimeout(resolve, 70_000))
  const before = mediaSession().description
  mediaKey('next')
  await expect.poll(() => mediaSession().description, { timeout: 15_000, intervals: [500] }).not.toBe(before)
  note('next from a headset after a paused minute', { before, after: mediaSession() })

  screenOn()
  page = await attach(onServer)
  // Repeat back off (all -> one -> off) and the music paused, for the tests after this one.
  const nowPlayingAgain = page.locator(':is(.nowplaying-overlay, .nowplaying-page)')
  if (!(await nowPlayingAgain.isVisible())) await page.getByRole('button', { name: 'Open now playing' }).first().click()
  await nowPlayingAgain.getByRole('button', { name: 'Repeat all' }).click()
  await nowPlayingAgain.getByRole('button', { name: 'Repeat one' }).click()
  await expect(nowPlayingAgain.getByRole('button', { name: 'Repeat off' })).toBeVisible()
  // Left on "Four", paused, as test 2 leaves it ("Three" is downloaded next,
  // and must not already be loaded from the stream).
  mediaKey('next')
  await expect.poll(() => mediaSession().description, { timeout: 15_000 }).toContain('Four')
  mediaKey('pause')
  await expect.poll(() => mediaSession().state).toBe('PAUSED')
  await page.goBack().catch(() => undefined)
})

test('3 songs and a video download at the download quality, which is set apart from the streaming quality', async () => {
  const page = await attach(onServer)
  await page.goto(`${WEB_URL}/#/settings/downloads`)
  const music = page.getByRole('group', { name: 'Music download quality' })
  const video = page.getByRole('group', { name: 'Video download quality' })
  const streaming = page.getByRole('group', { name: 'Streaming quality' })
  await music.getByRole('button', { name: '96 kbps' }).click()
  await video.getByRole('button', { name: '480p' }).click()
  await expect(music.getByRole('button', { name: '96 kbps' })).toHaveAttribute('aria-pressed', 'true')
  await expect(video.getByRole('button', { name: '480p' })).toHaveAttribute('aria-pressed', 'true')
  // The streaming quality did not move with them.
  await expect(streaming.getByRole('button', { name: 'Original' })).toHaveAttribute('aria-pressed', 'true')

  // Two songs, from their row menus.
  await page.goto(`${WEB_URL}/#/songs`)
  for (const title of ['Three', 'Four']) {
    await page.getByRole('button', { name: `More actions for ${title}`, exact: true }).click()
    await page.getByRole('menuitem', { name: 'Download to this device' }).click()
  }
  // The 720p clip, from its menu: the server prepares a 480p file first.
  await page.goto(`${WEB_URL}/#/videos`)
  await page.getByRole('button', { name: new RegExp(`More actions for ${APP_VIDEO_TITLE}`) }).first().click()
  await page.getByRole('menuitem', { name: 'Download to this device' }).click()

  await page.goto(`${WEB_URL}/#/settings/downloads`)
  const row = (title: string) => page.getByTestId('download-row').filter({ hasText: title })
  for (const title of ['Three', 'Four', APP_VIDEO_TITLE]) {
    await expect(row(title)).toHaveAttribute('data-state', 'completed', { timeout: 120_000 })
  }
  await expect(row('Three').getByTestId('download-quality')).toHaveText('96 kbps')
  await expect(row(APP_VIDEO_TITLE).getByTestId('download-quality')).toHaveText('480p')

  const downloads = await deviceDownloads(page)
  note('downloads on the device', downloads.map((d) => ({ title: d.title, kind: d.kind, quality: d.qualityLabel, bytes: d.bytes, state: d.state })))
  const three = downloads.find((d) => d.title === 'Three')
  // 20 s of 16-bit 8 kHz mono WAV is 320 kB; the 96 kbps copy is smaller.
  expect(three?.bytes).toBeGreaterThan(50_000)
  expect(three?.bytes).toBeLessThan(300_000)
  expect(downloads.find((d) => d.title === APP_VIDEO_TITLE)).toMatchObject({ quality: '480', state: 'completed' })
  // The stream of a track that is not downloaded is still asked for at the streaming quality (no cap).
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('atriolyric.settings') ?? '{}').maxBitrateKbps ?? null)).toBeNull()

  // Meanwhile the app has fetched the whole library in the background, for use with no connection.
  await expect.poll(() => page.evaluate(() => localStorage.getItem('atriolyric.offlinePrimedAt')), { timeout: 90_000 }).not.toBeNull()
  // That includes which songs carry which genre and tag.
  const kept = await page.evaluate(() => new Promise<string[]>((resolve) => {
    const open = indexedDB.open('atriolyric-offline')
    open.onsuccess = () => {
      const keys = open.result.transaction('answers').objectStore('answers').getAllKeys()
      keys.onsuccess = () => resolve(keys.result.map(String))
    }
  }))
  expect(kept).toContain('/api/library/labels/all')
  expect(kept).toContain('offline:tracks')

  // What is on the device is marked in the lists, and is offered for removal, not for a second download.
  await page.goto(`${WEB_URL}/#/songs`)
  const songRow = (title: string) => page.locator('.track-row').filter({ has: page.getByRole('button', { name: `Play ${title}`, exact: true }) })
  await expect(songRow('Three').getByTestId('download-mark')).toHaveAttribute('data-status', 'downloaded')
  await expect(songRow('One').getByTestId('download-mark')).toHaveCount(0)
  await page.getByRole('button', { name: 'More actions for Three', exact: true }).click()
  await expect(page.getByRole('menuitem', { name: 'Remove download' })).toBeVisible()
  await expect(page.getByRole('menuitem', { name: 'Download to this device' })).toHaveCount(0)
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: 'More actions for One', exact: true }).click()
  await expect(page.getByRole('menuitem', { name: 'Download to this device' })).toBeVisible()
  await page.keyboard.press('Escape')
  // Asking the device again changes nothing: the same download, not a new one.
  const before = (await deviceDownloads(page)).find((d) => d.title === 'Three')
  await native(page, 'downloadStart', { mediaId: ids.Three, kind: 'track', url: `${WEB_URL}/api/stream/${ids.Three}`, quality: 'original', qualityLabel: 'Original', title: 'Three' })
  await new Promise((r) => setTimeout(r, 1500))
  expect((await deviceDownloads(page)).find((d) => d.title === 'Three')).toMatchObject({ state: 'completed', quality: before?.quality, bytes: before?.bytes })

  // With the server right there, the copy on the device is still the one that plays.
  await songRow('Three').getByRole('button', { name: 'Play Three', exact: true }).click()
  await expect.poll(async () => (await activeSlot(page))?.playing, { timeout: 30_000 }).toBe(true)
  expect(await activeSlot(page)).toMatchObject({ mediaId: ids.Three, downloaded: true })
  await page.locator('.player-bar, .side-panel-player').getByRole('button', { name: 'Pause', exact: true }).first().click()
  await expect.poll(() => mediaSession().state).toBe('PAUSED')
})

test('4 the downloads screen shows size and quality for each item and removes one', async () => {
  const page = await attach(onServer)
  await page.goto(`${WEB_URL}/#/settings/downloads`)
  const rows = page.getByTestId('download-row')
  await expect(rows).toHaveCount(3)
  for (const title of ['Three', 'Four', APP_VIDEO_TITLE]) {
    const row = rows.filter({ hasText: title })
    await expect(row.getByTestId('download-size')).toHaveText(/^\d+(\.\d)? (MB|GB)$/)
    await expect(row.getByTestId('download-quality')).toHaveText(/^(96 kbps|480p)$/)
  }
  note('rows', await rows.allInnerTexts())
  await expect(page.getByTestId('downloads-summary')).toContainText('3 items')

  await page.getByRole('button', { name: 'Remove Four from this device' }).click()
  // Removing asks first (getting it back means downloading it again).
  await page.locator('.confirm-dialog .confirm-dialog-actions button').last().click()
  await expect(rows).toHaveCount(2)
  await expect(rows.filter({ hasText: 'Four' })).toHaveCount(0)
  await expect.poll(async () => (await deviceDownloads(page)).map((d) => d.title).sort()).toEqual([APP_VIDEO_TITLE, 'Three'].sort())
  await expect(page.getByTestId('downloads-summary')).toContainText('2 items')
})

test('5 aeroplane mode, cold start: the same app opens, shows what is downloaded and plays it; back online the server carries on from there', async () => {
  await detach()
  stopApp()
  // No radios, and no adb tunnel to this machine either: nothing can reach the server.
  setAeroplaneMode(true)
  adb('reverse', '--remove-all')
  let page: Page
  const songRow = (title: string) => page.locator('.track-row').filter({ has: page.getByRole('button', { name: `Play ${title}`, exact: true }) })
  try {
    await expect.poll(() => shell('settings get global airplane_mode_on').trim()).toBe('1')
    expect(shell(`pidof ${PACKAGE} || true`).trim()).toBe('')
    startApp()
    // The web app itself, at the server's address, from the app's own copy: no separate offline page.
    page = await attach(onServer, 60_000)
    await expect(page.getByRole('navigation').first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByTestId('offline-notice')).toBeVisible({ timeout: 30_000 })
    note('offline start', { url: page.url(), notice: await page.getByTestId('offline-notice').innerText() })

    // The library as it was last loaded: what is on the device is marked, the rest is greyed and does not play.
    await page.goto(`${WEB_URL}/#/songs`)
    await expect(songRow('Three').getByTestId('download-mark')).toHaveAttribute('data-status', 'downloaded')
    await expect(songRow('Three')).not.toHaveClass(/track-row-unavailable/)
    await expect(songRow('One')).toHaveClass(/track-row-unavailable/)
    await songRow('One').getByRole('button', { name: 'Play One', exact: true }).click()
    await expect(page.locator('.toast').filter({ hasText: 'Not downloaded' })).toBeVisible()
    expect((await slots(page)).some((slot) => slot.playing)).toBe(false)

    // Lists that were never opened while connected are filled from the app's
    // copy of the library: the artists, and one artist's songs.
    await page.goto(`${WEB_URL}/#/artists`)
    // (Other tabs stay mounted, hidden, and name the artist too: the visible one.)
    const shown = (text: string) => page.locator('.app-main').getByText(text).filter({ visible: true }).first()
    await expect(shown('Real Artist')).toBeVisible({ timeout: 30_000 })
    await shown('Real Artist').click()
    await expect(shown('Real Album')).toBeVisible({ timeout: 30_000 })
    note('offline artists', await page.locator('.app-main').innerText().then((text) => text.slice(0, 200)))

    // The downloads screen is the app's own, too.
    await page.goto(`${WEB_URL}/#/settings/downloads`)
    const rows = page.getByTestId('download-row')
    await expect(rows).toHaveCount(2)
    await expect(rows.filter({ hasText: 'Three' }).getByTestId('download-quality')).toHaveText('96 kbps')
    note('offline rows', await rows.allInnerTexts())

    // The video, in the native player, at the size it was downloaded at.
    await page.goto(`${WEB_URL}/#/videos`)
    await page.getByRole('button', { name: `Play ${APP_VIDEO_TITLE}`, exact: true }).first().click()
    interface VideoState { open: boolean; playing: boolean; position: number; width: number; height: number; error: string | null }
    const videoState = () => native<VideoState>(page, 'videoState')
    await expect.poll(async () => (await videoState()).playing, { timeout: 30_000 }).toBe(true)
    const v0 = (await videoState()).position
    await expect.poll(async () => (await videoState()).position).toBeGreaterThan(v0 + 1)
    const playingVideo = await videoState()
    expect(playingVideo).toMatchObject({ open: true, height: 480, error: null })
    note('offline video', playingVideo)
    // Back closes the video player (the first press may only hide its controls).
    await expect.poll(() => {
      if (topActivity().includes('VideoActivity')) shell('input keyevent KEYCODE_BACK')
      return topActivity()
    }, { intervals: [1500] }).toContain('MainActivity')

    // The song, from the device, through the app's own player and queue.
    await page.goto(`${WEB_URL}/#/songs`)
    await songRow('Three').getByRole('button', { name: 'Play Three', exact: true }).click()
    await expect.poll(async () => (await activeSlot(page))?.playing, { timeout: 30_000 }).toBe(true)
    await expect(trackTitle(page)).toContainText('Three')
    const t0 = await position(page)
    await expect.poll(() => position(page)).toBeGreaterThan(t0 + 1)
    expect(await activeSlot(page)).toMatchObject({ mediaId: ids.Three, downloaded: true })
    await expect.poll(() => mediaSession().state).toBe('PLAYING')
    expect(mediaSession().description).toContain('Three')
    note('offline song', { slot: await activeSlot(page), session: mediaSession() })
    expect(shell('settings get global airplane_mode_on').trim()).toBe('1')
  } finally {
    setAeroplaneMode(false)
    adb('reverse', `tcp:${PHONE_PORT}`, `tcp:${PHONE_PORT}`)
  }

  // The connection is back: the notice goes, the song plays on without a
  // restart, and the server now has what this device is playing.
  const before = await position(page)
  await expect(page.getByTestId('offline-notice')).toHaveCount(0, { timeout: 60_000 })
  // (The server already had this song, paused, from test 3: playing is what the hand-over adds.)
  await expect.poll(async () => {
    const state = await server.state()
    return [state.media_item_id, state.is_playing]
  }, { timeout: 30_000 }).toEqual([ids.Three, true])
  await expect.poll(() => position(page)).toBeGreaterThan(before + 1)
  expect(await activeSlot(page)).toMatchObject({ mediaId: ids.Three, playing: true })
  await expect(songRow('One')).not.toHaveClass(/track-row-unavailable/)
  note('back online', { server: { media: (await server.state()).media_item_id, position: (await server.state()).position_seconds }, slot: await activeSlot(page) })
  await page.locator('.player-bar, .side-panel-player').getByRole('button', { name: 'Pause', exact: true }).first().click()
  await expect.poll(() => mediaSession().state).toBe('PAUSED')
})

/** A fresh start of the app with the server reachable: it lands on the server's page, signed in. */
async function openOnline(): Promise<Page> {
  await detach()
  stopApp()
  startApp()
  const page = await attach(onServer, 60_000)
  await expect(page.getByRole('navigation').first()).toBeVisible({ timeout: 30_000 })
  return page
}

test('6 the equaliser reaches the native music players', async () => {
  const page = await openOnline()
  await page.goto(`${WEB_URL}/${ALBUM_HASH}`)
  await page.locator('.hero-play-btn[aria-label="Play"]').click()
  await expect.poll(async () => (await activeSlot(page))?.playing, { timeout: 30_000 }).toBe(true)
  const music = async () => (await slots(page)).filter((slot) => slot.slot === 'a' || slot.slot === 'b')
  // Flat to begin with: nothing is processed.
  expect((await music()).every((slot) => slot.eq?.every((db) => db === 0) && slot.limiter === false)).toBe(true)

  await page.goto(`${WEB_URL}/#/settings/playback`)
  const bands = page.getByRole('group', { name: 'Equaliser bands' }).getByRole('slider')
  await expect(bands).toHaveCount(10)
  await bands.nth(0).fill('6')
  await bands.nth(5).fill('-4')
  const wanted = [6, 0, 0, 0, 0, -4, 0, 0, 0, 0]
  // Both players (the playing one and the standby) have the bands, and the limiter runs.
  await expect.poll(async () => (await music()).map((slot) => slot.eq)).toEqual([wanted, wanted])
  expect((await music()).every((slot) => slot.limiter === true)).toBe(true)
  expect((await activeSlot(page))?.playing).toBe(true)
  note('equaliser', (await music()).map((slot) => ({ slot: slot.slot, eq: slot.eq, limiter: slot.limiter, gainDb: slot.gainDb })))

  // Back to flat (so the tests after this one hear the files as they are).
  await bands.nth(0).fill('0')
  await bands.nth(5).fill('0')
  await expect.poll(async () => (await music()).every((slot) => slot.eq?.every((db) => db === 0) && slot.limiter === false)).toBe(true)
  await page.locator('.player-bar, .side-panel-player').getByRole('button', { name: 'Pause', exact: true }).first().click()
})

test('7 radio plays through the native service and carries on with the screen off', async () => {
  let page = await attach(onServer)
  await page.goto(`${WEB_URL}/#/radio`)
  await page.getByRole('button', { name: new RegExp(RADIO_STATION) }).first().click()
  const radio = async () => (await slots(page)).find((slot) => slot.slot === 'r')
  await expect.poll(async () => (await radio())?.playing, { timeout: 45_000 }).toBe(true)
  expect((await radio())?.active).toBe(true)
  await expect.poll(() => mediaSession().state).toBe('PLAYING')
  expect(mediaSession().description).toContain(RADIO_STATION)
  const before = (await radio())?.position ?? 0
  note('radio', { slot: await radio(), session: mediaSession() })

  await detach()
  screenOff()
  await expect.poll(() => wakefulness(), { timeout: 10_000 }).toBe('Asleep')
  await new Promise((r) => setTimeout(r, 12_000))
  expect(mediaSession().state).toBe('PLAYING')
  expect(wakefulness()).toBe('Asleep')
  // A lock-screen pause, then play again.
  mediaKey('pause')
  await expect.poll(() => mediaSession().state).toBe('PAUSED')
  mediaKey('play')
  await expect.poll(() => mediaSession().state, { timeout: 30_000 }).toBe('PLAYING')

  screenOn()
  page = await attach(onServer)
  const after = (await radio())?.position ?? 0
  note('radio after 12 s with the screen off', { before, after, session: mediaSession() })
  // It really played through those seconds.
  expect(after - before).toBeGreaterThan(8)
  await page.locator('.player-bar-radio').getByRole('button', { name: 'Pause', exact: true }).click()
  await expect.poll(async () => (await radio())?.playing).toBe(false)
})

test('8 a streamed video keeps its sound, natively, when the screen goes off', async () => {
  let page = await attach(onServer)
  await page.goto(`${WEB_URL}/#/videos`)
  await page.getByRole('button', { name: `Play ${VIDEO_TITLE}`, exact: true }).first().click()
  const videoTime = () => page.evaluate(() => {
    const video = document.querySelector('video')
    return video && !video.paused ? video.currentTime : -1
  })
  await expect.poll(videoTime, { timeout: 60_000 }).toBeGreaterThan(2)
  const shownBefore = await videoTime()

  await detach()
  screenOff()
  await expect.poll(() => wakefulness(), { timeout: 10_000 }).toBe('Asleep')
  // The page is hidden now; the video's sound moves to a native player, with a session.
  await expect.poll(() => mediaSession().state, { timeout: 45_000 }).toBe('PLAYING')
  expect(mediaSession().description).toContain(VIDEO_TITLE)
  await new Promise((r) => setTimeout(r, 10_000))
  expect(mediaSession().state).toBe('PLAYING')
  note('video sound with the screen off', { ...mediaSession(), wakefulness: wakefulness() })
  mediaKey('pause')
  await expect.poll(() => mediaSession().state).toBe('PAUSED')

  screenOn()
  page = await attach(onServer)
  const sound = (await slots(page)).find((slot) => slot.slot === 'v')
  const shownAfter = await page.evaluate(() => document.querySelector('video')?.currentTime ?? -1)
  note('video back on screen', { shownBefore, shownAfter, nativeSoundSlot: sound })
  // The picture comes back where the sound got to, about ten seconds on.
  expect(shownAfter - shownBefore).toBeGreaterThan(7)
})

test('9 swiping the app away stops the music; the next start shows what was playing, online and offline', async () => {
  // Online: the server's queue is what is remembered, as in the PWA.
  let page = await attach(onServer)
  await page.goto(`${WEB_URL}/${ALBUM_HASH}`)
  await page.locator('.hero-play-btn[aria-label="Play"]').click()
  await expect(trackTitle(page)).toContainText('One')
  await expect.poll(() => position(page), { timeout: 30_000 }).toBeGreaterThan(6)
  await detach()
  swipeAway()
  await expect.poll(() => mediaSession().state).not.toBe('PLAYING')
  await expect.poll(async () => (await server.state()).is_playing).toBe(false)
  note('after the swipe (online)', { session: mediaSession(), server: { is_playing: (await server.state()).is_playing, position: (await server.state()).position_seconds } })

  startApp()
  page = await attach(onServer, 60_000)
  await expect(trackTitle(page)).toContainText('One', { timeout: 30_000 })
  await expect.poll(() => shownPosition(page)).toBeGreaterThan(4)
  // Shown, not playing: nothing starts by itself.
  await new Promise((r) => setTimeout(r, 3000))
  expect((await slots(page)).some((slot) => slot.playing)).toBe(false)
  expect(mediaSession().state).not.toBe('PLAYING')
  note('reopened (online)', { title: await trackTitle(page).innerText(), shownPosition: await shownPosition(page) })

  // Offline: the app is its own playback server and remembers the same way.
  await detach()
  stopApp()
  setAeroplaneMode(true)
  adb('reverse', '--remove-all')
  try {
    startApp()
    page = await attach(onServer, 60_000)
    await expect(page.getByTestId('offline-notice')).toBeVisible({ timeout: 30_000 })
    await page.goto(`${WEB_URL}/#/songs`)
    await page.getByRole('button', { name: 'Play Three', exact: true }).click()
    await expect(trackTitle(page)).toContainText('Three')
    await expect.poll(() => position(page), { timeout: 30_000 }).toBeGreaterThan(6)
    await detach()
    swipeAway()
    await expect.poll(() => mediaSession().state).not.toBe('PLAYING')

    startApp()
    // The app comes back in the same process: give the page of the start
    // before a moment to go, so it is the new one that is attached to.
    await new Promise((r) => setTimeout(r, 5000))
    page = await attach(onServer, 60_000)
    await expect(trackTitle(page)).toContainText('Three', { timeout: 30_000 })
    await expect.poll(() => shownPosition(page)).toBeGreaterThan(4)
    const restored = await shownPosition(page)
    await new Promise((r) => setTimeout(r, 3000))
    expect((await slots(page)).some((slot) => slot.playing)).toBe(false)
    // Play carries on from there, not from the start.
    await page.locator('.player-bar, .side-panel-player').getByRole('button', { name: 'Play', exact: true }).first().click()
    await expect.poll(async () => (await activeSlot(page))?.playing, { timeout: 30_000 }).toBe(true)
    await expect.poll(() => position(page)).toBeGreaterThan(restored + 1)
    note('reopened (offline)', { restoredAt: restored, slot: await activeSlot(page) })
  } finally {
    setAeroplaneMode(false)
    adb('reverse', `tcp:${PHONE_PORT}`, `tcp:${PHONE_PORT}`)
  }
})

test('10 a server that carries the app offers it to a browser, and a newer one to the installed app', async ({ browser }) => {
  // Nothing on offer so far: no button, no prompt, no download.
  expect((await server.api.get('/api/download/app/android')).status()).toBe(404)
  expect(((await (await server.api.get('/api/version')).json()) as { android_app: unknown }).android_app).toBeNull()

  // The server now carries an app newer than the installed one (0.x).
  mkdirSync(dirname(OFFERED_APK), { recursive: true })
  copyFileSync(APK, OFFERED_APK)
  writeFileSync(`${OFFERED_APK}.json`, JSON.stringify({ version: '99.0.0' }))
  const size = statSync(OFFERED_APK).size
  try {
    // A browser: Settings > About has the download, and it is the APK.
    const context = await browser.newContext({ storageState: STORAGE_STATE, baseURL: WEB_URL })
    const desktop = await context.newPage()
    await desktop.goto('/#/settings/about')
    const link = desktop.getByRole('link', { name: 'Get the Android app' })
    await expect(link).toBeVisible()
    await expect(link).toHaveAttribute('href', '/api/download/app/android')
    await expect(desktop.getByText(/Version 99\.0\.0, \d/)).toBeVisible()
    const sent = await server.api.get('/api/download/app/android')
    expect(sent.status()).toBe(200)
    expect(sent.headers()['content-type']).toBe('application/vnd.android.package-archive')
    expect((await sent.body()).length).toBe(size)
    await context.close()

    // The installed app: it says an update is there, fetches it and opens Android's installer.
    const page = await openOnline()
    const prompt = page.getByTestId('app-update-prompt')
    await expect(prompt).toBeVisible({ timeout: 30_000 })
    await expect(prompt).toContainText('99.0.0')
    const installed = (await native<{ appVersion: string }>(page, 'shellState')).appVersion
    await expect(prompt).toContainText(`you have ${installed})`)
    note('update prompt', await prompt.innerText())
    // No "Get the Android app" inside the app itself.
    await page.goto(`${WEB_URL}/#/settings/about`)
    await expect(page.getByTestId('app-version')).toContainText(`Android app ${installed}`)
    await expect(page.getByRole('link', { name: 'Get the Android app' })).toHaveCount(0)
    // The offered file is this same build: the app fetches it, finds its
    // version code is not higher than its own, and refuses it before
    // Android's installer is ever opened (only a newer build of this app,
    // signed with the same key, is handed to the installer).
    await prompt.getByRole('button', { name: 'Update' }).click()
    await expect(page.getByText("The server's app is not newer than this one")).toBeVisible({ timeout: 60_000 })
    expect(topActivity()).not.toMatch(/packageinstaller/i)
    note('update refused', { topActivity: topActivity() })
  } finally {
    rmSync(dirname(OFFERED_APK), { recursive: true, force: true })
  }
})
