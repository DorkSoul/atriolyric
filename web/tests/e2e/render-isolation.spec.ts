import { type Page } from 'playwright/test'
import { expect, test } from './fixtures'
import { installApiMock, pushPlaybackState, type AppMockState } from './mockApi'
import { openVideoNowPlaying, player } from './app-helpers'

// The player's position changes several times a second while music plays.
// Only the player may re-render for it: the views under <main> (the Songs
// list, a playlist's rows, Home) must not. `window.__renderCommits` is
// filled by the dev-only RenderProbe around the views and the player bar.

async function settle(page: Page): Promise<void> {
  await page.evaluate(
    () => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))),
  )
}

const commits = (page: Page) =>
  page.evaluate(() => {
    const counts = (window as Window & { __renderCommits?: Record<string, number> }).__renderCommits ?? {}
    return { views: counts.views ?? 0, player: counts.player ?? 0 }
  })

async function tick(page: Page, seconds: number) {
  await page.locator('[data-player-element="main"]').evaluate((element, to) => {
    const media = element as HTMLMediaElement
    media.currentTime = to
    media.dispatchEvent(new Event('timeupdate'))
  }, seconds)
  await expect(player(page).getByRole('slider', { name: 'Seek' })).toHaveAttribute('aria-valuenow', String(seconds))
  await settle(page)
}

async function startOnSongs(page: Page): Promise<AppMockState> {
  // Each media element's `canplay`, for quietUntilLoaded.
  await page.addInitScript(() => {
    const w = window as Window & { __canPlay?: string[] }
    w.__canPlay = []
    window.addEventListener('canplay', (event) => {
      const media = event.target
      if (media instanceof HTMLMediaElement) w.__canPlay!.push(`${media.dataset.playerElement}:${media.dataset.streamSrc}`)
    }, true)
  })
  const mock = await installApiMock(page)
  await page.goto('/#/songs')
  const row = page.locator('.track-row').filter({ hasText: 'Bohemian Rhapsody' })
  await row.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
  await expect(player(page)).toContainText('Bohemian Rhapsody')
  await expect(player(page).getByRole('button', { name: 'Pause' })).toBeVisible()
  await quietUntilLoaded(page)
  return mock
}

// Starting a track sets off work that lands a little later and re-renders the
// views once each, as it should: the stream's load (`loadstart` shows the
// loading state, `canplay` clears it - the player's state changes twice),
// art held back while the track started, rows moving into view as the player
// bar appears. Two frames after the click are not always enough for that on
// a loaded machine (WebKit's media load starts on its own schedule), and its
// commits would be counted as the ticks'. Measure only once the track has loaded,
// no art is pending and the views have not committed for a while.
async function quietUntilLoaded(page: Page): Promise<void> {
  await expect.poll(() => page.evaluate(() => ((window as Window & { __canPlay?: string[] }).__canPlay ?? [])
    .some((entry) => /^main:\/api\/stream\/1(\?|$)/.test(entry)))).toBe(true)
  await expect(page.locator('main.app-main .art-spinner')).toHaveCount(0)
  let last = -1
  let since = Date.now()
  await expect.poll(async () => {
    const { views } = await commits(page)
    if (views !== last) {
      last = views
      since = Date.now()
    }
    return Date.now() - since
  }, { intervals: [100], timeout: 10_000 }).toBeGreaterThanOrEqual(500)
}

test.describe('position ticks', () => {
  test('re-render the player bar but not the views', async ({ page }) => {
    const mock = await startOnSongs(page)
    const before = await commits(page)

    for (let second = 1; second <= 8; second++) await tick(page, second)
    // The active device's periodic position save comes back as a broadcast
    // that changes nothing but the position.
    mock.playback.position_seconds = 8
    pushPlaybackState(mock, { position_changed: true, source_device_id: 1, source_command: 'position_report' })
    await tick(page, 9)

    const after = await commits(page)
    expect(after.player - before.player, 'player bar commits').toBeGreaterThanOrEqual(9)
    expect(after.views - before.views, 'view commits during playback ticks').toBe(0)
  })
})

// How many times one probed subtree or component has committed (see
// RenderProbe and useRenderProbe).
const commitsOf = (page: Page, id: string) =>
  page.evaluate(
    (key) => (window as Window & { __renderCommits?: Record<string, number> }).__renderCommits?.[key] ?? 0,
    id,
  )

/** Waits until `id` has not committed for half a second. */
async function quiet(page: Page, id: string): Promise<void> {
  let last = -1
  let since = Date.now()
  await expect.poll(async () => {
    const count = await commitsOf(page, id)
    if (count !== last) {
      last = count
      since = Date.now()
    }
    return Date.now() - since
  }, { intervals: [100], timeout: 10_000 }).toBeGreaterThanOrEqual(500)
}

