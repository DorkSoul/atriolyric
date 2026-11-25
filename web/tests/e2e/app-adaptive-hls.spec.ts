// Adaptive quality, hls.js and recovering from media errors.

import { expect, test } from './fixtures'
import type { Page, Route } from 'playwright/test'
import { installApiMock, mockStates, pushPlaybackState } from './mockApi'
import { emitFakeHls, fakeHlsCalls, fakeHlsCount, installFakeHls, openApp, player, settle, silentWav, openSettings } from './app-helpers'

test.describe('Atriolyric browser flows', () => {
  test('lowers playback quality when adaptive mode detects buffering', async ({ page }) => {
    await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')

    await openSettings(page, 'Playback')
    const playback = page.locator('#settings-playback')
    await playback.getByRole('button', { name: '320 kbps' }).click()
    await playback.getByRole('switch', { name: 'Automatically lower quality on slow networks' }).check()

    const activeAudio = page.locator('audio[src*="/api/stream/1?max_bitrate=320"]').first()
    // The buffering monitor's listeners are attached by an effect.
    await settle(page)
    await activeAudio.evaluate((element) => {
      const media = element as HTMLMediaElement
      media.currentTime = 5
      media.dispatchEvent(new Event('waiting'))
    })
    await expect(page.locator('.toast-warning')).toContainText(
      'Reduced playback quality to 192 due to connection speed.',
    )
    await expect(page.locator('audio[src*="/api/stream/1?max_bitrate=192"]')).toHaveCount(1)
  })

  // Bandwidth stub shared by the adaptive upgrade tests: every stream request
  // looks like it was delivered at `__adaptiveTestBandwidthKbps`.
  async function stubMeasuredBandwidth(page: Parameters<typeof installApiMock>[0]) {
    await page.evaluate(() => {
      const testWindow = window as Window & { __adaptiveTestBandwidthKbps?: number }
      testWindow.__adaptiveTestBandwidthKbps = 1_000
      const nativeGetEntriesByName = performance.getEntriesByName.bind(performance)
      Object.defineProperty(performance, 'getEntriesByName', {
        configurable: true,
        value: (name: string, type?: string): PerformanceEntry[] => {
          if (!name.includes('/api/stream/')) return nativeGetEntriesByName(name, type)
          const bandwidthKbps = testWindow.__adaptiveTestBandwidthKbps ?? 0
          const bytes = (bandwidthKbps * 1_000) / 8
          const now = performance.now()
          const streamKbps = /max_bitrate=(\d+)/.exec(name)?.[1] ?? '1000'
          return [0, 1].map((index) => {
            const responseStart = now - (index + 2) * 1_000
            return {
              name,
              entryType: 'resource',
              startTime: responseStart,
              responseStart,
              responseEnd: responseStart + 1_000,
              encodedBodySize: bytes,
              transferSize: bytes + 300,
              serverTiming: [
                { name: 'source-bitrate', description: '1000', duration: 0 },
                { name: 'stream-bitrate', description: streamKbps, duration: 0 },
              ],
            } as unknown as PerformanceResourceTiming
          })
        },
      })
    })
  }

  async function startAdaptivePlayback(page: Parameters<typeof installApiMock>[0]) {
    await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')

    await openSettings(page, 'Playback')
    const playback = page.locator('#settings-playback')
    await playback.getByRole('button', { name: '320 kbps' }).click()
    await playback.getByRole('switch', { name: 'Automatically lower quality on slow networks' }).check()

    const activeAudio = page.locator('audio[src*="/api/stream/1"]').first()
    await expect(activeAudio).toHaveAttribute('src', /max_bitrate=320/)
    await activeAudio.evaluate((element) => {
      const media = element as HTMLMediaElement
      Object.defineProperty(media, 'readyState', { configurable: true, get: () => 4 })
      Object.defineProperty(media, 'buffered', {
        configurable: true,
        get: () => ({ length: 1, start: () => 0, end: () => media.currentTime + 45 }),
      })
    })
    return activeAudio
  }

  test('jumps straight to the sustainable rung and only upgrades on the next track', async ({ page }) => {
    await page.clock.install()
    const activeAudio = await startAdaptivePlayback(page)
    await stubMeasuredBandwidth(page)
    // 320 kbps needs more than 75% of 400 kbps, 192 does too; 128 is the
    // highest rung that fits, and it is reached in one step.
    await page.evaluate(() => {
      ;(window as Window & { __adaptiveTestBandwidthKbps?: number }).__adaptiveTestBandwidthKbps = 200
    })
    await page.clock.runFor(3_000)
    await activeAudio.evaluate((element) => {
      const media = element as HTMLMediaElement
      media.currentTime = 5
      media.dispatchEvent(new Event('waiting'))
    })
    await expect(activeAudio).toHaveAttribute('src', /max_bitrate=128/)

    // The connection recovers: the upgrade target is decided but never applied
    // while this song is playing.
    await page.evaluate(() => {
      ;(window as Window & { __adaptiveTestBandwidthKbps?: number }).__adaptiveTestBandwidthKbps = 2_000
    })
    await activeAudio.evaluate((element) => element.dispatchEvent(new Event('timeupdate')))
    await page.clock.runFor(40_000)
    await expect(activeAudio).toHaveAttribute('src', /max_bitrate=128/)

    // The next track is fetched at the upgraded quality straight away.
    await expect(page.locator('audio[data-stream-src*="/api/stream/2?max_bitrate=320"]')).toHaveCount(1)
    await player(page).getByRole('button', { name: 'Next' }).click()
    await expect(player(page)).toContainText("Don't Stop Me Now")
    await expect(page.locator('audio[data-stream-src*="/api/stream/2?max_bitrate=320"]')).toHaveCount(1)
  })

  // The buffering monitor measures bandwidth from the stream requests made
  // since its track's source started. Anything that hands it a new `current`
  // object re-runs its effect without touching the source; that must not reset
  // the start, or the next `waiting` has no samples and steps down one rung
  // blindly (320 -> 192) instead of to the sustainable one. Playing from the
  // song list resolves track 1 twice at once (the current track and the
  // queue), the later copy replaces the cached one, and the next snapshot (a
  // position-save echo) swaps `current` for it just before the `waiting`.
  for (const trigger of ['a snapshot', 'liking the track'] as const) {
    test(`a re-render from ${trigger} does not cost the monitor its bandwidth samples`, async ({ page }) => {
      await page.clock.install()
      const audio = await startAdaptivePlayback(page)
      const mock = mockStates.get(page)!
      await stubMeasuredBandwidth(page)
      await page.evaluate(() => {
        ;(window as Window & { __adaptiveTestBandwidthKbps?: number }).__adaptiveTestBandwidthKbps = 200
      })
      await page.clock.runFor(3_000)
      if (trigger === 'a snapshot') {
        pushPlaybackState(mock)
      } else {
        await player(page).getByRole('button', { name: /^Not liked: .*Press to like$/ }).click()
        await expect(player(page).getByRole('button', { name: /^Liked: .*Press to dislike$/ })).toBeVisible()
      }
      await settle(page)
      await audio.evaluate((element) => {
        const media = element as HTMLMediaElement
        media.currentTime = 5
        media.dispatchEvent(new Event('waiting'))
      })
      await expect(audio).toHaveAttribute('src', /max_bitrate=128/)
    })
  }

  test('does not queue an adaptive upgrade that the measured bandwidth cannot sustain', async ({ page }) => {
    await page.clock.install()
    const activeAudio = await startAdaptivePlayback(page)
    await stubMeasuredBandwidth(page)
    await activeAudio.evaluate((element) => {
      const media = element as HTMLMediaElement
      media.currentTime = 5
      media.dispatchEvent(new Event('waiting'))
    })
    await expect(activeAudio).toHaveAttribute('src', /max_bitrate=(192|128)/)
    // 300 kbps * 75% = 225 kbps: 320 does not fit, so no upgrade is queued.
    await page.evaluate(() => {
      ;(window as Window & { __adaptiveTestBandwidthKbps?: number }).__adaptiveTestBandwidthKbps = 300
    })
    await activeAudio.evaluate((element) => element.dispatchEvent(new Event('timeupdate')))
    await page.clock.runFor(40_000)
    await expect(page.locator('audio[data-stream-src*="/api/stream/2?max_bitrate=320"]')).toHaveCount(0)
  })

  // After a downgrade every following track inherits the lower quality, is
  // downloaded whole ahead of time and played from memory, where the buffer
  // monitor has nothing to measure. The downloads themselves are the
  // measurement.
  test('a fast download of an upcoming track restores the quality from the track after next', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    // Decodable streams. A download of an upcoming track (a fetch) is held
    // until the test lets it through; the playing element's own stream is not.
    const streamId = /\/api\/stream\/(\d+)(\?|$)/
    const asked: string[] = []
    const held = new Map<string, Route>()
    const serve = (route: Route) => route.fulfill({
      status: 200,
      contentType: 'audio/wav',
      headers: { 'Server-Timing': 'source-bitrate;desc="1000.000"' },
      body: silentWav(30),
    }).catch(() => {})
    await page.route(streamId, async (route) => {
      const url = new URL(route.request().url())
      const key = `${url.pathname}${url.search}`
      asked.push(key)
      if (route.request().resourceType() === 'fetch') held.set(key, route)
      else await serve(route)
    })
    const release = async (key: string) => {
      await expect.poll(() => held.has(key), `a download of ${key}`).toBe(true)
      await serve(held.get(key)!)
      held.delete(key)
    }

    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    await openSettings(page, 'Playback')
    const playback = page.locator('#settings-playback')
    await playback.getByRole('button', { name: '320 kbps' }).click()
    await playback.getByRole('switch', { name: 'Automatically lower quality on slow networks' }).check()
    const [first, second, third] = mock.playback.queue_media_item_ids
    const active = () => page.evaluate(() => {
      const audio = Array.from(document.querySelectorAll('audio'))
        .find((a) => !a.paused && !a.error && a.readyState > 0 && a.dataset.streamSrc)
      return audio?.dataset.streamSrc ?? null
    })
    await expect.poll(active).toBe(`/api/stream/${first}?max_bitrate=320`)
    await settle(page)

    // The connection stalls: one rung down, now.
    await page.locator(`audio[data-stream-src="/api/stream/${first}?max_bitrate=320"]`).evaluate((element) => {
      const media = element as HTMLMediaElement
      Object.defineProperty(media, 'currentTime', { configurable: true, writable: true, value: 5 })
      media.dispatchEvent(new Event('waiting'))
    })
    await expect.poll(active).toBe(`/api/stream/${first}?max_bitrate=192`)
    const askedBefore = asked.length

    // The next track is downloaded at the lower quality. That download is
    // slow (480 kB in 40 s is under 100 kbps): nothing to step up on.
    await expect.poll(() => held.has(`/api/stream/${second}?max_bitrate=192`)).toBe(true)
    await page.clock.runFor(40_000)
    await release(`/api/stream/${second}?max_bitrate=192`)
    // The one after it arrives at once: the connection is back.
    await release(`/api/stream/${third}?max_bitrate=192`)
    await settle(page)
    // Never mid-track, and not for the next track, whose copy is in memory.
    await expect.poll(active).toBe(`/api/stream/${first}?max_bitrate=192`)

    const end = () => page.evaluate(() => {
      const playing = Array.from(document.querySelectorAll('audio')).find((a) => !a.paused && !a.error && a.readyState > 0)
      playing!.dispatchEvent(new Event('ended'))
    })
    await end()
    await expect.poll(active).toBe(`/api/stream/${second}?max_bitrate=192`)
    // The track after it is fetched at the restored quality while this plays...
    await release(`/api/stream/${third}?max_bitrate=320`)
    await expect(page.locator(`audio[data-stream-src="/api/stream/${third}?max_bitrate=320"]`)).toHaveCount(1)
    // ...and plays at it.
    await end()
    await expect.poll(active).toBe(`/api/stream/${third}?max_bitrate=320`)
    // The next track itself was never fetched again at the higher quality.
    expect(asked.slice(askedBefore)).not.toContain(`/api/stream/${second}?max_bitrate=320`)
  })

  // The decision itself, in the module (there is no unit-test runner in web/:
  // it is loaded into a bare page through Vite).
  test('the upgrade decided from a download keeps the margin, the cooldown and the rejected qualities', async ({ page }) => {
    await page.route('**/adaptive-blank', (route) =>
      route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>blank</title>' }))
    await page.goto('/adaptive-blank')
    const results = await page.evaluate(async () => {
      type Adaptive = typeof import('../../src/player/adaptiveBitrate')
      const modulePath = '/src/player/adaptiveBitrate.ts'
      const adaptive: Adaptive = await import(/* @vite-ignore */ modulePath)
      const MB = 1024 * 1024
      // `null` is a quality (the original file), so "no upgrade" is spelled out.
      const decide = (overrides: Partial<Parameters<Adaptive['downloadUpgradeQuality']>[0]>) => {
        const quality = adaptive.downloadUpgradeQuality({
          quality: 96,
          maxBitrate: 320,
          // 2 MB in 4 s: about 4,200 kbps.
          sample: { bytes: 2 * MB, elapsedMs: 4_000, sourceKbps: 1_000 },
          rejected: [],
          msSinceLastSwitch: 60_000,
          ...overrides,
        })
        return quality === undefined ? 'stay' : quality
      }
      return {
        kbps: Math.round(adaptive.downloadThroughputKbps({ bytes: 2 * MB, elapsedMs: 4_000 }) ?? 0),
        tooSmall: adaptive.downloadThroughputKbps({ bytes: 10_000, elapsedMs: 1 }) ?? 'unknown',
        fast: decide({}),
        // 2 MB in 60 s is 280 kbps: 75% of it carries 192, not 320.
        middling: decide({ sample: { bytes: 2 * MB, elapsedMs: 60_000, sourceKbps: 1_000 } }),
        // 2 MB in 300 s is 56 kbps: nothing above 96.
        slow: decide({ sample: { bytes: 2 * MB, elapsedMs: 300_000, sourceKbps: 1_000 } }),
        small: decide({ sample: { bytes: 10_000, elapsedMs: 1, sourceKbps: 1_000 } }),
        coolingDown: decide({ msSinceLastSwitch: 5_000 }),
        topRejected: decide({ rejected: [320] }),
        allRejected: decide({ rejected: [320, 192, 128] }),
        atTop: decide({ quality: 320 }),
        // "Original" is on the ladder only with no bitrate limit, and needs the source's bitrate.
        original: decide({ maxBitrate: null }),
        originalUnknownSource: decide({ maxBitrate: null, sample: { bytes: 2 * MB, elapsedMs: 4_000 } }),
      }
    })
    expect(results).toEqual({
      kbps: 4194,
      tooSmall: 'unknown',
      fast: 320,
      middling: 192,
      slow: 'stay',
      small: 'stay',
      coolingDown: 'stay',
      topRejected: 192,
      allRejected: 'stay',
      atTop: 'stay',
      original: null,
      originalUnknownSource: 320,
    })
  })

  // hls.js recovers a fatal network or media error in place. The recovery
  // count resets once playback moves on, so a long video is not ended by its
  // third fatal error when the earlier ones recovered; a network recovery
  // reloads after a pause. The clock is paused, so the reload delay is exact.
  test('recovers hls.js errors after a pause and gives up only without progress', async ({ page }) => {
    await page.clock.install()
    await openApp(page)
    await installFakeHls(page, 5)
    await page.getByRole('button', { name: 'Videos', exact: true }).click()
    await page.locator('.browse-card').filter({ hasText: 'Shows' }).click()
    await page.locator('.video-card').filter({ hasText: 'Episode One' }).click()
    await expect(player(page)).toContainText('Episode One')
    await expect.poll(() => fakeHlsCount(page)).toBe(1)
    expect(await fakeHlsCalls(page, 0, 'loadSource /api/stream/5/hls/index.m3u8')).toBe(1)
    await page.clock.pauseAt(new Date(Date.now() + 5_000))
    const fatal = (type: string) => emitFakeHls(page, 0, 'hlsError', { type, details: 'test', fatal: true })
    const video = page.locator('video.nowplaying-video-el')
    const playedTo = async (seconds: number) => {
      await video.evaluate((element, to) => { (element as HTMLVideoElement).currentTime = to }, seconds)
      await emitFakeHls(page, 0, 'hlsFragLoaded')
    }
    const failure = page.locator('.toast').filter({ hasText: 'Couldn\'t play "Episode One"' })

    // A network error reloads after a short pause, not at once.
    await fatal('networkError')
    expect(await fakeHlsCalls(page, 0, 'startLoad')).toBe(0)
    await page.clock.runFor(1_000)
    expect(await fakeHlsCalls(page, 0, 'startLoad')).toBe(1)

    // Playback moving on after a recovery starts the count again...
    await playedTo(10)
    await fatal('mediaError')
    await playedTo(20)
    await fatal('mediaError')
    await playedTo(30)
    await fatal('mediaError')
    // ...but a fragment loading where it failed is no progress.
    await emitFakeHls(page, 0, 'hlsFragLoaded')
    await fatal('mediaError')
    await fatal('mediaError')
    // Each of those five was recovered (a final one is not: it goes to the user).
    expect(await fakeHlsCalls(page, 0, 'recoverMediaError')).toBe(5)
    // Three failures in a row at one spot are final.
    await fatal('mediaError')
    await expect(failure).toBeVisible()
    expect(await fakeHlsCalls(page, 0, 'recoverMediaError')).toBe(5)
  })

  test('says why a busy converter stops a video instead of retrying', async ({ page }) => {
    await openApp(page)
    await installFakeHls(page, 5)
    await page.getByRole('button', { name: 'Videos', exact: true }).click()
    await page.locator('.browse-card').filter({ hasText: 'Shows' }).click()
    await page.locator('.video-card').filter({ hasText: 'Episode One' }).click()
    await expect(player(page)).toContainText('Episode One')
    await expect.poll(() => fakeHlsCount(page)).toBe(1)
    await emitFakeHls(page, 0, 'hlsError', {
      type: 'networkError', details: 'fragLoadError', fatal: true, response: { code: 503 },
    })
    await expect(page.locator('.toast').filter({
      hasText: 'Too many videos are being converted right now - try again in a moment',
    })).toBeVisible()
    await expect(page.locator('.toast').filter({ hasText: 'Couldn\'t play' })).toHaveCount(0)
    expect(await fakeHlsCalls(page, 0, 'startLoad')).toBe(0)
  })

  test('does not reload a destroyed hls.js instance', async ({ page }) => {
    await page.clock.install()
    await openApp(page)
    await installFakeHls(page, 5)
    await page.getByRole('button', { name: 'Videos', exact: true }).click()
    await page.locator('.browse-card').filter({ hasText: 'Shows' }).click()
    await page.locator('.video-card').filter({ hasText: 'Episode One' }).click()
    await expect(player(page)).toContainText('Episode One')
    await expect.poll(() => fakeHlsCount(page)).toBe(1)
    await page.clock.pauseAt(new Date(Date.now() + 5_000))

    await emitFakeHls(page, 0, 'hlsError', { type: 'networkError', details: 'test', fatal: true })
    // A song takes over before the pause is up: the video's instance is gone.
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    await expect.poll(() => fakeHlsCalls(page, 0, 'destroy')).toBe(1)
    await page.clock.runFor(2_000)
    expect(await fakeHlsCalls(page, 0, 'startLoad')).toBe(0)
  })
})

