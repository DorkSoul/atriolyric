// Local playback on the active device: resume, transfer, preloading, hand-offs and playing while hidden.

import { expect, test } from './fixtures'
import type { Route } from 'playwright/test'
import { type AppMockState, installApiMock, playbackSnapshot, pushPlaybackState, releaseHeldPlaybackMessages } from './mockApi'
import { makeTrack } from './mock/state'
import { devicePickerToggle, dropPlaybackSocket, installMediaSessionStub, markActive, openApp, player, readMediaSession, settle, silentWav } from './app-helpers'

test.describe('Atriolyric browser flows', () => {
  test('requests playback resume for a reloaded tab and saves its position before hiding', async ({ page, browserName }) => {
    await page.addInitScript(() => window.sessionStorage.setItem('atriolyric.playback-resume', '1'))
    const mock = await openApp(page)

    const registration = mock.wsMessages.find((message) => message.type === 'register_device')
    expect(registration?.resume_playback).toBe(true)

    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    const row = page.locator('.track-row').filter({ hasText: 'Bohemian Rhapsody' })
    await row.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')

    // WebKit does not allow the mocked media prototype to report an active
    // element, so its synthetic pagehide cannot exercise the position send.
    if (browserName !== 'webkit') {
      await page.evaluate(() => window.dispatchEvent(new Event('pagehide')))
      // The position save is a `position_report` naming the playing queue item.
      expect(mock.wsMessages.some((message) => message.type === 'position_report' &&
        message.queue_item_id === playbackSnapshot(mock).current_queue_item_id)).toBe(true)
      expect(await page.evaluate(() => window.sessionStorage.getItem('atriolyric.playback-resume'))).toBe('1')
      // Firefox sometimes never fires `load` here while the mock's empty audio
      // stream is still pending; the app's second registration is the signal
      // that matters, so only wait for the navigation to commit.
      await page.reload({ waitUntil: 'commit' })
      await expect.poll(
        () => mock.wsMessages.filter((message) => message.type === 'register_device').length,
      ).toBeGreaterThan(1)
      const registrations = mock.wsMessages.filter((message) => message.type === 'register_device')
      expect(registrations.at(-1)?.resume_playback).toBe(true)
    } else {
      // WebKit's mocked page reload does not establish the second socket in
      // CI. The initial handshake above is the portable assertion for this
      // browser; WebKit can also clear the seeded sessionStorage during its
      // synthetic navigation setup.
    }
  })

  test('retries a track end after the playback socket reconnects', async ({ page }) => {
    const mock = await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')

    // The close handler must have run before the media element reaches the end.
    await dropPlaybackSocket(page, mock)
    await page.locator('audio[src*="/api/stream/1"]').evaluate((element) => {
      element.dispatchEvent(new Event('ended'))
    })

    await expect(player(page)).toContainText("Don't Stop Me Now")
    await expect
      .poll(() => mock.wsMessages.filter((message) => message.type === 'register_device').length)
      .toBeGreaterThan(1)
    await expect
      .poll(() => mock.wsMessages.filter((message) => message.type === 'track_ended').length)
      .toBe(1)
    await expect(player(page)).toContainText("Don't Stop Me Now")
  })

  test('does not pause the queue when a transferred device blocks autoplay', async ({ page }) => {
    const mock = await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')

    // Simulate the browser rejecting automatic play on the newly active
    // device. The server must remain playing so the queue can advance.
    await page.evaluate(() => {
      const mediaPrototype = HTMLMediaElement.prototype as HTMLMediaElement & {
        __playwrightPaused?: boolean
      }
      mediaPrototype.play = function play() {
        this.__playwrightPaused = true
        return Promise.reject(new Error('autoplay blocked'))
      }
    })
    const pauseCount = mock.wsMessages.filter((message) => message.type === 'pause').length
    await page.locator('audio[src*="/api/stream/1"]').evaluate((element) => {
      element.dispatchEvent(new Event('ended'))
    })
    await expect(player(page)).toContainText("Don't Stop Me Now")
    expect(mock.playback.is_playing).toBe(true)
    expect(mock.wsMessages.filter((message) => message.type === 'pause')).toHaveLength(pauseCount)
  })

  test('restores local playback on device transfer and keeps advancing naturally', async ({ page }) => {
    const mock = await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')

    // The old device remains registered and active, but is no longer
    // producing audio. Transfer output back to this page, then simulate two
    // consecutive natural ends to ensure each transition keeps playing.
    mock.playback.devices.push({
      id: 3,
      name: 'Old device',
      device_type: 'web',
      is_active: true,
      is_connected: true,
      last_seen_at: new Date().toISOString(),
    })
    mock.playback.active_device_id = 3
    for (const device of mock.playback.devices) device.is_active = device.id === 3
    pushPlaybackState(mock)
    const firstTrackAudio = page.locator('audio[src*="/api/stream/1"]').first()
    await expect.poll(() => firstTrackAudio.evaluate((element) => (element as HTMLAudioElement).paused)).toBe(true)

    await page.evaluate(() => {
      const prototype = HTMLMediaElement.prototype as HTMLMediaElement & { __playCalls?: number }
      const originalPlay = prototype.play
      prototype.__playCalls = 0
      prototype.play = function play() {
        prototype.__playCalls = (prototype.__playCalls ?? 0) + 1
        return originalPlay.call(this)
      }
    })
    const playCallsBeforeTransfer = await page.evaluate(() =>
      (HTMLMediaElement.prototype as HTMLMediaElement & { __playCalls?: number }).__playCalls ?? 0,
    )

    await devicePickerToggle(page).click()
    const devicePanel = page.locator('.device-picker-panel')
    await devicePanel
      .locator('.device-picker-row')
      .filter({ hasText: '(this device)' })
      .getByRole('menuitemradio')
      .click()
    await expect.poll(() => mock.playback.active_device_id).toBe(1)
    await expect.poll(() => page.evaluate(() =>
      (HTMLMediaElement.prototype as HTMLMediaElement & { __playCalls?: number }).__playCalls ?? 0,
    )).toBeGreaterThan(playCallsBeforeTransfer)
    await expect.poll(() => firstTrackAudio.evaluate((element) => (element as HTMLAudioElement).paused)).toBe(false)

    await firstTrackAudio.evaluate((element) => element.dispatchEvent(new Event('ended')))
    await expect(player(page)).toContainText("Don't Stop Me Now")
    await expect.poll(() => mock.wsMessages.filter((message) => message.type === 'track_ended').length).toBe(1)

    const secondTrackAudio = page.locator('audio[src*="/api/stream/2"]').first()
    await expect(secondTrackAudio).toHaveCount(1)
    await expect.poll(() => secondTrackAudio.evaluate((element) => (element as HTMLAudioElement).paused)).toBe(false)
    await secondTrackAudio.evaluate((element) => {
      element.dispatchEvent(new Event('ended'))
    })
    await expect(player(page)).toContainText('Midnight City')
    await expect.poll(() => mock.wsMessages.filter((message) => message.type === 'track_ended').length).toBe(2)
  })

  test('preloads the next queued track', async ({ page }) => {
    const mock = await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')

    await expect(page.locator('audio[src*="/api/stream/2"]')).toHaveCount(1)
    await player(page).getByRole('button', { name: 'Next' }).click()
    await expect(player(page)).toContainText("Don't Stop Me Now")
    await expect(page.locator('audio[src*="/api/stream/3"]')).toHaveCount(1)
    expect(mock.wsMessages.some((message) => message.type === 'next')).toBe(true)
  })

  // A skip onto a track that is not in memory yet must not start the
  // whole-file download of the track after it in the same instant as the
  // stream, or the song takes many seconds to start.
  test('a skip onto a track that is not preloaded starts it before downloading the one after', async ({ page }) => {
    await openApp(page)
    const requested: number[] = []
    // Track 2 never arrives: neither its preload nor, after the skip, its stream.
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) => {
      const id = Number(/\/api\/stream\/(\d+)/.exec(route.request().url())![1])
      requested.push(id)
      if (id === 2) return
      return route.fallback()
    })
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    await expect.poll(() => requested.includes(2)).toBe(true)

    // From here on a load only starts: the stubs stop pretending it succeeded.
    await page.evaluate(() => {
      ;(window as Window & { __mockLoadEvents?: boolean }).__mockLoadEvents = false
    })
    await player(page).getByRole('button', { name: 'Next' }).click()
    await expect(player(page)).toContainText("Don't Stop Me Now")
    const active = page.locator('audio[src*="/api/stream/2"]')
    await expect(active).toHaveCount(1)
    // While it has not started, the track after it is left alone.
    await page.waitForTimeout(1_000)
    expect(requested).not.toContain(3)

    await active.evaluate((element) => element.dispatchEvent(new Event('canplay')))
    await expect.poll(() => requested.includes(3)).toBe(true)
  })

  // `loadstart` holds the next-track prefetch back so the requested track gets
  // the connection first; a stream that then stalls (the browser stops
  // fetching) must not keep holding it.
  test('preloads the next track when the active element stalls after loadstart', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    await expect(page.locator('audio[src*="/api/stream/2"]')).toHaveCount(1)

    // From here on no element reaches canplay or fails: it only starts loading.
    // The clock is paused as well, so the hold's own 4 s cap cannot release
    // the prefetch: only the stall can.
    await page.evaluate(() => {
      ;(window as Window & { __mockLoadEvents?: boolean }).__mockLoadEvents = false
    })
    await page.clock.pauseAt(new Date(Date.now() + 5_000))
    const isStarting = () =>
      page.evaluate(async () => {
        // The app's own instance of the module (the same URL it was loaded from).
        const url = '/src/playbackPriority.ts'
        return (await import(/* @vite-ignore */ url)).isPlaybackStarting() as boolean
      })
    const fireOnPlayingElement = (type: string) => page.evaluate((eventType) => {
      Array.from(document.querySelectorAll('audio')).find((audio) => !audio.paused)
        ?.dispatchEvent(new Event(eventType))
    }, type)

    // The active element starts loading: the hold is on. The user skips ahead
    // while it is, so the prefetch of the track after that waits.
    await fireOnPlayingElement('loadstart')
    await expect.poll(isStarting).toBe(true)
    await player(page).getByRole('button', { name: 'Next' }).click()
    await expect(player(page)).toContainText("Don't Stop Me Now")
    expect(await isStarting()).toBe(true)
    await expect(page.locator('audio[src*="/api/stream/3"]')).toHaveCount(0)

    // The stream stalls after loadstart, never reaching canplay.
    await fireOnPlayingElement('loadstart')
    await fireOnPlayingElement('stalled')
    await expect(page.locator('audio[src*="/api/stream/3"]')).toHaveCount(1, { timeout: 5_000 })
    expect(await isStarting()).toBe(false)
    expect(mock.wsMessages.some((message) => message.type === 'next')).toBe(true)
  })

  test('keeps the finished track cached so going back does not re-download it', async ({ page }) => {
    await openApp(page)
    const streamRequests: number[] = []
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) => {
      streamRequests.push(Number(/\/api\/stream\/(\d+)/.exec(route.request().url())![1]))
      return route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(30) })
    })
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    await expect(page.locator('audio[data-stream-src*="/api/stream/2"]')).toHaveAttribute('src', /^blob:/)

    await player(page).getByRole('button', { name: 'Next' }).click()
    await expect(player(page)).toContainText("Don't Stop Me Now")
    await expect(page.locator('audio[data-stream-src*="/api/stream/3"]')).toHaveAttribute('src', /^blob:/)
    await player(page).getByRole('button', { name: 'Next' }).click()
    await expect(player(page)).not.toContainText("Don't Stop Me Now")
    const requestsBefore = streamRequests.filter((id) => id === 2).length

    await player(page).getByRole('button', { name: 'Previous' }).click()
    await expect(player(page)).toContainText("Don't Stop Me Now")
    await expect(page.locator('audio[data-stream-src*="/api/stream/2"]').first()).toHaveAttribute('src', /^blob:/)
    expect(streamRequests.filter((id) => id === 2).length).toBe(requestsBefore)
  })

  test('tells the server when the OS pauses or resumes the active element', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(30) }),
    )
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    const active = page.locator('audio[data-stream-src*="/api/stream/1"]')
    await expect.poll(() => active.evaluate((el) => (el as HTMLAudioElement).readyState)).toBeGreaterThan(0)
    // Let the app's own source-swap window pass, then pause behind its back
    // (headphones unplugged, another app taking audio focus).
    await page.clock.fastForward(1_300)
    const pausesBefore = mock.wsMessages.filter((message) => message.type === 'pause').length
    await active.evaluate((el) => {
      // Headless Chromium has no audio sink, so emulate what the OS does: the
      // element ends up paused and fires `pause` without the app asking.
      Object.defineProperty(el, 'paused', { configurable: true, get: () => true })
      el.dispatchEvent(new Event('pause'))
    })
    await expect
      .poll(() => mock.wsMessages.filter((message) => message.type === 'pause').length)
      .toBe(pausesBefore + 1)
    await expect(player(page).getByRole('button', { name: 'Play', exact: true })).toBeVisible()

    await page.clock.fastForward(1_300)
    await active.evaluate((el) => {
      Object.defineProperty(el, 'paused', { configurable: true, get: () => false })
      el.dispatchEvent(new Event('play'))
    })
    await expect
      .poll(() => mock.wsMessages.filter((message) => message.type === 'play').length)
      .toBeGreaterThan(0)
  })

  test('does not report the app\'s own track hand-off as an external pause', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(30) }),
    )
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    await player(page).getByRole('button', { name: 'Next' }).click()
    await expect(player(page)).toContainText("Don't Stop Me Now")
    // An external pause is reported after a 250 ms debounce: run that out.
    await page.clock.fastForward(1_000)
    await settle(page)
    expect(mock.wsMessages.filter((message) => message.type === 'pause')).toHaveLength(0)
  })

  // Returning to the foreground deliberately replaces the playback socket,
  // which momentarily reads as a drop. That must not pause the audio.
  test('does not pause playing audio when the app returns to the foreground', async ({ page }) => {
    const mock = await openApp(page)
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav() }))
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    await expect.poll(() => page.locator('[data-player-element="main"]').evaluate((el) => (el as HTMLMediaElement).paused)).toBe(false)

    const registrationsBefore = mock.wsMessages.filter((m) => m.type === 'register_device').length
    await page.evaluate(() => {
      const w = window as unknown as { __pauses: number }
      w.__pauses = 0
      document.querySelectorAll('audio').forEach((el) => {
        const original = el.pause.bind(el)
        el.pause = () => { if (!el.paused) w.__pauses += 1; original() }
      })
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await expect.poll(() => mock.wsMessages.filter((m) => m.type === 'register_device').length)
      .toBeGreaterThan(registrationsBefore)
    expect(await page.evaluate(() => (window as unknown as { __pauses: number }).__pauses)).toBe(0)
    expect(await page.locator('[data-player-element="main"]').evaluate((el) => (el as HTMLMediaElement).paused)).toBe(false)
  })

  test('keeps the active audio element when advancing in the background', async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    })
    const mock = await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    await expect(page.locator('[data-player-element="standby"]')).toHaveAttribute('src', /\/api\/stream\/2/)

    await player(page).getByRole('button', { name: 'Next' }).click()
    await expect(player(page)).toContainText("Don't Stop Me Now")
    await expect(page.locator('[data-player-element="main"]')).toHaveAttribute('src', /\/api\/stream\/2/)
    await expect(page.locator('[data-player-element="standby"]')).toHaveAttribute('src', /\/api\/stream\/3/)
    expect(mock.wsMessages.some((message) => message.type === 'next')).toBe(true)
  })

  // On a visible page the preloaded standby element takes over inside the
  // `ended` handler itself, so no React render sits between the two tracks.
  test('hands off to the buffered standby element within the ended event', async ({ page }) => {
    await openApp(page)
    // The shared mock serves empty audio, which the browser rejects with a
    // media error that the player treats as a failed track and skips - a
    // race with the hand-off under test. Serve decodable files instead.
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav() }))
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    // The standby holds an in-memory copy (a blob: src), so identify the track
    // it holds by its stream URL rather than by `src`.
    await expect(page.locator('[data-player-element="standby"]')).toHaveAttribute('data-stream-src', /\/api\/stream\/2/)
    await expect(page.locator('[data-player-element="standby"]')).toHaveAttribute('src', /^blob:/)
    const handoff = await page.evaluate(() => {
      const [first, standby] = Array.from(document.querySelectorAll('audio')).slice(0, 2)
      Object.defineProperty(standby, 'readyState', { configurable: true, get: () => HTMLMediaElement.HAVE_ENOUGH_DATA })
      const standbySrc = (standby as HTMLElement).dataset.streamSrc ?? ''
      let srcWrites = 0
      const srcDescriptor = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src')!
      Object.defineProperty(standby, 'src', {
        configurable: true,
        get() { return srcDescriptor.get!.call(this) },
        set(value: string) { srcWrites += 1; srcDescriptor.set!.call(this, value) },
      })
      first.dispatchEvent(new Event('ended'))
      // Read synchronously: the swap must not wait for a render.
      const result = { standbyPaused: standby.paused, firstPaused: first.paused, standbySrc }
      ;(window as unknown as { __srcWrites: () => number }).__srcWrites = () => srcWrites
      return result
    })
    expect(handoff.standbyPaused).toBe(false)
    expect(handoff.firstPaused).toBe(true)
    expect(handoff.standbySrc).toMatch(/\/api\/stream\/2/)

    await expect(player(page)).toContainText("Don't Stop Me Now")
    // The promoted element kept its buffer: its src was never rewritten, and
    // the old element became the standby for the track after.
    await expect(page.locator('[data-player-element="standby"]')).toHaveAttribute('data-stream-src', /\/api\/stream\/2/)
    await expect(page.locator('[data-player-element="main"]')).toHaveAttribute('data-stream-src', /\/api\/stream\/3/)
    expect(await page.evaluate(() => (window as unknown as { __srcWrites: () => number }).__srcWrites())).toBe(0)
    expect(await page.locator('[data-player-element="standby"]').evaluate((element) => (element as HTMLMediaElement).paused)).toBe(false)
  })

  // At `ended` the page moves to the next track itself and sends track_ended.
  // Whatever the server broadcast before it processed that message still names
  // the track that ended; applying it would put that track back, reload it at
  // its tail, and the server's next-track snapshot would then restart the next
  // track.
  test('a late snapshot for the ended track does not undo the local advance', async ({ page }) => {
    const mock = await openApp(page)
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(30) }))
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    const bar = player(page)
    await expect(bar).toContainText('Bohemian Rhapsody')
    const standby = page.locator('[data-player-element="standby"]')
    await expect(standby).toHaveAttribute('data-stream-src', /\/api\/stream\/2/)
    await expect(standby).toHaveAttribute('src', /^blob:/)

    // The server has not processed the track end yet.
    mock.holdPlaybackTypes.add('track_ended')
    await page.evaluate(() => {
      const [first, next] = Array.from(document.querySelectorAll('audio')).slice(0, 2)
      Object.defineProperty(next, 'readyState', { configurable: true, get: () => HTMLMediaElement.HAVE_ENOUGH_DATA })
      const w = window as unknown as { __loads: number; __srcWrites: number }
      w.__loads = 0
      w.__srcWrites = 0
      const srcDescriptor = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src')!
      Object.defineProperty(next, 'src', {
        configurable: true,
        get() { return srcDescriptor.get!.call(this) },
        set(value: string) { w.__srcWrites += 1; srcDescriptor.set!.call(this, value) },
      })
      next.load = () => { w.__loads += 1 }
      first.dispatchEvent(new Event('ended'))
      next.currentTime = 3
    })
    await expect(bar).toContainText("Don't Stop Me Now")
    await expect.poll(() => mock.heldPlaybackMessages.filter((message) => message.type === 'track_ended').length).toBe(1)

    const expectStillOnNextTrack = async () => {
      await settle(page)
      await expect(bar).toContainText("Don't Stop Me Now")
      await expect(standby).toHaveAttribute('data-stream-src', /\/api\/stream\/2/)
      expect(await standby.evaluate((element) => ({
        paused: (element as HTMLMediaElement).paused,
        currentTime: (element as HTMLMediaElement).currentTime,
        loads: (window as unknown as { __loads: number }).__loads,
        srcWrites: (window as unknown as { __srcWrites: number }).__srcWrites,
      }))).toEqual({ paused: false, currentTime: 3, loads: 0, srcWrites: 0 })
    }

    // The echo of the position report sent just before the end...
    mock.playback.position_seconds = 350
    pushPlaybackState(mock, { position_changed: true, source_device_id: 1, source_command: 'position_report' })
    await expectStillOnNextTrack()
    // ...another device registering...
    mock.playback.devices.push({
      id: 3,
      name: 'Kitchen speaker',
      device_type: 'web',
      is_active: false,
      is_connected: true,
      last_seen_at: new Date().toISOString(),
    })
    pushPlaybackState(mock, { position_changed: false, source_device_id: 3, source_command: null })
    await expectStillOnNextTrack()
    // ...and a scrub another device made on the track that has now ended.
    mock.playback.position_seconds = 340
    pushPlaybackState(mock, { position_changed: true, source_device_id: 2, source_command: 'seek' })
    await expectStillOnNextTrack()
    // The device list from those snapshots was applied all the same.
    await devicePickerToggle(page).click()
    await expect(page.locator('.device-picker-panel')).toContainText('Kitchen speaker')
    await devicePickerToggle(page).click()

    // The server catches up and names the next track: everything agrees.
    releaseHeldPlaybackMessages(mock)
    await expect.poll(() => mock.playback.media_item_id).toBe(2)
    await expect(page.locator('[data-player-element="main"]')).toHaveAttribute('data-stream-src', /\/api\/stream\/3/)
    await expectStillOnNextTrack()

    // The track end is settled, so the ended track is not ignored from now on.
    await bar.getByRole('button', { name: 'Previous' }).click()
    await expect(bar).toContainText('Bohemian Rhapsody')
  })

  // After a gapless hand-off the old element becomes the standby. Until the
  // server answers track_ended its state still names the track that ended;
  // filled from that index, the standby would load the track now playing a
  // second time from the same in-memory copy (WebKit's page then stops
  // answering) instead of the track after it.
  test('while the server has not answered a track end, the standby takes the track after the new one', async ({ page }) => {
    const mock = await openApp(page)
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(1) }))
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    const bar = player(page)
    await expect(bar).toContainText('Bohemian Rhapsody')
    const main = page.locator('[data-player-element="main"]')
    const standby = page.locator('[data-player-element="standby"]')
    await expect(main).toHaveAttribute('data-stream-src', /\/api\/stream\/1(\?|$)/)
    await expect(standby).toHaveAttribute('data-stream-src', /\/api\/stream\/2(\?|$)/)

    mock.holdPlaybackTypes.add('track_ended')
    await standby.evaluate((element) => {
      Object.defineProperty(element, 'readyState', { configurable: true, get: () => HTMLMediaElement.HAVE_ENOUGH_DATA })
    })
    await main.evaluate((element) => element.dispatchEvent(new Event('ended')))
    await expect(bar).toContainText("Don't Stop Me Now")
    await expect.poll(() => mock.heldPlaybackMessages.filter((message) => message.type === 'track_ended').length).toBe(1)
    await settle(page)
    expect(mock.playback.media_item_id).toBe(1) // not answered yet
    await expect(main).toHaveAttribute('data-stream-src', /\/api\/stream\/3(\?|$)/)
    await expect(page.locator('audio[data-stream-src*="/api/stream/2"]')).toHaveCount(1)
    expect(await standby.evaluate((element) => (element as HTMLMediaElement).paused)).toBe(false)

    // The server catches up: nothing moves.
    releaseHeldPlaybackMessages(mock)
    await expect.poll(() => mock.playback.media_item_id).toBe(2)
    await settle(page)
    await expect(main).toHaveAttribute('data-stream-src', /\/api\/stream\/3(\?|$)/)
    await expect(standby).toHaveAttribute('data-stream-src', /\/api\/stream\/2(\?|$)/)
  })

  // Repeat-one and a one-track queue restart the very track that ended, so a
  // snapshot naming it is current, not stale: it must still apply.
  test('a track restarted at its end still follows later snapshots for it', async ({ page }) => {
    // Below the side panel's 1200px, so Now Playing (a page) carries Up next.
    await page.setViewportSize({ width: 1100, height: 800 })
    // The mock's empty stream never loads, so the element reports whatever
    // position it is given (a fulfilled WAV is not seekable without ranges).
    const mock = await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    const bar = player(page)
    await expect(bar).toContainText('Bohemian Rhapsody')
    const active = page.locator('[data-player-element="main"]')
    await expect(active).toHaveAttribute('data-stream-src', /\/api\/stream\/1/)
    mock.holdPlaybackTypes.add('track_ended')

    const restartAndFollowRemoteSeek = async (expectedEnds: number) => {
      await active.evaluate((element) => {
        ;(element as HTMLMediaElement).currentTime = 20
        element.dispatchEvent(new Event('ended'))
      })
      await expect.poll(() => mock.heldPlaybackMessages.length).toBe(expectedEnds)
      expect(await active.evaluate((element) => (element as HTMLMediaElement).currentTime)).toBe(0)
      // The server's answer to track_ended: the same track from 0...
      mock.playback.position_seconds = 0
      pushPlaybackState(mock, { position_changed: true, source_device_id: 1, source_command: 'track_ended' })
      await settle(page)
      expect(await active.evaluate((element) => (element as HTMLMediaElement).currentTime)).toBe(0)
      // ...and then another device scrubs it, which must move this element.
      mock.playback.position_seconds = 12
      pushPlaybackState(mock, { position_changed: true, source_device_id: 2, source_command: 'seek' })
      await expect.poll(() => active.evaluate((element) => (element as HTMLMediaElement).currentTime)).toBe(12)
      await expect(bar).toContainText('Bohemian Rhapsody')
      await expect(active).toHaveAttribute('data-stream-src', /\/api\/stream\/1/)
    }

    mock.playback.repeat_mode = 'one'
    pushPlaybackState(mock)
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    await expect(page.locator(':is(.nowplaying-overlay, .nowplaying-page)').getByRole('button', { name: 'Repeat one' })).toBeVisible()
    await restartAndFollowRemoteSeek(1)

    // A queue of one track on repeat-all restarts it the same way.
    mock.playback.repeat_mode = 'all'
    mock.playback.queue_media_item_ids = [1]
    mock.playback.queue_item_ids = [1001]
    mock.playback.queue_index = 0
    mock.playback.queue_context_count = 1
    pushPlaybackState(mock)
    await expect(page.locator(':is(.nowplaying-overlay, .nowplaying-page)').getByRole('button', { name: 'Repeat all' })).toBeVisible()
    await expect(page.locator('audio[data-stream-src*="/api/stream/2"]')).toHaveCount(0)
    await restartAndFollowRemoteSeek(2)
  })

  // When the standby does not hold the next track yet (a large file on mobile
  // data), the sync effect loads it itself. The snapshot is still the ended
  // track's then, and its saved position must not become the new track's start.
  test('a next track that was not preloaded starts at zero', async ({ page }) => {
    const mock = await openApp(page)
    // The download of track 2 never completes, so nothing preloads it.
    const heldStreams: Route[] = []
    await page.route(/\/api\/stream\/2(\?|$)/, (route) => { heldStreams.push(route) })
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    const bar = player(page)
    await expect(bar).toContainText('Bohemian Rhapsody')
    await expect.poll(() => heldStreams.length).toBe(1)
    const nextTrackElement = page.locator('audio[data-stream-src*="/api/stream/2"]')
    await expect(nextTrackElement).toHaveCount(0)

    // The last position this device saved for track 1, 200 s in.
    mock.holdPlaybackTypes.add('track_ended')
    mock.playback.position_seconds = 200
    pushPlaybackState(mock, { position_changed: true, source_device_id: 1, source_command: 'position_report' })
    await settle(page)
    await page.locator('audio[data-stream-src*="/api/stream/1"]').evaluate((element) => {
      element.dispatchEvent(new Event('ended'))
    })

    await expect(bar).toContainText("Don't Stop Me Now")
    await expect(nextTrackElement).toHaveCount(1)
    await settle(page)
    expect(await nextTrackElement.evaluate((element) => (element as HTMLMediaElement).currentTime)).toBe(0)
    await expect(bar.getByRole('slider', { name: 'Seek' })).toHaveAttribute('aria-valuetext', '0:00 of 3:30')
    for (const route of heldStreams) await route.fulfill({ status: 200, contentType: 'audio/wav', body: '' }).catch(() => {})
  })

  // A manual Next onto the preloaded standby promotes it without reloading, so
  // no load sets the new track's length: the app sets it, or the seek bar keeps
  // the previous one and a click on it (fraction x length) seeks to the wrong
  // place.
  test('the seek bar takes the next track\'s length after a preloaded skip', async ({ page }) => {
    const mock = await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    const bar = player(page)
    await expect(bar).toContainText('Bohemian Rhapsody')
    const seekBar = bar.getByRole('slider', { name: 'Seek' })
    await expect(seekBar).toHaveAttribute('aria-valuemax', '355')
    await expect(page.locator('[data-player-element="standby"]')).toHaveAttribute('data-stream-src', /\/api\/stream\/2/)

    await bar.getByRole('button', { name: 'Next' }).click()
    await expect(bar).toContainText("Don't Stop Me Now")
    // The standby was promoted, not reloaded.
    await expect(page.locator('[data-player-element="standby"]')).toHaveAttribute('data-stream-src', /\/api\/stream\/2/)
    await expect(seekBar).toHaveAttribute('aria-valuemax', '210')
    await expect(seekBar).toHaveAttribute('aria-valuetext', '0:00 of 3:30')

    const bounds = (await seekBar.boundingBox())!
    await page.mouse.click(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2)
    await expect.poll(
      () => mock.wsMessages.filter((message) => message.type === 'seek').at(-1)?.position_seconds ?? 0,
    ).toBeCloseTo(105, -1)

    // The element's own figure wins once it knows it (`durationchange`).
    await page.locator('[data-player-element="standby"]').evaluate((element) => {
      Object.defineProperty(element, 'duration', { configurable: true, get: () => 211.6 })
      element.dispatchEvent(new Event('durationchange'))
    })
    await expect(seekBar).toHaveAttribute('aria-valuemax', '212')
  })

  // remove_from_queue and transfer_to are broadcast with position_changed but
  // carry only the last position the active device saved. After a pause longer
  // than the window in which this device recognises its own saves (30 s), that
  // position must not pull the paused element back by up to five seconds.
  test('a queue edit or a repeated transfer does not move the active element', async ({ page }) => {
    // The mock's empty stream never loads, so the element reports whatever
    // position it is given.
    const mock = await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    const bar = player(page)
    await expect(bar).toContainText('Bohemian Rhapsody')
    await bar.getByRole('button', { name: 'Pause' }).click()
    await expect(bar.getByRole('button', { name: 'Play', exact: true })).toBeVisible()
    const active = page.locator('audio[data-stream-src*="/api/stream/1"]').first()
    await active.evaluate((element) => { (element as HTMLMediaElement).currentTime = 50 })
    const position = () => active.evaluate((element) => (element as HTMLMediaElement).currentTime)

    // The server's position is a save from longer ago than this device remembers.
    mock.playback.position_seconds = 46
    mock.playback.queue_media_item_ids = [1, 2]
    mock.playback.queue_item_ids = [1001, 1002]
    mock.playback.queue_context_count = 2
    pushPlaybackState(mock, { position_changed: true, source_device_id: 1, source_command: 'remove_from_queue' })
    await settle(page)
    expect(await position()).toBe(50)
    // Choosing this device again while it already is the active one.
    pushPlaybackState(mock, { position_changed: true, source_device_id: 1, source_command: 'transfer_to' })
    await settle(page)
    expect(await position()).toBe(50)

    // A transfer that makes this device the active one brings the previous
    // device's position, and that is applied.
    markActive(mock, 2)
    pushPlaybackState(mock, { position_changed: true, source_device_id: 1, source_command: 'transfer_to' })
    await expect(devicePickerToggle(page)).toHaveAccessibleName(/Living room/)
    markActive(mock, 1)
    mock.playback.position_seconds = 44
    pushPlaybackState(mock, { position_changed: true, source_device_id: 2, source_command: 'transfer_to' })
    await expect.poll(position).toBe(44)
    // So is removing the playing item when the next one is the same track.
    await active.evaluate((element) => { (element as HTMLMediaElement).currentTime = 30 })
    mock.playback.position_seconds = 0
    mock.playback.queue_media_item_ids = [1, 2]
    mock.playback.queue_item_ids = [1005, 1002]
    pushPlaybackState(mock, { position_changed: true, source_device_id: 1, source_command: 'remove_from_queue' })
    await expect.poll(position).toBe(0)
  })

  // A backgrounded page must move the audio element onto the next stream
  // inside the native `ended` handler itself: waiting for React to re-render
  // and commit an effect postpones the .play() by exactly the amount of time
  // a hidden tab gets throttled, which is what stalls the second advance on a
  // locked phone. Playwright's mocked media elements are not throttled, so
  // this asserts the code path (src and play() applied synchronously, before
  // `ended` returns) rather than real Chrome scheduling.
  test('advances twice from the ended event while hidden without a render hop', async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    })
    const mock = await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')

    const firstAdvance = await page.locator('audio[src*="/api/stream/1"]').evaluate((element) => {
      element.dispatchEvent(new Event('ended'))
      return {
        src: (element as HTMLAudioElement).src,
        paused: (element as HTMLAudioElement).paused,
      }
    })
    expect(firstAdvance.src).toMatch(/\/api\/stream\/2/)
    expect(firstAdvance.paused).toBe(false)

    await expect(player(page)).toContainText("Don't Stop Me Now")
    // The element the lock-screen notification is bound to stays active while
    // hidden, and the warmed standby is still queued behind it.
    await expect(page.locator('[data-player-element="main"]')).toHaveAttribute('src', /\/api\/stream\/2/)
    await expect(page.locator('[data-player-element="standby"]')).toHaveAttribute('src', /\/api\/stream\/3/)

    const secondAdvance = await page.locator('audio[src*="/api/stream/2"]').evaluate((element) => {
      element.dispatchEvent(new Event('ended'))
      return {
        src: (element as HTMLAudioElement).src,
        paused: (element as HTMLAudioElement).paused,
      }
    })
    expect(secondAdvance.src).toMatch(/\/api\/stream\/3/)
    expect(secondAdvance.paused).toBe(false)

    await expect(player(page)).toContainText('Midnight City')
    await expect(page.locator('[data-player-element="main"]')).toHaveAttribute('src', /\/api\/stream\/3/)
    // Both advances still reconcile with the server when the socket is up.
    await expect
      .poll(() => mock.wsMessages.filter((message) => message.type === 'track_ended').length)
      .toBe(2)
  })

  // The next track is downloaded ahead of time, so a hidden page moves onto it
  // from memory. A fresh stream request made at that moment is the one a
  // locked phone in Doze may never complete, which leaves the element silent
  // at readyState 0 and costs the page its media notification.
  // The silent keepalive is for mobile browsers only: a desktop gets no extra
  // player that could take over its Now Playing widget.
  test('adds no keepalive player on a desktop browser', async ({ page }) => {
    await openApp(page)
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav() }))
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    await expect(page.locator('audio[data-keepalive]')).toHaveCount(0)
  })

  test('advances while hidden from the in-memory copy without a new stream request', async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    })
    await openApp(page)
    const streamRequests: number[] = []
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) => {
      streamRequests.push(Number(/\/api\/stream\/(\d+)/.exec(route.request().url())![1]))
      return route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav() })
    })
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    const standby = page.locator('audio[data-stream-src*="/api/stream/2"]')
    await expect(standby).toHaveAttribute('src', /^blob:/)
    // One request for track 2 so far: the prefetch. The element used its copy.
    expect(streamRequests.filter((id) => id === 2)).toHaveLength(1)

    const advance = await page.locator('audio[data-stream-src*="/api/stream/1"]').evaluate((element) => {
      element.dispatchEvent(new Event('ended'))
      const audio = element as HTMLAudioElement
      return { src: audio.src, stream: audio.dataset.streamSrc ?? '', paused: audio.paused }
    })
    expect(advance.src).toMatch(/^blob:/)
    expect(advance.stream).toMatch(/\/api\/stream\/2/)
    expect(advance.paused).toBe(false)

    await expect(player(page)).toContainText("Don't Stop Me Now")
    // Advancing did not go back to the network for the track it moved onto.
    expect(streamRequests.filter((id) => id === 2)).toHaveLength(1)
  })

  // A locked phone's playback socket is suspended routinely (doze). That is a
  // lost control plane, not a transport change: the playback-sync effect's
  // teardown must not pause the elements when `connected` flips false, which
  // would abort the sync hidden advance's in-flight play() ("play() interrupted
  // by a call to pause()") a moment after the track had already moved on - and
  // the queue would never play again.
  test('keeps playing when the playback socket drops while hidden', async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    })
    const mock = await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')

    // The ended event moves the element onto the next stream synchronously, so
    // the element is genuinely playing by the time the socket drops a moment
    // later - the scenario under test.
    await page.locator('audio[src*="/api/stream/1"]').evaluate((element) => {
      element.dispatchEvent(new Event('ended'))
    })
    await expect(player(page)).toContainText("Don't Stop Me Now")
    const activeAudio = page.locator('audio[src*="/api/stream/2"]').first()
    await expect
      .poll(() => activeAudio.evaluate((element) => (element as HTMLAudioElement).paused))
      .toBe(false)

    // Record every pause() the app issues from here on, per element: the
    // assertion is then about the teardown itself rather than a paused flag
    // that a later play() could have flipped back.
    await page.evaluate(() => {
      const testWindow = window as Window & { __mediaPauses?: string[] }
      const pauses: string[] = []
      testWindow.__mediaPauses = pauses
      for (const element of Array.from(document.querySelectorAll('audio, video'))) {
        const media = element as HTMLMediaElement
        const originalPause = media.pause.bind(media)
        media.pause = () => {
          pauses.push(media.src)
          originalPause()
        }
      }
    })

    // Returns once the close handler has run and the playback-sync effect has
    // committed, still before the reconnect.
    await dropPlaybackSocket(page, mock)

    const pausedSources = await page.evaluate(
      () => (window as Window & { __mediaPauses?: string[] }).__mediaPauses ?? [],
    )
    expect(pausedSources.filter((src) => src.includes('/api/stream/2'))).toEqual([])
    expect(await activeAudio.evaluate((element) => (element as HTMLAudioElement).paused)).toBe(false)
  })

  // A stream that cannot be fetched reads as "not supported" to the media
  // element, the same as a broken file. With the phone still calling itself
  // online (a lift on mobile data, the proxy's 502 while the server restarts)
  // that is not final: no "can't be played", the stream is tried again, and
  // the queue does not stay stuck on "playing".
  test('a stream the proxy answers with 502 is tried again, not called unplayable', async ({ page }) => {
    await openApp(page)
    let healthy = false
    let requests = 0
    await page.route(/\/api\/stream\/1(\?|$)/, (route) => {
      requests += 1
      return healthy
        ? route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(30) })
        : route.fulfill({ status: 502, contentType: 'text/html', body: '<h1>502 Bad Gateway</h1>' })
    })
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')

    // The element's own request, the question "what do you answer?", a retry.
    await expect.poll(() => requests, { timeout: 10_000 }).toBeGreaterThanOrEqual(3)
    await expect(page.getByText("can't be played")).toHaveCount(0)

    healthy = true
    const main = page.locator('[data-player-element="main"]')
    await expect
      .poll(() => main.evaluate((element) => !(element as HTMLAudioElement).paused && (element as HTMLAudioElement).readyState >= 2), { timeout: 15_000 })
      .toBe(true)
    await expect(page.getByText("can't be played")).toHaveCount(0)
  })

  test('a stream that answers 404 is still called unplayable at once', async ({ page }) => {
    await openApp(page)
    let requests = 0
    await page.route(/\/api\/stream\/1(\?|$)/, (route) => {
      requests += 1
      return route.fulfill({ status: 404, json: { detail: 'Not found' } })
    })
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(page.getByText("can't be played").first()).toBeVisible()
    await settle(page)
    expect(requests).toBeLessThanOrEqual(2) // the element and the one question
  })

  // The standby's own load can fail (a blip while it was warming up). Nothing
  // retries an element that is not the active one, so promoting it as it was
  // would leave the server on "playing" with no sound.
  test('a preloaded next track whose load had failed is asked for again when it takes over', async ({ page }) => {
    await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    const standby = page.locator('[data-player-element="standby"]')
    await expect(standby).toHaveAttribute('data-stream-src', /\/api\/stream\/2/)
    // The mocked stream is not audio, so the standby's load has failed.
    await expect.poll(() => standby.evaluate((element) => (element as HTMLAudioElement).error?.code ?? null)).not.toBeNull()
    await standby.evaluate((element) => {
      const media = element as HTMLAudioElement & { __loads?: number }
      const load = media.load.bind(media)
      media.__loads = 0
      media.load = () => {
        media.__loads = (media.__loads ?? 0) + 1
        load()
      }
    })

    await player(page).getByRole('button', { name: 'Next' }).click()
    await expect(player(page)).toContainText("Don't Stop Me Now")

    await expect
      .poll(() => standby.evaluate((element) => (element as HTMLAudioElement & { __loads?: number }).__loads))
      .toBe(1)
  })

  // A socket closed with 4000 is not a dropout: another tab of this browser
  // registered the same device and plays from now on. Treated like a lost
  // connection, this page would keep playing (and advancing through the
  // queue), so two copies would run a few seconds apart and Pause in the new
  // tab could not stop this one.
  test('falls silent when another tab takes the device over', async ({ page }) => {
    const mock = await openApp(page)
    // Real (silent) audio, so the element is genuinely playing.
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(30) }))
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    const audio = page.locator('[data-player-element="main"]')
    const paused = () => audio.evaluate((element) => (element as HTMLAudioElement).paused)
    await expect.poll(paused).toBe(false)

    await mock.playbackSocket!.close({ code: 4000, reason: 'replaced' })

    await expect.poll(paused).toBe(true)
    // Nor does it move on through the queue by itself.
    await audio.evaluate((element) => element.dispatchEvent(new Event('ended')))
    await expect
      .poll(() => page.evaluate(() => Array.from(document.querySelectorAll('audio')).filter((el) => !el.paused).length))
      .toBe(0)
  })

  // The other half of the same dropout: `isActiveDevice` folds in `connected`,
  // so a suspended socket also re-runs the mediaSession effect. Nulling
  // `navigator.mediaSession.metadata` there would destroy the OS media
  // notification, and with it the page's background audio exemption: the
  // hidden page is frozen and stays silent until a full reload. The metadata
  // is what keeps Chrome Android from freezing the hidden page, so a dropout
  // must leave it - and the lock-screen handlers - alone.
  test('keeps the media session when the playback socket drops while hidden', async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    })
    await installMediaSessionStub(page)
    const mock = await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')

    // The hidden sync advance is what makes this the real scenario: the
    // element is genuinely playing the next track when the socket drops.
    await page.locator('audio[src*="/api/stream/1"]').evaluate((element) => {
      element.dispatchEvent(new Event('ended'))
    })
    await expect(player(page)).toContainText("Don't Stop Me Now")

    const beforeDropout = await readMediaSession(page)
    expect(beforeDropout.title).toBe("Don't Stop Me Now")
    expect(beforeDropout.playbackState).toBe('playing')
    expect(beforeDropout.handlers.play).toBe(true)
    expect(beforeDropout.handlers.pause).toBe(true)

    // Hold the handler references page-side so identity can be compared after
    // the drop (functions do not survive serialization).
    await page.evaluate(() => {
      const testWindow = window as Window & {
        __mediaSession?: { handlers: Record<string, unknown> }
        __mediaHandlersBeforeDropout?: Record<string, unknown>
      }
      testWindow.__mediaHandlersBeforeDropout = { ...(testWindow.__mediaSession?.handlers ?? {}) }
    })

    // Returns once the close handler has set `connected` false and the
    // mediaSession effect has committed, still before the reconnect.
    await dropPlaybackSocket(page, mock)

    const afterDropout = await readMediaSession(page)
    expect(afterDropout.title).toBe("Don't Stop Me Now")
    expect(afterDropout.playbackState).toBe('playing')
    expect(afterDropout.handlers).toMatchObject({ play: true, pause: true, nexttrack: true, previoustrack: true })

    const handlersUnchanged = await page.evaluate(() => {
      const testWindow = window as Window & {
        __mediaSession?: { handlers: Record<string, unknown> }
        __mediaHandlersBeforeDropout?: Record<string, unknown>
      }
      const before = testWindow.__mediaHandlersBeforeDropout ?? {}
      const after = testWindow.__mediaSession?.handlers ?? {}
      const actions = Object.keys(after)
      return actions.length > 0 && actions.every((action) => after[action] === before[action])
    })
    expect(handlersUnchanged).toBe(true)
  })

  // The retention above must not swallow a real hand-off: on a device
  // transfer `connected` stays true and the snapshot points at another
  // device, so this page is no longer producing sound and must drop the
  // notification rather than leave a stale one behind.
  test('clears the media session when playback transfers to another device', async ({ page }) => {
    await installMediaSessionStub(page)
    const mock = await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    // The session is set by an effect once this device is the active one,
    // which can be a render after the title shows.
    await expect.poll(async () => (await readMediaSession(page)).title).toBe('Bohemian Rhapsody')

    // This page stays connected; the server just names another device as the
    // one producing sound.
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
    pushPlaybackState(mock)

    await expect.poll(async () => (await readMediaSession(page)).title).toBe(null)
    expect((await readMediaSession(page)).playbackState).toBe('playing')
  })
})

