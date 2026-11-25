// Synced playback, Now Playing, video taps, remote control and mobile tabs.

import { expect, test } from './fixtures'
import { answerDialog } from './dialogs'
import { pushPlaybackState } from './mockApi'
import { devicePickerToggle, openApp, openVideoNowPlaying, player, settle } from './app-helpers'

test.describe('Atriolyric browser flows', () => {
  test('controls synced playback, devices, queue, and Now Playing', async ({ page }) => {
    // Below the side panel's 1200px, so Now Playing (a page) carries Up next.
    await page.setViewportSize({ width: 1100, height: 800 })
    const mock = await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()

    const bar = player(page).filter({ hasText: 'Bohemian Rhapsody' })
    await expect(bar).toBeVisible()
    await bar.getByRole('button', { name: 'Pause' }).click()
    await expect(bar.getByRole('button', { name: 'Play', exact: true })).toBeVisible()
    await bar.getByRole('button', { name: 'Play', exact: true }).click()
    await expect(bar.getByRole('button', { name: 'Pause', exact: true })).toBeVisible()
    await bar.getByRole('button', { name: 'Next' }).click()
    await expect(player(page)).toContainText("Don't Stop Me Now")
    await player(page).getByRole('button', { name: 'Previous' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')

    const seekTrack = bar.locator('.player-progress-track')
    const seekBounds = await seekTrack.boundingBox()
    expect(seekBounds).not.toBeNull()
    if (!seekBounds) return
    await page.mouse.click(
      seekBounds.x + seekBounds.width * (42 / 355),
      seekBounds.y + seekBounds.height / 2,
    )
    // Within a pixel's worth of time: the side panel's seek bar is short.
    await expect.poll(
      () => Math.abs(Number(mock.wsMessages.filter((message) => message.type === 'seek').at(-1)?.position_seconds ?? 0) - 42),
    ).toBeLessThan(355 / seekBounds.width + 0.5)

    const activeMedia = page.locator('audio[src*="/api/stream/1"]')
    await activeMedia.evaluate((element) => {
      const media = element as HTMLMediaElement
      media.currentTime = 25
    })
    mock.playback.position_seconds = 80
    pushPlaybackState(mock, { position_changed: true })
    // Another device's scrub on this (active) device is applied...
    await expect
      .poll(() => activeMedia.evaluate((element) => (element as HTMLMediaElement).currentTime))
      .toBe(80)
    // ...but the echo of a position this device sent itself is not.
    await activeMedia.evaluate((element) => {
      ;(element as HTMLMediaElement).currentTime = 25
    })
    mock.playback.position_seconds = Number(
      mock.wsMessages.filter((message) => message.type === 'seek' || message.type === 'position_report')
        .at(-1)?.position_seconds,
    )
    pushPlaybackState(mock, { position_changed: true })
    await settle(page)
    expect(await activeMedia.evaluate((element) => (element as HTMLMediaElement).currentTime)).toBe(25)
    // A slow link delivers an older position report after newer ones were
    // sent: tagged as this device's own report, it must not pull playback back.
    await activeMedia.evaluate((element) => {
      ;(element as HTMLMediaElement).currentTime = 60
    })
    mock.playback.position_seconds = 7
    pushPlaybackState(mock, { position_changed: true, source_device_id: mock.playback.active_device_id, source_command: 'position_report' })
    await settle(page)
    expect(await activeMedia.evaluate((element) => (element as HTMLMediaElement).currentTime)).toBe(60)

    await devicePickerToggle(page).click()
    const devicePanel = page.locator('.device-picker-panel')
    await devicePanel
      .locator('.device-picker-row')
      .filter({ hasText: 'Living room' })
      .getByRole('menuitemradio')
      .click()
    await expect(devicePickerToggle(page)).toHaveAccessibleName(/Living room/)

    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    const nowPlaying = page.locator(':is(.nowplaying-overlay, .nowplaying-page)')
    await expect(nowPlaying).toContainText('Bohemian Rhapsody')
    await nowPlaying.getByRole('button', { name: 'Shuffle off' }).click()
    await expect(nowPlaying.getByRole('button', { name: 'Shuffle on' })).toBeVisible()
    await nowPlaying.getByRole('button', { name: 'Repeat off' }).click()
    await expect(nowPlaying.getByRole('button', { name: 'Repeat all' })).toBeVisible()
    await nowPlaying.getByRole('button', { name: /^Not liked: .*Press to like$/ }).click()
    await expect(nowPlaying.getByRole('button', { name: /^Liked: .*Press to dislike$/ })).toBeVisible()
    await nowPlaying.getByRole('button', { name: /^Liked: .*Press to dislike$/ }).click()
    await expect(nowPlaying.getByRole('button', { name: /^Disliked: .*Press to clear the dislike$/ })).toBeVisible()
    await expect(page.locator('.toast').filter({ hasText: 'Disliked – won’t appear in mixes' })).toBeVisible()
    await expect(nowPlaying).toContainText('Bohemian Rhapsody') // not skipped
    await nowPlaying.getByRole('button', { name: /^Disliked: .*Press to clear the dislike$/ }).click()
    await expect(nowPlaying.getByRole('button', { name: /^Not liked: .*Press to like$/ })).toBeVisible()

    // Up next is always in view beside the controls on a desktop.
    await expect(nowPlaying.getByRole('heading', { name: 'Up next' })).toBeVisible()
    const queueRows = nowPlaying.locator('.queue-row')
    await expect(queueRows).toHaveCount(3)
    // Reordering is by the drag handle only: no arrow buttons.
    await expect(nowPlaying.getByRole('button', { name: /^Move .* (up|down)$/ })).toHaveCount(0)
    // The current row is highlighted and pinned: no drag handle on it.
    await expect(nowPlaying.locator('.queue-row-current')).toHaveCount(1)
    await expect(nowPlaying.locator('.queue-row-current [aria-label^="Reorder "]')).toHaveCount(0)
    await expect(nowPlaying.locator('.queue-row:not(.queue-row-current) [aria-label^="Reorder "]')).toHaveCount(2)
    await expect(nowPlaying.getByRole('button', { name: /previous$/ })).toHaveCount(0)
    await queueRows.first().getByRole('button', { name: /^Remove .* from queue$/ }).click()
    await answerDialog(page, 'accept')
    await expect(nowPlaying.locator('.queue-row')).toHaveCount(2)
    await nowPlaying.getByRole('button', { name: 'Back', exact: true }).click()
  })

  // The gestures depend on taps a fraction of a second apart and on timers
  // from 0.15 s to 10 s. The clock is paused and moved with runFor, so the
  // timing is exact however busy the machine is and nothing really waits.
  test('video taps: double-tap seeks and toggles, ghost taps and stray taps are ignored', async ({ page }) => {
    await page.clock.install()
    await openApp(page)
    const videoOverlay = await openVideoNowPlaying(page)
    const picture = videoOverlay.locator('.nowplaying-video-placeholder')
    const box = (await picture.boundingBox())!
    const flash = videoOverlay.getByTestId('video-flash')
    await page.clock.pauseAt(new Date(Date.now() + 5_000))

    // Double taps flash the action over the picture: left/right thirds seek,
    // the middle toggles play/pause. Two distinct taps ~120 ms apart
    // (anything under 80 ms is treated as a ghost tap).
    const doubleTap = async (xFraction: number) => {
      const position = { x: box.width * xFraction, y: 80 }
      await page.clock.runFor(150)
      await picture.click({ position })
      await page.clock.runFor(120)
      await picture.click({ position })
    }
    await doubleTap(0.1)
    await expect(flash).toHaveClass(/video-flash-back/)
    await doubleTap(0.9)
    await expect(flash).toHaveClass(/video-flash-forward/)
    await doubleTap(0.5)
    await expect(flash).toHaveClass(/video-flash-(play|pause)/)
    await page.clock.runFor(700)
    await expect(flash).toHaveCount(0)

    // Ghost tap: a second click arriving <80 ms after the first is ignored, so
    // it must not turn into a double-tap action.
    await picture.click({ position: { x: box.width * 0.9, y: 80 } })
    await picture.click({ position: { x: box.width * 0.9, y: 80 } })
    await page.clock.runFor(500)
    await expect(flash).toHaveCount(0)

    // Hide the controls with a tap, then run a series of double taps plus one
    // stray extra tap: the controls must stay hidden throughout, and only
    // come back with a lone tap after the grace period. The pointer is moved
    // once and then only pressed - a desktop mouse move would (by design)
    // bring the controls back.
    const hiddenOverlay = page.locator('.nowplaying-overlay.video-mode.controls-hidden')
    const tapX = box.x + box.width * 0.9
    const tapY = box.y + 80
    const press = async (count: number) => {
      for (let i = 1; i <= count; i++) {
        await page.mouse.down({ clickCount: i })
        await page.mouse.up({ clickCount: i })
        if (i < count) await page.clock.runFor(120)
      }
    }
    await page.clock.runFor(1000)
    await page.mouse.move(tapX, tapY)
    // (A click straight after the move that revealed them keeps them shown;
    // wait that out, then a real tap hides them.)
    await page.clock.runFor(1100)
    // A lone tap is only acted on once the double-tap window has run out.
    await press(1)
    await page.clock.runFor(300)
    await expect(hiddenOverlay).toHaveCount(1)
    await press(2)
    await page.clock.runFor(400)
    await press(2)
    await page.clock.runFor(150)
    await press(1)
    await page.clock.runFor(600)
    await expect(hiddenOverlay).toHaveCount(1)
    await page.clock.runFor(1200)
    await press(1)
    await page.clock.runFor(300)
    await expect(hiddenOverlay).toHaveCount(0)
  })

  test('video controls stay shown after a hide, wait and tap (no flicker)', async ({ page }) => {
    // Paused clock: every wait below is exact, and the 16 s of waiting is virtual.
    await page.clock.install()
    await openApp(page)
    const overlay = await openVideoNowPlaying(page)
    const hidden = page.locator('.nowplaying-overlay.video-mode.controls-hidden')
    const picture = overlay.locator('.nowplaying-video-placeholder')
    const box = (await picture.boundingBox())!
    const spot = { x: box.x + box.width * 0.8, y: box.y + 80 }
    await page.clock.pauseAt(new Date(Date.now() + 5_000))
    // The auto-hide interval is 10 s, so anything vanishing within ~2 s of a
    // tap is the bug, not the timer.
    for (let cycle = 0; cycle < 4; cycle++) {
      // Hide: the mouse is moved onto the picture first, as a real user does.
      await page.mouse.move(spot.x + cycle * 10, spot.y)
      await page.mouse.down()
      await page.mouse.up()
      await page.clock.runFor(300) // the double-tap window, after which a lone tap acts
      await expect(hidden).toHaveCount(1)
      await page.clock.runFor(1000)
      // Show: moving there reveals them, and the click must not hide them again.
      await page.mouse.move(spot.x + cycle * 10 - 40, spot.y + 20)
      await page.mouse.down()
      await page.mouse.up()
      for (let i = 0; i < 8; i++) {
        await page.clock.runFor(250)
        await expect(hidden).toHaveCount(0)
      }
      await page.clock.runFor(1000)
    }
  })

  test('an inactive device acts as a remote control for the playing device', async ({ page }) => {
    // Below the side panel's 1200px, so Now Playing (a page) carries Up next.
    await page.setViewportSize({ width: 1100, height: 800 })
    const mock = await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    const bar = player(page).filter({ hasText: 'Bohemian Rhapsody' })
    await expect(bar).toBeVisible()

    // Another device takes over and is playing, 100 s in.
    mock.playback.devices.push({
      id: 3,
      name: 'Phone',
      device_type: 'mobile',
      is_active: true,
      is_connected: true,
      last_seen_at: new Date().toISOString(),
    })
    mock.playback.active_device_id = 3
    for (const device of mock.playback.devices) device.is_active = device.id === 3
    mock.playback.position_seconds = 100
    mock.playback.is_playing = true
    pushPlaybackState(mock)
    const slider = bar.getByRole('slider', { name: 'Seek' })
    await expect(devicePickerToggle(page)).toHaveAccessibleName(/Phone/)
    await expect.poll(async () => Number(await slider.getAttribute('aria-valuenow'))).toBeGreaterThanOrEqual(100)

    const allPaused = () =>
      page.evaluate(() => [...document.querySelectorAll('audio, video')].every((el) => (el as HTMLMediaElement).paused))
    expect(await allPaused()).toBe(true)

    // The displayed position keeps advancing between the other device's reports.
    const before = Number(await slider.getAttribute('aria-valuenow'))
    await expect.poll(async () => Number(await slider.getAttribute('aria-valuenow')), { timeout: 5000 })
      .toBeGreaterThan(before)

    // Scrubbing sends a seek and does not take over or play locally.
    const track = bar.locator('.player-progress-track')
    const bounds = (await track.boundingBox())!
    mock.wsMessages.length = 0
    await page.mouse.click(bounds.x + bounds.width * 0.5, bounds.y + bounds.height / 2)
    await expect.poll(() => mock.wsMessages.find((m) => m.type === 'seek')?.position_seconds ?? 0)
      .toBeCloseTo(355 / 2, -1)
    await expect.poll(async () => Number(await slider.getAttribute('aria-valuenow'))).toBeGreaterThan(170)
    expect(mock.playback.active_device_id).toBe(3)
    expect(mock.wsMessages.some((m) => m.type === 'transfer_to')).toBe(false)
    expect(await allPaused()).toBe(true)

    // The other device's report moves the shown position.
    mock.playback.position_seconds = 300
    pushPlaybackState(mock, { position_changed: true })
    await expect.poll(async () => Number(await slider.getAttribute('aria-valuenow'))).toBeGreaterThanOrEqual(300)

    // Next / Previous are commands, not local playback.
    mock.wsMessages.length = 0
    await bar.getByRole('button', { name: 'Next' }).click()
    await expect.poll(() => mock.wsMessages.some((m) => m.type === 'next')).toBe(true)
    await player(page).getByRole('button', { name: 'Previous' }).click()
    await expect.poll(() => mock.wsMessages.some((m) => m.type === 'prev')).toBe(true)

    // A queue entry jumps the playing device there.
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    const nowPlaying = page.locator(':is(.nowplaying-overlay, .nowplaying-page)')
    mock.wsMessages.length = 0
    await nowPlaying.locator('.queue-row').nth(2).locator('.queue-row-info').click()
    await expect.poll(() => mock.wsMessages.find((m) => m.type === 'jump_to_index')?.index).toBe(2)
    expect(mock.wsMessages.some((m) => m.type === 'transfer_to')).toBe(false)
    expect(mock.playback.active_device_id).toBe(3)
    expect(await allPaused()).toBe(true)
  })

  test('radio "+ Want list": one request however often it is pressed, then a message either way', async ({ page }) => {
    const mock = await openApp(page)
    // Each request waits for its release; `refuse` fails it.
    let release = () => {}
    let refuse = false
    let arrived = 0
    await page.route(/\/api\/radio\/want-list$/, async (route) => {
      await new Promise<void>((resolve) => {
        release = resolve
        arrived += 1
      })
      if (refuse) await route.fulfill({ status: 500, json: { detail: 'Something broke on the server.' } })
      else await route.fallback()
    })
    await page.locator('.home-view .rail-card').filter({ hasText: 'Radio Paradise' }).click()
    await expect(player(page)).toContainText('Miles Davis - So What')
    await page.getByRole('button', { name: 'Open now playing' }).click()
    const nowPlaying = page.locator(':is(.nowplaying-overlay, .nowplaying-page)')
    const want = nowPlaying.getByRole('button', { name: '+ Want list' })

    await want.click()
    await expect(want).toBeDisabled()
    // A second tap while the first is out sends nothing.
    await want.click({ force: true })
    await expect.poll(() => arrived).toBe(1)
    release()
    await expect(page.locator('.toast-success')).toContainText('Added "So What" by Miles Davis to your Want list.')
    await expect(want).toBeEnabled()
    expect(arrived).toBe(1)
    expect(mock.calls.filter((call) => call.path === '/api/radio/want-list' && call.method === 'POST')).toHaveLength(1)

    refuse = true
    await want.click()
    await expect.poll(() => arrived).toBe(2)
    release()
    await expect(page.locator('.toast-error')).toContainText('Something broke on the server.')
    await expect(want).toBeEnabled()
  })

  test('supports the mobile bottom tab bar (no dropdown menu) and browse search', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await openApp(page)

    await expect(page.locator('nav.tabs')).toBeHidden()
    const shortcuts = page.getByRole('navigation', { name: 'Quick tab navigation' })
    // Home, the first three tabs and More (the rest are under More).
    await expect(shortcuts.getByRole('button')).toHaveCount(5)
    await expect(shortcuts.getByRole('button', { name: 'Home' })).toHaveAttribute('aria-current', 'page')
    for (const button of await shortcuts.getByRole('button').all()) {
      expect((await button.boundingBox())!.width).toBeGreaterThanOrEqual(43.5)
    }
    await shortcuts.getByRole('button', { name: 'Songs' }).click()
    await expect(page).toHaveURL(/#\/songs$/)

    await expect(page.getByRole('button', { name: 'Open tab menu' })).toHaveCount(0)
    await shortcuts.getByRole('button', { name: 'Albums' }).click()
    await expect(page.getByRole('button', { name: 'Sort' })).toBeVisible()
    // The genre filter sits in the page toolbar; there is no search box on a phone either.
    await expect(page.getByRole('button', { name: 'Search and filter' })).toHaveCount(0)
    await expect(page.getByRole('textbox')).toHaveCount(0)
    await page.getByRole('button', { name: /^Genre/ }).click()
    await page.getByRole('menuitemradio', { name: 'Rock', exact: true }).click()
    const album = page.locator('.browse-card-album').filter({ hasText: 'A Night at the Opera' })
    await expect(album).toBeVisible()
  })
})