// An `error` on the playing element is recovered by reloading it, not ended
// with a toast blaming the file for a dropped connection: Play loads the
// errored element again instead of calling play() on it, and radio recovers
// too.
test.describe('Recovering from media errors', () => {
  /** Fires `error` on the element as if it had failed with `code`
   * (2 network, 3 decode, 4 not supported). `sticky` leaves the error set. */
  async function failMedia(element: ReturnType<Page['locator']>, code: number, sticky = false) {
    await element.evaluate((el, { code, sticky }) => {
      Object.defineProperty(el, 'error', { configurable: true, get: () => ({ code, message: 'test failure' }) })
      el.dispatchEvent(new Event('error'))
      if (!sticky) delete (el as unknown as { error?: unknown }).error
    }, { code, sticky })
  }

  /** Counts the app's load() calls on the element. */
  async function countLoads(element: ReturnType<Page['locator']>) {
    await element.evaluate((el) => {
      const media = el as HTMLMediaElement & { __loads?: number }
      media.__loads = 0
      const load = media.load.bind(media)
      media.load = () => {
        media.__loads = (media.__loads ?? 0) + 1
        load()
      }
    })
    return () => element.evaluate((el) => (el as HTMLMediaElement & { __loads?: number }).__loads ?? 0)
  }

  async function startTrack(page: Page) {
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(30) }))
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    const active = page.locator('audio[data-stream-src*="/api/stream/1"]').first()
    await expect.poll(() => active.evaluate((el) => (el as HTMLMediaElement).paused)).toBe(false)
    return active
  }

  /** Plays on to `seconds` the way the element reports it (once it has
   * data). The mocked stream is not seekable, so the element's clock is a
   * plain value the app can read and set. */
  async function playedTo(element: ReturnType<Page['locator']>, seconds: number) {
    await expect.poll(() => element.evaluate((el) => (el as HTMLMediaElement).readyState)).toBeGreaterThanOrEqual(2)
    await element.evaluate((el, to) => {
      let time = to
      Object.defineProperty(el, 'currentTime', { configurable: true, get: () => time, set: (value: number) => { time = value } })
      el.dispatchEvent(new Event('timeupdate'))
    }, seconds)
  }

  const toasts = (page: Page) => page.locator('.toast')

  test('a network error mid-track reloads the same stream at the same position', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    const active = await startTrack(page)
    await playedTo(active, 12)
    const loads = await countLoads(active)

    await failMedia(active, 2)
    expect(await loads()).toBe(0)
    await page.clock.runFor(1_000)
    expect(await loads()).toBe(1)
    await expect(active).toHaveAttribute('src', /\/api\/stream\/1(\?|$)/)
    expect(await active.evaluate((el) => (el as HTMLMediaElement).currentTime)).toBe(12)
    expect(await active.evaluate((el) => (el as HTMLMediaElement).paused)).toBe(false)
    await expect(toasts(page)).toHaveCount(0)
    expect(mock.wsMessages.filter((m) => m.type === 'pause')).toHaveLength(0)
  })

  test('gives up after three failed reloads with one message, paused, and tells the server', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    const active = await startTrack(page)
    const loads = await countLoads(active)
    const bar = player(page)

    for (const [delay, expected] of [[1_000, 1], [3_000, 2], [8_000, 3]] as const) {
      await failMedia(active, 2)
      await page.clock.runFor(delay)
      expect(await loads()).toBe(expected)
    }
    await expect(toasts(page)).toHaveCount(0)
    await failMedia(active, 2, true)
    await expect(toasts(page).filter({ hasText: 'Lost connection while playing "Bohemian Rhapsody"' })).toHaveCount(1)
    await expect(toasts(page)).toHaveCount(1)
    expect(await active.evaluate((el) => (el as HTMLMediaElement).paused)).toBe(true)
    await expect.poll(() => mock.wsMessages.filter((m) => m.type === 'pause').length).toBe(1)
    await expect.poll(() => mock.playback.is_playing).toBe(false)
    // No skipping on: a dead network would burn through the queue.
    expect(mock.wsMessages.filter((m) => m.type === 'next' || m.type === 'track_ended')).toHaveLength(0)
    await page.clock.runFor(30_000)
    expect(await loads()).toBe(3)

    // Play on the errored element loads it again rather than calling play() on it.
    await bar.getByRole('button', { name: 'Play', exact: true }).click()
    expect(await loads()).toBe(4)
    await expect.poll(() => mock.playback.is_playing).toBe(true)
  })

  test('a decode error on the in-memory copy falls back to the stream', async ({ page }) => {
    await page.clock.install()
    await openApp(page)
    await startTrack(page)
    const next = page.locator('audio[data-stream-src*="/api/stream/2"]').first()
    await expect(next).toHaveAttribute('src', /^blob:/)
    await player(page).getByRole('button', { name: 'Next' }).click()
    await expect(player(page)).toContainText("Don't Stop Me Now")
    await expect.poll(() => next.evaluate((el) => (el as HTMLMediaElement).paused)).toBe(false)

    await failMedia(next, 3)
    await page.clock.runFor(1_000)
    await expect(next).toHaveAttribute('src', /\/api\/stream\/2(\?|$)/)
    await expect(toasts(page)).toHaveCount(0)
  })

  test('an unsupported file is not retried and says so at once', async ({ page }) => {
    await page.clock.install()
    await openApp(page)
    const active = await startTrack(page)
    const loads = await countLoads(active)
    await failMedia(active, 4)
    await expect(toasts(page).filter({ hasText: '"Bohemian Rhapsody" can\'t be played (missing or unsupported file)' })).toHaveCount(1)
    await page.clock.runFor(15_000)
    expect(await loads()).toBe(0)
  })

  test('the connection coming back retries at once', async ({ page }) => {
    await page.clock.install()
    await openApp(page)
    const active = await startTrack(page)
    const loads = await countLoads(active)
    await failMedia(active, 2)
    await page.evaluate(() => window.dispatchEvent(new Event('online')))
    expect(await loads()).toBe(1)
  })

  test('pressing play on an element that failed reloads it', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    const active = await startTrack(page)
    const loads = await countLoads(active)
    await failMedia(active, 4, true)
    await expect(toasts(page)).toHaveCount(1)
    const bar = player(page)
    // The server still says playing: Pause pauses as usual...
    await bar.getByRole('button', { name: 'Pause', exact: true }).click()
    await expect.poll(() => mock.playback.is_playing).toBe(false)
    expect(await loads()).toBe(0)
    // ...and Play loads the failed element again rather than calling play() on it.
    await bar.getByRole('button', { name: 'Play', exact: true }).click()
    expect(await loads()).toBe(1)
    await expect.poll(() => mock.playback.is_playing).toBe(true)
  })

  test.describe('radio', () => {
    async function startRadio(page: Page) {
      // A stream that decodes: the mock's empty one fails by itself in some
      // engines, which would start the recovery under test on its own.
      await page.route(/\/api\/radio\/stations\/\d+\/stream/, (route) =>
        route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(30) }))
      await page.locator('.home-view .rail-card').filter({ hasText: 'Radio Paradise' }).click()
      await expect(player(page)).toContainText('Radio Paradise')
      const radio = page.locator('audio[src*="/api/radio/stations/"]')
      await expect(radio).toHaveCount(1)
      return radio
    }

    test('an error reconnects with a fresh URL, and three failures stop with a message', async ({ page }) => {
      await page.clock.install()
      await openApp(page)
      const radio = await startRadio(page)
      const first = await radio.getAttribute('src')

      await failMedia(radio, 2)
      await page.clock.runFor(1_000)
      const second = await radio.getAttribute('src')
      expect(second).toMatch(/\/api\/radio\/stations\/\d+\/stream\?reconnect=/)
      expect(second).not.toBe(first)
      for (const delay of [3_000, 8_000]) {
        const before = await radio.getAttribute('src')
        await failMedia(radio, 2)
        await page.clock.runFor(delay)
        expect(await radio.getAttribute('src')).not.toBe(before)
      }
      await expect(toasts(page).filter({ hasText: 'Radio Paradise' })).toHaveCount(0)
      const last = await radio.getAttribute('src')
      await failMedia(radio, 2)
      await expect(toasts(page).filter({ hasText: 'Couldn\'t play "Radio Paradise"' })).toHaveCount(1)
      await page.clock.runFor(30_000)
      expect(await radio.getAttribute('src')).toBe(last)
      await expect(player(page).getByRole('button', { name: 'Play', exact: true })).toBeVisible()
    })

    /** Every stream request answers `status`; counts the media element's
     * requests and the app's status probes (a `fetch`) apart. */
    async function refuseStreams(page: Page, status: number) {
      const seen = { media: 0, probes: 0 }
      await page.route(/\/api\/radio\/stations\/\d+\/stream/, (route) => {
        if (route.request().resourceType() === 'fetch') seen.probes += 1
        else seen.media += 1
        return route.fulfill({ status, json: { detail: status === 429 ? 'Too many streams' : 'Station unavailable' } })
      })
      return seen
    }

    test('a stream refused with 429 says too many streams are playing and is not retried', async ({ page }) => {
      await page.clock.install()
      await openApp(page)
      const seen = await refuseStreams(page, 429)
      await page.locator('.home-view .rail-card').filter({ hasText: 'Radio Paradise' }).click()
      const radio = page.locator('audio[src*="/api/radio/stations/"]')
      await expect(radio).toHaveCount(1)
      const first = await radio.getAttribute('src')
      // Some engines report the refused load by themselves; make sure every one does.
      await failMedia(radio, 4)
      await expect(toasts(page).filter({ hasText: 'Too many radio streams are playing right now' })).toHaveCount(1)
      await expect(toasts(page).filter({ hasText: 'may be offline' })).toHaveCount(0)
      expect(seen.probes).toBeGreaterThanOrEqual(1)
      const probes = seen.probes
      await page.clock.runFor(30_000)
      expect(await radio.getAttribute('src')).toBe(first)
      expect(seen.probes).toBe(probes)
      await expect(player(page).getByRole('button', { name: 'Play', exact: true })).toBeVisible()
    })

    for (const status of [404, 502]) {
      test(`a stream answering ${status} is retried and then called offline`, async ({ page }) => {
        await page.clock.install()
        await openApp(page)
        const seen = await refuseStreams(page, status)
        await page.locator('.home-view .rail-card').filter({ hasText: 'Radio Paradise' }).click()
        const radio = page.locator('audio[src*="/api/radio/stations/"]')
        await expect(radio).toHaveCount(1)
        const first = await radio.getAttribute('src')
        await failMedia(radio, 4)
        await expect.poll(() => seen.probes).toBeGreaterThanOrEqual(1)
        await page.clock.runFor(1_000)
        expect(await radio.getAttribute('src')).not.toBe(first)
        // Each reload fails again (by itself in some engines; failMedia makes
        // sure in all) until the retries run out. A toast lasts a few
        // seconds, so look for it after every step.
        const offline = toasts(page).filter({ hasText: 'Couldn\'t play "Radio Paradise" - the station may be offline.' })
        for (let step = 0; step < 30 && (await offline.count()) === 0; step++) {
          await failMedia(radio, 4)
          await page.clock.runFor(1_000)
        }
        await expect(offline).toHaveCount(1)
        await expect(toasts(page).filter({ hasText: 'Too many radio streams' })).toHaveCount(0)
        expect(seen.media).toBeGreaterThanOrEqual(1 + 3) // the first load and three reloads
      })
    }

    test('a stream that stalls for 10 seconds is reconnected', async ({ page }) => {
      await page.clock.install()
      await openApp(page)
      const radio = await startRadio(page)
      const first = await radio.getAttribute('src')
      // Once the stream has loaded, so that nothing real moves it on.
      await expect.poll(() => radio.evaluate((el) => (el as HTMLMediaElement).readyState)).toBeGreaterThanOrEqual(2)
      await radio.evaluate((el) => el.dispatchEvent(new Event('waiting')))
      await page.clock.runFor(9_000)
      expect(await radio.getAttribute('src')).toBe(first)
      await page.clock.runFor(1_500)
      expect(await radio.getAttribute('src')).not.toBe(first)
    })

    test('choosing the same station again reloads it', async ({ page }) => {
      await openApp(page)
      const radio = await startRadio(page)
      const first = await radio.getAttribute('src')
      await page.locator('.home-view .rail-card').filter({ hasText: 'Radio Paradise' }).click()
      await expect.poll(() => radio.getAttribute('src')).not.toBe(first)
    })
  })
})