// The current track and the next four queue items are held in memory
// (PREFETCH_AHEAD in src/player/prefetch.ts): the next one first, then the rest
// one at a time in play order, within a byte budget, and only the next one with
// the data saver on.
test.describe('Preloading the tracks ahead', () => {
  // The Songs queue from "Bohemian Rhapsody": 1, 2, 3, then these.
  const EXTRA_IDS = [11, 12, 13, 14, 15]
  const QUEUE = [1, 2, 3, ...EXTRA_IDS]

  type Downloads = {
    mock: AppMockState
    /** Track ids the app downloaded itself (fetch), in request order. */
    fetched: number[]
    /** The most of those downloads in flight at once. */
    maxInFlight: () => number
    /** Holds the download of this track until the returned function is called. */
    hold: (id: number) => () => void
  }

  async function openWithLongQueue(page: Parameters<typeof installApiMock>[0], options: { saveData?: boolean } = {}): Promise<Downloads> {
    // Each track's file is `id` seconds long, so a copy's size says which
    // track it is; the copies made and revoked are recorded by track id.
    await page.addInitScript((saveData) => {
      if (saveData) Object.defineProperty(navigator, 'connection', { configurable: true, value: { saveData: true } })
      const w = window as unknown as { __blobTrack: Record<string, number>; __revokedTracks: number[] }
      w.__blobTrack = {}
      w.__revokedTracks = []
      const create = URL.createObjectURL.bind(URL)
      const revoke = URL.revokeObjectURL.bind(URL)
      URL.createObjectURL = (object: Blob | MediaSource) => {
        const url = create(object)
        if (object instanceof Blob && object.type.startsWith('audio/')) w.__blobTrack[url] = Math.round((object.size - 44) / 16000)
        return url
      }
      URL.revokeObjectURL = (url: string) => {
        if (url in w.__blobTrack) w.__revokedTracks.push(w.__blobTrack[url])
        revoke(url)
      }
    }, options.saveData ?? false)
    const mock = await installApiMock(page)
    mock.tracks.push(...EXTRA_IDS.map((id) => makeTrack(id, { title: `Ahead ${id}` })))
    const fetched: number[] = []
    let inFlight = 0
    let maxInFlight = 0
    const held = new Map<number, Promise<void>>()
    await page.route(/\/api\/stream\/\d+(\?|$)/, async (route: Route) => {
      const id = Number(/\/api\/stream\/(\d+)/.exec(route.request().url())![1])
      const download = route.request().resourceType() === 'fetch'
      if (download) {
        fetched.push(id)
        inFlight += 1
        maxInFlight = Math.max(maxInFlight, inFlight)
      }
      try {
        await (held.get(id) ?? new Promise((resolve) => setTimeout(resolve, 50)))
        await route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(id) })
      } catch {
        // The app gave up on it (an aborted download).
      } finally {
        if (download) inFlight -= 1
      }
    })
    await page.goto('/#/home')
    await expect(page.locator('.home-view')).toBeVisible()
    return {
      mock,
      fetched,
      maxInFlight: () => maxInFlight,
      hold: (id) => {
        let release = () => {}
        held.set(id, new Promise<void>((resolve) => { release = resolve }))
        return () => release()
      },
    }
  }

  async function playFirst(page: Parameters<typeof installApiMock>[0], mock: AppMockState) {
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    expect(mock.playback.queue_media_item_ids).toEqual(QUEUE)
  }

  const copiesMade = (page: Parameters<typeof installApiMock>[0]) =>
    page.evaluate(() => Object.values((window as unknown as { __blobTrack: Record<string, number> }).__blobTrack).sort((a, b) => a - b))
  const revoked = (page: Parameters<typeof installApiMock>[0]) =>
    page.evaluate(() => [...(window as unknown as { __revokedTracks: number[] }).__revokedTracks].sort((a, b) => a - b))

  test('downloads the next four tracks one at a time, in play order, and moves the window on', async ({ page }) => {
    const { mock, fetched, maxInFlight } = await openWithLongQueue(page)
    await playFirst(page, mock)

    await expect.poll(() => fetched.join(',')).toBe('2,3,11,12')
    await page.waitForTimeout(500)
    expect(fetched).toEqual([2, 3, 11, 12])
    expect(maxInFlight()).toBe(1)
    await expect.poll(() => copiesMade(page)).toEqual([2, 3, 11, 12])
    // The next track's element took its copy.
    await expect(page.locator('audio[data-stream-src*="/api/stream/2"]')).toHaveAttribute('src', /^blob:/)

    // Track 2 plays: the window now ends at track 13, the one fetched next.
    // Track 1 streamed from the network (it was never a copy), so nothing goes.
    await player(page).getByRole('button', { name: 'Next' }).click()
    await expect(player(page)).toContainText("Don't Stop Me Now")
    await expect.poll(() => fetched.join(',')).toBe('2,3,11,12,13')
    await expect.poll(() => copiesMade(page)).toEqual([2, 3, 11, 12, 13])
    expect(await revoked(page)).toEqual([])

    // Straight to track 14 (queue position 6), which streams; 15, now the
    // next one, is fetched; 3, 11, 12 and 13 have left the window and their
    // copies are revoked. Track 2, the one just finished, is kept for Previous.
    mock.playback.queue_index = 6
    mock.playback.media_item_id = 14
    pushPlaybackState(mock, { position_changed: true })
    await expect(player(page)).toContainText('Ahead 14')
    await expect.poll(() => fetched.join(',')).toBe('2,3,11,12,13,15')
    await expect.poll(() => revoked(page)).toEqual([3, 11, 12, 13])
    expect(maxInFlight()).toBe(1)
  })

  test('a queue change stops the download of a track that left the window', async ({ page }) => {
    const { mock, fetched, hold } = await openWithLongQueue(page)
    const release = hold(11)
    await playFirst(page, mock)
    await expect.poll(() => fetched.join(',')).toBe('2,3,11')

    // Tracks 11 and 12 are taken out of the queue while 11 is downloading.
    const keep = mock.playback.queue_media_item_ids.map((id, index) => ({ id, row: mock.playback.queue_item_ids[index] }))
      .filter(({ id }) => id !== 11 && id !== 12)
    mock.playback.queue_media_item_ids = keep.map(({ id }) => id)
    mock.playback.queue_item_ids = keep.map(({ row }) => row)
    mock.playback.queue_context_count = keep.length
    pushPlaybackState(mock)

    // The window is now 2, 3, 13, 14: the downloads carry on with 13 and 14.
    await expect.poll(() => fetched.join(',')).toBe('2,3,11,13,14')
    release()
    await page.waitForTimeout(300)
    // The download of 11 was aborted: no copy of it was ever made.
    expect(await copiesMade(page)).toEqual([2, 3, 13, 14])
  })

  test('stops fetching further ahead once the byte budget is used', async ({ page }) => {
    const { mock, fetched } = await openWithLongQueue(page)
    // Copies of 2 (32 kB) and 3 (48 kB) and 11 (176 kB) fit in 300 kB; 12
    // (192 kB) does not, so it is refused on its declared length and nothing
    // after it is asked for.
    await page.evaluate(async () => {
      const url = '/src/player/prefetch.ts'
      ;(await import(/* @vite-ignore */ url)).PREFETCH_BUDGET.bytes = 300_000
    })
    await playFirst(page, mock)
    await expect.poll(() => fetched.join(',')).toBe('2,3,11,12')
    await page.waitForTimeout(500)
    expect(fetched).toEqual([2, 3, 11, 12])
    expect(await copiesMade(page)).toEqual([2, 3, 11])
  })

  test('with the data saver on, only the next track is downloaded', async ({ page }) => {
    const { mock, fetched } = await openWithLongQueue(page, { saveData: true })
    await playFirst(page, mock)
    await expect.poll(() => fetched.join(',')).toBe('2')
    await expect(page.locator('audio[data-stream-src*="/api/stream/2"]')).toHaveAttribute('src', /^blob:/)
    await page.waitForTimeout(500)
    expect(fetched).toEqual([2])
  })
})
