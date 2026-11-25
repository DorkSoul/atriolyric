// Play reports.

import { expect, test } from './fixtures'
import type { Page } from 'playwright/test'
import { type AppMockState, installApiMock, pushPlaybackState } from './mockApi'
import { openApp, player, settle, silentWav, openSettings } from './app-helpers'

// A play is reported once it has been listened to - half the track
// or four minutes - by the device producing the sound, once per play.
test.describe('Play reports follow listening', () => {
  // A media element the test drives: play() and pause() only flip `paused`
  // (nothing really plays, so no real `ended` or clock), and currentTime is a
  // plain value. Sources still load (decodable silence), so readyState is real.
  async function installFakeMedia(page: Page) {
    await page.addInitScript(() => {
      const times = new WeakMap<HTMLMediaElement, number>()
      const playing = new WeakSet<HTMLMediaElement>()
      const proto = HTMLMediaElement.prototype
      Object.defineProperty(proto, 'currentTime', {
        configurable: true,
        get(this: HTMLMediaElement) { return times.get(this) ?? 0 },
        set(this: HTMLMediaElement, value: number) { times.set(this, value) },
      })
      Object.defineProperty(proto, 'paused', { configurable: true, get(this: HTMLMediaElement) { return !playing.has(this) } })
      Object.defineProperty(proto, 'seeking', { configurable: true, get: () => false })
      proto.play = function play(this: HTMLMediaElement) { playing.add(this); return Promise.resolve() }
      proto.pause = function pause(this: HTMLMediaElement) { playing.delete(this) }
    })
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(1) }))
  }

  /** Moves the clock of the track element holding `trackId` from `from` to
   * `to` one second at a time, a `timeupdate` per step. The one the app is
   * playing when there is one (both can hold the same track for a moment). */
  async function listen(page: Page, trackId: number, from: number, to: number) {
    await page.evaluate(({ trackId, from, to }) => {
      const pattern = new RegExp(`/api/stream/${trackId}(\\?|$)`)
      const holders = Array.from(document.querySelectorAll<HTMLMediaElement>(
        '[data-player-element="main"], [data-player-element="standby"]',
      )).filter((el) => pattern.test(el.dataset.streamSrc ?? ''))
      const element = holders.find((el) => !el.paused) ?? holders[0]
      if (!element) throw new Error(`no element holds track ${trackId}`)
      for (let t = from; t <= to; t += 1) {
        element.currentTime = t
        element.dispatchEvent(new Event('timeupdate'))
      }
    }, { trackId, from, to })
  }

  type PlayReport = { id: number; started_at?: unknown; queue_item_id?: unknown; listened_seconds?: number }
  const reports = (mock: AppMockState): PlayReport[] =>
    mock.calls.filter((call) => call.method === 'POST' && /^\/api\/stream\/\d+\/played$/.test(call.path))
      .map((call) => ({ ...(call.body as Omit<PlayReport, 'id'>), id: Number(call.path.split('/')[3]) }))

  async function playBohemian(page: Page) {
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    await expect(page.locator('[data-player-element="main"]')).toHaveAttribute('data-stream-src', /\/api\/stream\/1(\?|$)/)
  }

  test('a track is recorded once, after half its length of listening and not before', async ({ page }) => {
    await installFakeMedia(page)
    const mock = await openApp(page)
    await playBohemian(page) // 355 s: counts at 177.5 s
    await listen(page, 1, 0, 176)
    await settle(page)
    expect(reports(mock)).toEqual([])

    await listen(page, 1, 177, 178)
    await expect.poll(() => reports(mock).length).toBe(1)
    const [report] = reports(mock)
    expect(report.id).toBe(1)
    expect(report.queue_item_id).toBe(mock.playback.queue_item_ids[0])
    expect(report.listened_seconds).toBeGreaterThanOrEqual(177.5)
    expect(Date.now() - Date.parse(String(report.started_at))).toBeLessThan(60_000)

    await listen(page, 1, 179, 340)
    await settle(page)
    expect(reports(mock)).toHaveLength(1)
  })

  test('a gapless advance onto the preloaded standby records the second track too', async ({ page }) => {
    await installFakeMedia(page)
    const mock = await openApp(page)
    await playBohemian(page)
    const standby = page.locator('[data-player-element="standby"]')
    await expect(standby).toHaveAttribute('data-stream-src', /\/api\/stream\/2(\?|$)/)
    await listen(page, 1, 0, 355)
    await expect.poll(() => reports(mock).map((r) => r.id)).toEqual([1])
    // Buffered enough for the hand-off (as in the gapless test above).
    await standby.evaluate((el) => {
      Object.defineProperty(el, 'readyState', { configurable: true, get: () => HTMLMediaElement.HAVE_ENOUGH_DATA })
    })

    await page.locator('[data-player-element="main"]').evaluate((el) => el.dispatchEvent(new Event('ended')))
    await expect(player(page)).toContainText("Don't Stop Me Now")
    expect(await standby.evaluate((el) => (el as HTMLMediaElement).paused)).toBe(false) // handed off, not reloaded
    await listen(page, 2, 0, 106) // 210 s: counts at 105 s
    await expect.poll(() => reports(mock).map((r) => r.id)).toEqual([1, 2])
  })

  test('a quality switch mid-track records nothing extra', async ({ page }) => {
    await installFakeMedia(page)
    const mock = await openApp(page)
    await playBohemian(page)
    await listen(page, 1, 0, 100)

    await openSettings(page, 'Playback')
    await page.locator('#settings-playback').getByRole('button', { name: '128 kbps' }).click()
    const main = page.locator('[data-player-element="main"]')
    await expect(main).toHaveAttribute('data-stream-src', /\/api\/stream\/1\?max_bitrate=128/)
    await settle(page)
    expect(reports(mock)).toEqual([])

    // The reloaded element goes on from where it was: 100 s already counted.
    await listen(page, 1, 100, 178)
    await expect.poll(() => reports(mock).length).toBe(1)
    expect(reports(mock)[0].listened_seconds).toBeLessThan(180)
    await listen(page, 1, 179, 300)
    await settle(page)
    expect(reports(mock)).toHaveLength(1)
  })

  test('seeking forward does not count as listening', async ({ page }) => {
    await installFakeMedia(page)
    const mock = await openApp(page)
    await playBohemian(page)
    await listen(page, 1, 0, 10)
    await listen(page, 1, 200, 355) // a jump to 200 s, then 155 s heard: 165 s < 177.5 s
    await settle(page)
    expect(reports(mock)).toEqual([])
  })

  test('reloading with a paused queue records nothing', async ({ page }) => {
    await installFakeMedia(page)
    const mock = await installApiMock(page)
    mock.playback.queue_media_item_ids = [1, 2]
    mock.playback.queue_item_ids = [5001, 5002]
    mock.playback.queue_context_count = 2
    mock.playback.media_item_id = 1
    mock.playback.position_seconds = 100
    mock.playback.is_playing = false
    await page.goto('/#/home')
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    await expect(page.locator('[data-player-element="main"]')).toHaveAttribute('data-stream-src', /\/api\/stream\/1(\?|$)/)
    await listen(page, 1, 100, 300) // the element is paused: nothing is heard
    await settle(page)
    expect(reports(mock)).toEqual([])
  })

  test('a skip after 10 seconds records nothing', async ({ page }) => {
    await installFakeMedia(page)
    const mock = await openApp(page)
    await playBohemian(page)
    await listen(page, 1, 0, 10)
    await player(page).getByRole('button', { name: 'Next' }).click()
    await expect(player(page)).toContainText("Don't Stop Me Now")
    await listen(page, 2, 0, 5)
    await settle(page)
    expect(reports(mock)).toEqual([])
  })

  test('a report the server could not take is retried and sent once', async ({ page }) => {
    await page.clock.install()
    await installFakeMedia(page)
    const mock = await openApp(page)
    const failed: Array<Record<string, unknown>> = []
    await page.route(/\/api\/stream\/\d+\/played$/, async (route) => {
      if (failed.length === 0) {
        failed.push(route.request().postDataJSON() as Record<string, unknown>)
        await route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"down"}' })
      } else {
        await route.fallback()
      }
    })
    await playBohemian(page)
    await listen(page, 1, 0, 178)
    await expect.poll(() => failed.length).toBe(1)
    expect(reports(mock)).toEqual([])

    await page.clock.runFor(6_000)
    await expect.poll(() => reports(mock).length).toBe(1)
    expect(reports(mock)[0].started_at).toBe(failed[0].started_at)
    await page.clock.runFor(10 * 60_000)
    await settle(page)
    expect(reports(mock)).toHaveLength(1)
  })

  // A plain video keeps its one report when it loads; that report has a body
  // with when it started, like every other.
  test('a plain video is reported once when it loads, with when it started', async ({ page }) => {
    await installFakeMedia(page)
    const mock = await openApp(page)
    await page.getByRole('button', { name: 'Videos', exact: true }).click()
    await page.locator('.browse-card').filter({ hasText: 'Shows' }).click()
    await page.locator('.video-card').filter({ hasText: 'Episode One' }).click()
    await expect(player(page)).toContainText('Episode One')
    await expect.poll(() => reports(mock).length).toBe(1)
    const [report] = reports(mock)
    expect(report.id).toBe(5)
    expect(typeof report.started_at).toBe('string')
    expect(Math.abs(Date.now() - Date.parse(String(report.started_at)))).toBeLessThan(60_000)
    await settle(page)
    expect(reports(mock)).toHaveLength(1)
  })

  test('repeat-one records each completed play', async ({ page }) => {
    await installFakeMedia(page)
    const mock = await openApp(page)
    mock.playback.repeat_mode = 'one'
    // The mock server does not model repeat-one; the page's own restart is
    // what is under test.
    mock.holdPlaybackTypes.add('track_ended')
    await playBohemian(page)
    await listen(page, 1, 0, 355)
    await expect.poll(() => reports(mock).length).toBe(1)
    await page.locator('[data-player-element="main"]').evaluate((el) => el.dispatchEvent(new Event('ended')))
    await listen(page, 1, 0, 178)
    await expect.poll(() => reports(mock).length).toBe(2)
    expect(reports(mock).map((r) => r.id)).toEqual([1, 1])
    expect(reports(mock)[1].started_at).not.toBe(reports(mock)[0].started_at)
  })

  // Every listen this device left is also reported once, with how it ended
  // (`/listened`), alongside the play reports above.
  type Outcome = {
    id: number
    started_at?: string
    end_reason?: string
    start_kind?: string | null
    source_kind?: string | null
    listened_seconds?: number
    position_seconds?: number | null
    duration_seconds?: number | null
  }
  const outcomes = (mock: AppMockState): Outcome[] =>
    mock.calls.filter((call) => call.method === 'POST' && /^\/api\/stream\/\d+\/listened$/.test(call.path))
      .map((call) => ({ ...(call.body as Omit<Outcome, 'id'>), id: Number(call.path.split('/')[3]) }))

  // The mock API's own stream route (registered after installFakeMedia's, so
  // tried first) answers with an empty body, which some engines report as an
  // unplayable file - an `error` outcome. Serve decodable silence over it.
  async function openAppWithSilence(page: Page): Promise<AppMockState> {
    await installFakeMedia(page)
    const mock = await openApp(page)
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(1) }))
    return mock
  }

  const bar = (page: Page) => player(page)
  async function next(page: Page, title: string) {
    await bar(page).getByRole('button', { name: 'Next' }).click()
    await expect(bar(page)).toContainText(title)
  }
  // Until the app has started the element holding the track (when the
  // standby was not buffered at the track end, that waits for a render),
  // a real element would not be producing `timeupdate`s for it, so `listen`
  // must not drive one yet.
  async function holding(page: Page, trackId: number) {
    await expect.poll(() => page.evaluate((trackId) => Array.from(document.querySelectorAll<HTMLMediaElement>(
      '[data-player-element="main"], [data-player-element="standby"]',
    )).some((el) => !el.paused && (el.dataset.streamSrc ?? '').includes(`/api/stream/${trackId}`)), trackId)).toBe(true)
  }

  test('a track played to its end is reported finished, started by the user', async ({ page }) => {
    const mock = await openAppWithSilence(page)
    await playBohemian(page)
    await listen(page, 1, 0, 355)
    await settle(page)
    expect(outcomes(mock)).toEqual([])
    await page.locator('[data-player-element="main"]').evaluate((el) => el.dispatchEvent(new Event('ended')))
    await expect.poll(() => outcomes(mock).length).toBe(1)
    const [outcome] = outcomes(mock)
    expect(outcome).toMatchObject({
      id: 1, end_reason: 'finished', start_kind: 'user', source_kind: 'library',
      listened_seconds: 355, duration_seconds: 355, position_seconds: 355,
    })
    // The same start as the play report, so the two join.
    expect(outcome.started_at).toBe(reports(mock)[0].started_at)
  })

  test('Next a few seconds into a track the queue moved on to is an early skip of an auto start', async ({ page }) => {
    const mock = await openAppWithSilence(page)
    await playBohemian(page)
    await listen(page, 1, 0, 355)
    await page.locator('[data-player-element="main"]').evaluate((el) => el.dispatchEvent(new Event('ended')))
    await expect(bar(page)).toContainText("Don't Stop Me Now")
    await holding(page, 2)
    await listen(page, 2, 0, 5)
    await next(page, 'Midnight City')
    await expect.poll(() => outcomes(mock).map((o) => o.id)).toEqual([1, 2])
    expect(outcomes(mock)[1]).toMatchObject({ end_reason: 'skipped', start_kind: 'auto', listened_seconds: 5, position_seconds: 5 })
  })

  test('picking another track mid-song skips the first; the new one is user-started', async ({ page }) => {
    const mock = await openAppWithSilence(page)
    await playBohemian(page)
    await listen(page, 1, 0, 20)
    await page.getByRole('button', { name: "Play Don't Stop Me Now" }).click()
    await expect(bar(page)).toContainText("Don't Stop Me Now")
    await expect.poll(() => outcomes(mock).length).toBe(1)
    expect(outcomes(mock)[0]).toMatchObject({ id: 1, end_reason: 'skipped', start_kind: 'user', listened_seconds: 20 })

    await holding(page, 2)
    await listen(page, 2, 0, 40)
    await next(page, 'Midnight City')
    await expect.poll(() => outcomes(mock).length).toBe(2)
    expect(outcomes(mock)[1]).toMatchObject({ id: 2, end_reason: 'skipped', start_kind: 'user', source_kind: 'library', listened_seconds: 40 })
  })

  test('a paused track left for another is stopped, not skipped', async ({ page }) => {
    const mock = await openAppWithSilence(page)
    await playBohemian(page)
    await listen(page, 1, 0, 20)
    await bar(page).getByRole('button', { name: 'Pause' }).click()
    await expect(bar(page).getByRole('button', { name: 'Play', exact: true })).toBeVisible()
    await page.getByRole('button', { name: "Play Don't Stop Me Now" }).click()
    await expect(bar(page)).toContainText("Don't Stop Me Now")
    await expect.poll(() => outcomes(mock).length).toBe(1)
    expect(outcomes(mock)[0]).toMatchObject({ id: 1, end_reason: 'stopped', listened_seconds: 20 })
  })

  test('a seek or a quality switch continues the same listen', async ({ page }) => {
    const mock = await openAppWithSilence(page)
    await playBohemian(page)
    await listen(page, 1, 0, 10)
    await listen(page, 1, 100, 110) // a seek to 100 s: 10 more heard
    await openSettings(page, 'Playback')
    await page.locator('#settings-playback').getByRole('button', { name: '128 kbps' }).click()
    await expect(page.locator('[data-player-element="main"]')).toHaveAttribute('data-stream-src', /\/api\/stream\/1\?max_bitrate=128/)
    await listen(page, 1, 110, 120)
    await settle(page)
    expect(outcomes(mock)).toEqual([])
    await next(page, "Don't Stop Me Now")
    await expect.poll(() => outcomes(mock).length).toBe(1)
    expect(outcomes(mock)[0]).toMatchObject({ id: 1, end_reason: 'skipped', listened_seconds: 30, position_seconds: 120 })
  })

  test('Next, Next, Next past tracks barely heard reports only what was listened to', async ({ page }) => {
    const mock = await openAppWithSilence(page)
    await playBohemian(page)
    await listen(page, 1, 0, 10)
    await next(page, "Don't Stop Me Now")
    await holding(page, 2)
    await listen(page, 2, 0, 1) // 1 s heard: scanning
    await next(page, 'Midnight City') // never even started
    await bar(page).getByRole('button', { name: 'Previous' }).click()
    await expect(bar(page)).toContainText("Don't Stop Me Now")
    await holding(page, 2)
    await listen(page, 2, 0, 1)
    await settle(page)
    await expect.poll(() => outcomes(mock).map((o) => o.id)).toEqual([1])
    expect(outcomes(mock)[0]).toMatchObject({ end_reason: 'skipped', listened_seconds: 10 })
  })

  test('a device that hands playback over reports it transferred, and nothing as a remote', async ({ page }) => {
    const mock = await openAppWithSilence(page)
    await playBohemian(page)
    await listen(page, 1, 0, 30)
    mock.playback.active_device_id = 2
    for (const device of mock.playback.devices) device.is_active = device.id === 2
    pushPlaybackState(mock)
    await expect.poll(() => outcomes(mock).length).toBe(1)
    expect(outcomes(mock)[0]).toMatchObject({ id: 1, end_reason: 'transferred', listened_seconds: 30 })

    // The other device skips on; this page only shows it.
    mock.playback.queue_index = 1
    mock.playback.media_item_id = mock.playback.queue_media_item_ids[1]
    pushPlaybackState(mock)
    await expect(bar(page)).toContainText("Don't Stop Me Now")
    await listen(page, 1, 30, 60).catch(() => {}) // an element left holding it says nothing either
    await settle(page)
    expect(outcomes(mock)).toHaveLength(1)
    expect(reports(mock)).toEqual([])
  })

  test('an outcome the server could not take is retried and sent once', async ({ page }) => {
    await page.clock.install()
    const mock = await openAppWithSilence(page)
    const failed: Array<Record<string, unknown>> = []
    await page.route(/\/api\/stream\/\d+\/listened$/, async (route) => {
      if (failed.length === 0) {
        failed.push(route.request().postDataJSON() as Record<string, unknown>)
        await route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"down"}' })
      } else {
        await route.fallback()
      }
    })
    await playBohemian(page)
    await listen(page, 1, 0, 20)
    await next(page, "Don't Stop Me Now")
    await expect.poll(() => failed.length).toBe(1)
    expect(outcomes(mock)).toEqual([])

    await page.clock.runFor(6_000)
    await expect.poll(() => outcomes(mock).length).toBe(1)
    expect(outcomes(mock)[0]).toMatchObject({ id: 1, end_reason: 'skipped', started_at: failed[0].started_at })
    await page.clock.runFor(10 * 60_000)
    await settle(page)
    expect(outcomes(mock)).toHaveLength(1)
  })

  test('a track that cannot be played ends as an error, never a skip', async ({ page }) => {
    const mock = await openAppWithSilence(page)
    await playBohemian(page)
    await listen(page, 1, 0, 10)
    // The file turns out to be unplayable (MEDIA_ERR_SRC_NOT_SUPPORTED).
    await page.locator('[data-player-element="main"]').evaluate((el) => {
      Object.defineProperty(el, 'error', {
        configurable: true,
        get: () => (/\/api\/stream\/1(\?|$)/.test((el as HTMLElement).dataset.streamSrc ?? '') ? { code: 4 } : null),
      })
      el.dispatchEvent(new Event('error'))
    })
    await expect(page.getByText("can't be played").first()).toBeVisible()
    await next(page, "Don't Stop Me Now")
    await expect.poll(() => outcomes(mock).length).toBe(1)
    expect(outcomes(mock)[0]).toMatchObject({ id: 1, end_reason: 'error', listened_seconds: 10 })
  })

  test('a page closed or reloaded mid-track says it cannot tell how the listen ended', async ({ page }) => {
    const mock = await openAppWithSilence(page)
    await playBohemian(page)
    await listen(page, 1, 0, 20)
    await settle(page)
    expect(outcomes(mock)).toEqual([])
    await page.reload()
    // The report sent as the page went away; the copy kept in the tab's queue
    // can already have been sent again by the time this looks (a fast reload).
    await expect.poll(() => outcomes(mock).length).toBeGreaterThanOrEqual(1)
    expect(outcomes(mock)[0]).toMatchObject({ id: 1, end_reason: 'unknown', listened_seconds: 20 })
    // Kept in the tab's queue too: sent again after the reload, recorded once.
    await expect.poll(() => outcomes(mock).length).toBeLessThanOrEqual(2)
    expect(new Set(outcomes(mock).map((o) => o.started_at)).size).toBe(1)
  })

  test('each repeat-one run is its own finished listen', async ({ page }) => {
    const mock = await openAppWithSilence(page)
    mock.playback.repeat_mode = 'one'
    mock.holdPlaybackTypes.add('track_ended')
    await playBohemian(page)
    const main = page.locator('[data-player-element="main"]')
    for (let run = 0; run < 2; run += 1) {
      await listen(page, 1, 0, 355)
      await main.evaluate((el) => el.dispatchEvent(new Event('ended')))
    }
    await expect.poll(() => outcomes(mock).length).toBe(2)
    const [first, second] = outcomes(mock)
    expect([first.end_reason, second.end_reason]).toEqual(['finished', 'finished'])
    expect(second.start_kind).toBe('auto')
    expect(second.started_at).not.toBe(first.started_at)
  })
})
