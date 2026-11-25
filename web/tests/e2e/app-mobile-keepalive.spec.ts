// The mobile media session keepalive.

import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'
import { emitFakeHls, fakeHlsCalls, fakeHlsCount, installFakeHls, openApp, player, silentWav } from './app-helpers'

// Chrome on Android 15 abandons audio focus the moment no element is playing
// and then denies the next track's fresh request when the app has no
// foreground service (the new track stays paused and the page is frozen). A
// silent looping element keeps Chrome's media session, and its foreground
// service, alive across every hand-off. Applied to every mobile browser.
test.describe('Android Chrome media session keepalive', () => {
  test.use({
    userAgent:
      'Mozilla/5.0 (Linux; Android 15; Blade20) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Mobile Safari/537.36',
  })

  async function startPlayback(page: Parameters<typeof installApiMock>[0]) {
    await openApp(page)
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav() }))
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
  }

  const keepAlivePaused = (page: Parameters<typeof installApiMock>[0]) =>
    page.locator('audio[data-keepalive]').evaluate((element) => (element as HTMLAudioElement).paused)

  test('plays a silent looping element while a track plays', async ({ page }) => {
    await startPlayback(page)
    await expect.poll(() => keepAlivePaused(page)).toBe(false)
    const keepAlive = await page.locator('audio[data-keepalive]').evaluate((element) => ({
      loop: (element as HTMLAudioElement).loop,
      src: (element as HTMLAudioElement).src,
    }))
    expect(keepAlive.loop).toBe(true)
    expect(keepAlive.src).toMatch(/^blob:/)
  })

  test('stays playing through a hidden hand-off, so focus is never given up', async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    })
    await startPlayback(page)
    await expect.poll(() => keepAlivePaused(page)).toBe(false)
    // From here on, any pause of the keepalive is a gap Chrome would see.
    await page.locator('audio[data-keepalive]').evaluate((element) => {
      const w = window as unknown as { __keepAlivePauses: number }
      w.__keepAlivePauses = 0
      element.addEventListener('pause', () => { w.__keepAlivePauses += 1 })
    })

    await page.locator('audio[data-stream-src*="/api/stream/1"]').evaluate((element) => {
      element.dispatchEvent(new Event('ended'))
    })
    await expect(player(page)).toContainText("Don't Stop Me Now")

    expect(await keepAlivePaused(page)).toBe(false)
    expect(await page.evaluate(() => (window as unknown as { __keepAlivePauses: number }).__keepAlivePauses)).toBe(0)
  })

  test('is silenced when playback pauses and resumes with it', async ({ page }) => {
    await startPlayback(page)
    const bar = player(page)
    await expect.poll(() => keepAlivePaused(page)).toBe(false)
    await bar.getByRole('button', { name: 'Pause', exact: true }).click()
    await expect.poll(() => keepAlivePaused(page)).toBe(true)
    await bar.getByRole('button', { name: 'Play', exact: true }).click()
    await expect.poll(() => keepAlivePaused(page)).toBe(false)
  })

  test('tells Chrome the lock-screen position, which two players hide', async ({ page, browserName }) => {
    // Chrome on Android is Chromium. Under WebKit (a spoofed UA that never
    // occurs in practice) the app makes the call but this stub does not see it.
    test.skip(browserName !== 'chromium', 'Chromium-only: models Chrome on Android')
    await page.addInitScript(() => {
      const w = window as unknown as { __positionStates: MediaPositionState[] }
      w.__positionStates = []
      navigator.mediaSession.setPositionState = (state?: MediaPositionState) => {
        if (state) w.__positionStates.push(state)
      }
    })
    await startPlayback(page)
    await expect
      .poll(() => page.evaluate(() => (window as unknown as { __positionStates: MediaPositionState[] }).__positionStates.length))
      .toBeGreaterThan(0)
    const state = await page.evaluate(
      () => (window as unknown as { __positionStates: MediaPositionState[] }).__positionStates[0],
    )
    expect(state.duration).toBeGreaterThan(0)
    expect(state.playbackRate).toBe(1)
    expect(state.position).toBeLessThanOrEqual(state.duration ?? 0)
  })

  // While hidden, a playing video's sound comes from an audio-only HLS
  // rendition. Its fatal error is not swallowed (silence while the app still
  // says it is playing).
  test('says so when a hidden video\'s background audio fails', async ({ page }) => {
    await openApp(page)
    await installFakeHls(page, 5)
    await page.getByRole('button', { name: 'Videos', exact: true }).click()
    await page.locator('.browse-card').filter({ hasText: 'Shows' }).click()
    await page.locator('.video-card').filter({ hasText: 'Episode One' }).click()
    await expect(player(page)).toContainText('Episode One')
    await expect.poll(() => fakeHlsCount(page)).toBe(1)

    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await expect.poll(() => fakeHlsCount(page)).toBe(2)
    expect(await fakeHlsCalls(page, 1, 'loadSource /api/stream/5/audio/index.m3u8')).toBe(1)

    const diagnostic = page.waitForEvent('console', (message) => message.text().includes('bg_audio_hls_fatal'))
    await emitFakeHls(page, 1, 'hlsError', { type: 'otherError', details: 'test', fatal: true })
    await diagnostic
    await expect(page.locator('.toast').filter({ hasText: 'Couldn\'t play "Episode One" in the background' })).toBeVisible()
  })
})

// The keepalive is applied to every mobile browser as a precaution, although
// only Chrome on Android has been measured. Each of these must get it.
const otherMobileBrowsers: Array<{
  name: string
  userAgent: string
  tabletDesktopUa?: boolean
}> = [
  {
    name: 'iPhone Safari',
    userAgent:
      'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
  },
  {
    name: 'Firefox for Android',
    userAgent: 'Mozilla/5.0 (Android 15; Mobile; rv:140.0) Gecko/140.0 Firefox/140.0',
  },
  {
    name: 'Samsung Internet',
    userAgent:
      'Mozilla/5.0 (Linux; Android 15; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/27.0 Chrome/125.0.0.0 Mobile Safari/537.36',
  },
  {
    // iPadOS Safari sends a desktop Mac user agent; only its touch screen and
    // platform give it away.
    name: 'iPadOS Safari (desktop-class user agent)',
    userAgent:
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15',
    tabletDesktopUa: true,
  },
]

for (const browser of otherMobileBrowsers) {
  test.describe(`Media session keepalive on ${browser.name}`, () => {
    test.use({ userAgent: browser.userAgent })

    test('plays a silent looping element with the music and stops with it', async ({ page }) => {
      if (browser.tabletDesktopUa) {
        await page.addInitScript(() => {
          Object.defineProperty(navigator, 'platform', { configurable: true, get: () => 'MacIntel' })
          Object.defineProperty(navigator, 'maxTouchPoints', { configurable: true, get: () => 5 })
        })
      }
      await openApp(page)
      await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
        route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav() }))
      await page.getByRole('button', { name: 'Songs', exact: true }).click()
      await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
      const bar = player(page)
      await expect(bar).toContainText('Bohemian Rhapsody')

      const keepAlive = page.locator('audio[data-keepalive]')
      const paused = () => keepAlive.evaluate((element) => (element as HTMLAudioElement).paused)
      await expect(keepAlive).toHaveCount(1)
      await expect.poll(paused).toBe(false)
      expect(await keepAlive.evaluate((element) => (element as HTMLAudioElement).loop)).toBe(true)

      await bar.getByRole('button', { name: 'Pause', exact: true }).click()
      await expect.poll(paused).toBe(true)
    })
  })
}