test.describe('position ticks in Now Playing', () => {
  test('re-render its seek row, not the rest of the view or the queue', async ({ page }) => {
    // Below the side panel's 1200px, so Now Playing carries Up next itself.
    await page.setViewportSize({ width: 1100, height: 800 })
    await startOnSongs(page)
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    const view = page.getByRole('region', { name: 'Now playing', exact: true })
    await expect(view.locator('.queue-row').first()).toBeVisible()
    const seek = view.getByRole('slider', { name: 'Seek' })
    await expect(seek).toBeVisible()
    await quiet(page, 'nowplaying')
    await quiet(page, 'queue')
    const before = { view: await commitsOf(page, 'nowplaying'), queue: await commitsOf(page, 'queue') }
    expect(before.view, 'Now Playing is probed').toBeGreaterThan(0)
    expect(before.queue, 'the queue is probed').toBeGreaterThan(0)

    for (let second = 1; second <= 8; second++) {
      await tick(page, second)
      // The seek row itself follows every tick.
      await expect(seek).toHaveValue(String(second))
    }
    await expect(view.locator('.nowplaying-seek .player-time').first()).toHaveText('0:08')

    expect(await commitsOf(page, 'nowplaying') - before.view, 'Now Playing renders during ticks').toBe(0)
    expect(await commitsOf(page, 'queue') - before.queue, 'queue renders during ticks').toBe(0)
  })

  test('the video skip buttons read the position when pressed', async ({ page }) => {
    await page.setViewportSize({ width: 1100, height: 800 })
    const mock = await installApiMock(page)
    await page.goto('/#/home')
    await openVideoNowPlaying(page)
    await page.locator('[data-player-element="video"]').evaluate((element) => {
      const media = element as HTMLMediaElement
      Object.defineProperty(media, 'currentTime', { configurable: true, writable: true, value: 100 })
      media.dispatchEvent(new Event('timeupdate'))
    })
    await page.getByRole('button', { name: 'Forward 30 seconds' }).click()
    await expect.poll(() => mock.wsMessages.filter((message) => message.type === 'seek').at(-1)?.position_seconds).toBe(130)
  })
})

test.describe('volume changes', () => {
  test('re-render the player but not a list of songs', async ({ page }) => {
    await startOnSongs(page)
    const volume = player(page).getByRole('slider', { name: 'Volume' })
    await expect(volume).toBeVisible()
    // Focus leaving the list is not what is measured.
    await volume.focus()
    await quiet(page, 'views')
    const before = await commits(page)

    // A drag: one change per step.
    for (const step of ['0.9', '0.8', '0.7', '0.6', '0.5']) {
      await volume.fill(step)
      await expect(volume).toHaveValue(step)
      await settle(page)
    }
    await expect
      .poll(() => page.locator('[data-player-element="main"]').evaluate((element) => (element as HTMLMediaElement).volume))
      .toBeCloseTo(0.5, 2)

    const after = await commits(page)
    expect(after.player - before.player, 'player commits').toBeGreaterThanOrEqual(5)
    expect(after.views - before.views, 'view commits during a volume drag').toBe(0)
  })
})

test.describe('player changes', () => {
  test('pausing and playing re-render the player and the page title, not the views', async ({ page }) => {
    await installApiMock(page)
    // Started from an album page (not kept mounted), so no view showing or
    // kept is a list of songs that marks the playing one.
    await page.goto('/#/albums/Queen/A%20Night%20at%20the%20Opera')
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page).getByRole('button', { name: 'Pause' })).toBeVisible()
    await page.locator('nav.tabs').getByRole('button', { name: 'Home', exact: true }).click()
    await expect(page.getByRole('region', { name: 'Recently played' })).not.toHaveAttribute('aria-busy', 'true')
    await expect(page).toHaveTitle(/^▶ Bohemian Rhapsody/)
    let last = -1
    let since = Date.now()
    await expect.poll(async () => {
      const { views } = await commits(page)
      if (views !== last) {
        last = views
        since = Date.now()
      }
      return Date.now() - since
    }, { intervals: [100], timeout: 10_000 }).toBeGreaterThanOrEqual(500)
    const before = await commits(page)

    await player(page).getByRole('button', { name: 'Pause' }).click()
    await expect(page).toHaveTitle('Home – Atriolyric')
    await player(page).getByRole('button', { name: 'Play', exact: true }).click()
    await expect(page).toHaveTitle(/^▶ Bohemian Rhapsody/)
    await settle(page)

    const after = await commits(page)
    expect(after.player - before.player, 'player commits').toBeGreaterThanOrEqual(2)
    expect(after.views - before.views, 'view commits for a pause and a play').toBe(0)
  })
})

test.describe('long playlist', () => {
  test('renders a bounded window of a 5,000-track playlist and scrolls to the last one', async ({ page }) => {
    const mock = await installApiMock(page)
    const base = mock.tracks[0]
    const items = Array.from({ length: 5_000 }, (_, i) => ({
      ...base,
      id: 100_000 + i,
      title: `Long Track ${i + 1}`,
      playlist_item_id: 900_000 + i,
    }))
    mock.playlists.push({
      id: 77,
      name: 'Very Long',
      created_at: '2026-01-05T00:00:00Z',
      item_count: items.length,
      is_auto_generated: false,
      auto_kind: null,
      preview_media_item_ids: items.slice(0, 4).map((item) => item.id),
    })
    mock.playlistDetails[77] = { id: 77, name: 'Very Long', created_at: '2026-01-05T00:00:00Z', items }

    await page.goto('/#/playlists/77')
    await expect(page.locator('.track-row').filter({ hasText: 'Long Track 1' }).first()).toBeVisible()
    const rows = page.locator('.track-list > .track-row')
    expect(await rows.count()).toBeLessThan(200)

    const main = page.locator('main.app-main')
    await main.evaluate((el) => { el.scrollTop = el.scrollHeight })
    const last = page.locator('.track-row').filter({ hasText: 'Long Track 5000' })
    await expect(last).toBeVisible()
    expect(await rows.count()).toBeLessThan(200)

    // Away to another tab and back: the playlist comes back where it was left.
    const scrolled = await main.evaluate((el) => el.scrollTop)
    await page.getByRole('button', { name: 'Songs', exact: true }).first().click()
    await expect(page.locator('.track-row').filter({ hasText: 'Bohemian Rhapsody' })).toBeVisible()
    await page.goBack()
    await expect(last).toBeVisible()
    expect(Math.abs((await main.evaluate((el) => el.scrollTop)) - scrolled)).toBeLessThan(2)
  })
})
