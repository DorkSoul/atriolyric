import type { Page, Route } from 'playwright/test'
import { expect, test } from './fixtures'
import { type AppMockState, installApiMock } from './mockApi'
import { installWantListMock } from './wantListMock'
import { player } from './app-helpers'

// When things load: the first screen does not wait for what it does not
// show, pages opened on their own ask only for what they show, kept tabs do
// not ask again while their data is fresh, and an answer the server is still
// completing (`pending`) is shown at once and asked for again until complete.

const calls = (mock: AppMockState, path: string) => mock.calls.filter((call) => call.path === path)

/** Holds the matching requests until the returned function is called. */
async function hold(page: Page, url: RegExp | string) {
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route(url, async (route: Route) => {
    await gate
    await route.fallback()
  })
  return release
}

/** Waits until the page has been idle long enough for its idle-time work
 * (the version check, the counts) to have run. */
async function afterIdle(page: Page, mock: AppMockState) {
  await expect.poll(() => calls(mock, '/api/library/stats/nav-counts').length).toBe(1)
  await expect.poll(() => mock.calls.filter((call) => call.path === '/api/version').length).toBe(1)
  await page.waitForTimeout(500)
}

test.describe('the first screen', () => {
  test('the shell and a ready Home section show while a slow section is still loading', async ({ page }) => {
    await installApiMock(page)
    const release = await hold(page, /\/api\/library\/mixes$/)
    await page.goto('/#/home')
    await expect(page.locator('.top-bar')).toBeVisible()
    await expect(page.locator('nav.tabs')).toBeVisible()
    const recent = page.getByRole('region', { name: 'Recently played' })
    await expect(recent.locator('.play-source-card, .home-rail-empty').first()).toBeVisible()
    // The slow one holds its place with its heading, busy, not "Loading…"
    // in place of the whole page.
    const mixes = page.getByRole('region', { name: 'Mixes for you' })
    await expect(mixes).toHaveAttribute('aria-busy', 'true')
    await expect(mixes.locator('.home-rail-placeholder')).toBeVisible()
    release()
    await expect(mixes).not.toHaveAttribute('aria-busy', 'true')
    await expect(mixes.locator('.home-rail-placeholder')).toHaveCount(0)
  })

  test('start-up asks for playlists and the version once, and genres only for a tab that has them', async ({ page }) => {
    const mock = await installApiMock(page)
    await page.goto('/#/home')
    await expect(page.getByRole('region', { name: 'Recently played' })).not.toHaveAttribute('aria-busy', 'true')
    await afterIdle(page, mock)
    expect(calls(mock, '/api/playlists')).toHaveLength(1)
    expect(calls(mock, '/api/version')).toHaveLength(1)
    expect(calls(mock, '/api/library/genres')).toHaveLength(0)
    expect(calls(mock, '/api/library/stats')).toHaveLength(0)
    expect(calls(mock, '/api/auth/me')).toHaveLength(1)

    await page.locator('nav.tabs').getByRole('button', { name: 'Songs', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
    await expect.poll(() => calls(mock, '/api/library/genres').length).toBeGreaterThan(0)
  })

  test('the sign-in check starts before the app code has arrived', async ({ page }) => {
    await installApiMock(page)
    let meAt = 0
    let mainServedAt = 0
    page.on('request', (request) => {
      if (new URL(request.url()).pathname === '/api/auth/me' && !meAt) meAt = Date.now()
    })
    await page.route(/\/src\/main\.tsx(\?|$)/, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 1500))
      mainServedAt = Date.now()
      await route.continue()
    })
    await page.goto('/#/home', { waitUntil: 'commit' })
    // A placeholder stands in #root meanwhile, so the page is not blank white.
    await expect(page.locator('#root .boot-shell')).toBeAttached()
    await expect(page.locator('.home-view')).toBeVisible()
    expect(meAt).toBeGreaterThan(0)
    expect(mainServedAt).toBeGreaterThan(0)
    expect(meAt).toBeLessThan(mainServedAt)
  })

  test('a tab whose code is still on its way keeps the shell and shows its loading line', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/home')
    await expect(page.locator('.home-view')).toBeVisible()
    const release = await hold(page, /\/src\/components\/RadioView\.tsx(\?|$)/)
    await page.locator('nav.tabs').getByRole('button', { name: 'Radio', exact: true }).click()
    await expect(page.locator('.top-bar')).toBeVisible()
    await expect(page.locator('.top-bar h1')).toHaveText('Radio')
    await expect(page.locator('nav.tabs')).toBeVisible()
    await expect(page.locator('.app-main .loading')).toBeVisible()
    release()
    await expect(page.getByRole('button', { name: 'Add station' })).toBeVisible()
  })
})

test.describe('pages opened on their own', () => {
  test('an album link asks for that album only, not the album or artist lists', async ({ page }) => {
    const mock = await installApiMock(page)
    await page.goto('/#/albums/Queen/A%20Night%20at%20the%20Opera')
    await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
    await expect.poll(() => calls(mock, '/api/library/album-summaries').length).toBe(1)
    await page.waitForTimeout(800)
    expect(calls(mock, '/api/library/albums')).toHaveLength(0)
    expect(calls(mock, '/api/library/artists')).toHaveLength(0)
    expect(calls(mock, '/api/library/genres')).toHaveLength(0)
    // The header has the album's own details from its summary.
    await expect(page.locator('.collection-detail h2')).toHaveText('A Night at the Opera')

    // Back to the grid loads it then.
    await page.locator('.top-bar').getByRole('button', { name: /^(Back|Albums|Artists|Playlists|Home|Queen)$/ }).click()
    await expect(page.locator('.browse-card-album').first()).toBeVisible()
    expect(calls(mock, '/api/library/albums').length).toBeGreaterThan(0)
  })

  test('the album page never says "not matched" while its details are still coming', async ({ page }) => {
    await installApiMock(page)
    await page.addInitScript(() => localStorage.setItem('atriolyric.showMissing', '1'))
    const release = await hold(page, /\/api\/library\/album-summaries/)
    await page.goto('/#/albums/Queen/A%20Night%20at%20the%20Opera')
    await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
    await expect(page.getByText('Loading the full tracklist…')).toBeVisible()
    await expect(page.getByText(/hasn't been matched on MusicBrainz/)).toHaveCount(0)
    release()
    // The mock album has no MusicBrainz match: now it can say so.
    await expect(page.getByText(/hasn't been matched on MusicBrainz/)).toBeVisible()
  })

  test('an artist link asks for no artist or album list', async ({ page }) => {
    const mock = await installApiMock(page)
    await page.goto('/#/artists/Queen')
    await expect(page.locator('.collection-detail h2')).toHaveText('Queen')
    await expect(page.locator('.browse-card-album').first()).toBeVisible()
    await expect.poll(() => calls(mock, '/api/library/artist-summaries').length).toBe(1)
    await page.waitForTimeout(800)
    expect(calls(mock, '/api/library/artists')).toHaveLength(0)
    // Only the artist's own albums (and appearances), never the whole list.
    const albumCalls = calls(mock, '/api/library/albums')
    expect(albumCalls.length).toBeGreaterThan(0)
    for (const call of albumCalls) expect(new URL(call.url, 'http://x').searchParams.get('artist')).toBe('Queen')
    expect(calls(mock, '/api/library/genres')).toHaveLength(0)
  })
})

test.describe('kept tabs', () => {
  test('shown again soon after, a kept tab asks again only after a reload or a Retry', async ({ page }) => {
    await installApiMock(page)
    let failing = false
    await page.route(/\/api\/radio\/stations$/, (route) =>
      failing && route.request().method() === 'GET'
        ? route.fulfill({ status: 500, json: { detail: 'Something broke on the server.' } })
        : route.fallback(),
    )
    let stationRequests = 0
    page.on('request', (request) => {
      if (new URL(request.url()).pathname === '/api/radio/stations' && request.method() === 'GET') stationRequests += 1
    })
    await page.goto('/#/radio')
    await expect(page.getByRole('button', { name: 'Add station' })).toBeVisible()
    const stations = () => stationRequests
    await expect.poll(stations).toBeGreaterThan(0)
    const before = stations()
    await page.locator('nav.tabs').getByRole('button', { name: 'Songs', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
    await page.locator('nav.tabs').getByRole('button', { name: 'Radio', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Add station' })).toBeVisible()
    await page.waitForTimeout(500)
    expect(stations()).toBe(before)

    // A reload (what pull-to-refresh does) asks again, and so does Retry.
    failing = true
    await page.reload()
    await expect(page.locator('.error-state')).toContainText('Something broke on the server.')
    const afterReload = stations()
    expect(afterReload).toBeGreaterThan(before)
    failing = false
    await page.locator('.error-state').getByRole('button', { name: 'Retry' }).click()
    await expect(page.getByRole('button', { name: 'Add station' })).toBeVisible()
    // Polled: the view can show before the test's request log has the call.
    await expect.poll(stations).toBeGreaterThan(afterReload)
  })

  test('a library change makes a kept tab ask again when it is shown', async ({ page }) => {
    const mock = await installApiMock(page)
    await page.goto('/#/playlists/2')
    await expect(page.locator('.collection-detail h2')).toHaveText('Workout')
    const detail = () => calls(mock, '/api/playlists/2').length
    const before = detail()
    // Adding a song to the playlist from Songs changes it.
    await page.locator('nav.tabs').getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'More actions for Bohemian Rhapsody' }).click()
    await page.getByRole('menuitem', { name: 'Add to playlist' }).click()
    await page.locator('.track-menu-panel').getByRole('menuitem', { name: 'Workout', exact: true }).click()
    await expect(page.getByText(/Added .* to "Workout"\./)).toBeVisible()
    // Back to the playlist, which stayed mounted while Songs showed.
    await page.goBack()
    await expect(page.locator('.collection-detail h2')).toHaveText('Workout')
    await expect.poll(detail).toBeGreaterThan(before)
    // Workout had Bohemian Rhapsody once; the fresh answer has it twice.
    await expect(page.locator('.track-row:visible').filter({ hasText: 'Bohemian Rhapsody' })).toHaveCount(2)
  })
})

test.describe('answers the server is still completing', () => {
  test('lyrics still being looked up, then found: the Lyrics control appears with them', async ({ page }) => {
    const mock = await installApiMock(page)
    mock.pendingAsks['lyrics:1'] = 2
    await page.goto('/#/songs')
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    const overlay = page.locator(':is(.nowplaying-overlay, .nowplaying-page):not(.nowplaying-skeleton)')
    const lyricsButton = overlay.getByRole('button', { name: 'Lyrics', exact: true })
    await expect(lyricsButton).toBeVisible({ timeout: 10_000 })
    expect(calls(mock, '/api/library/media/1/lyrics').length).toBeGreaterThanOrEqual(3)
    await lyricsButton.click()
    await expect(overlay.getByTestId('lyrics-inline')).toContainText('Is this the real life?')
  })

  test('lyrics still being looked up, then none: the dialog says so', async ({ page }) => {
    const mock = await installApiMock(page)
    // Development's StrictMode asks twice on mount, the first answer dropped.
    mock.pendingAsks['lyrics:2'] = 2
    await page.goto('/#/songs')
    await page.getByRole('button', { name: "More actions for Don't Stop Me Now" }).click()
    await page.getByRole('menuitem', { name: 'View lyrics' }).click()
    const dialog = page.getByRole('dialog', { name: "Don't Stop Me Now" })
    await expect(dialog.getByText('Looking for lyrics…')).toBeVisible()
    await expect(dialog.getByText('No lyrics found for this song.')).toBeVisible({ timeout: 10_000 })
    await expect(dialog.getByText('Looking for lyrics…')).toHaveCount(0)
  })

  test('an Info dialog shows what is stored at once and fills in the rest', async ({ page }) => {
    const mock = await installApiMock(page)
    // Development's StrictMode asks twice on mount, the first answer dropped.
    mock.pendingAsks['artist-info:Queen'] = 2
    await page.goto('/#/artists/Queen')
    await page.getByRole('button', { name: 'More actions for Queen', exact: true }).click()
    await page.locator('.track-menu-panel').getByRole('menuitem', { name: 'Info', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: 'Queen' })
    await expect(dialog.getByText('Fetching more…')).toBeVisible()
    await expect(dialog.locator('.info-stats')).toHaveCount(0)
    await expect(dialog.locator('.info-stats')).toBeVisible({ timeout: 10_000 })
    await expect(dialog.getByText('Fetching more…')).toHaveCount(0)
    expect(calls(mock, '/api/library/artist-info').length).toBeGreaterThanOrEqual(3)
  })

  test('the Now Playing stats line waits out a pending answer', async ({ page }) => {
    const mock = await installApiMock(page)
    mock.pendingAsks['track-info:1'] = 2
    await page.goto('/#/songs')
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    await expect(page.locator(':is(.nowplaying-overlay, .nowplaying-page) .nowplaying-stats')).toHaveText(
      '1.2M plays · 340K listeners on ListenBrainz',
      { timeout: 10_000 },
    )
    const infoCalls = mock.calls.filter((call) => call.path === '/api/library/media/1/info')
    expect(infoCalls.length).toBeGreaterThanOrEqual(3)
    // No `light` flag is sent (the server ignores it).
    expect(infoCalls.every((call) => !new URL(call.url, 'http://x').searchParams.has('light'))).toBe(true)
  })

  test('Top tracks holds its place while ListenBrainz is fetching them', async ({ page }) => {
    const mock = await installApiMock(page)
    mock.pendingAsks['top-tracks:Queen'] = 1
    await page.goto('/#/artists/Queen')
    await expect(page.locator('.top-tracks-placeholder')).toBeVisible()
    const section = page.getByRole('region', { name: 'Top tracks' })
    await expect(section.locator('.top-track-row').first()).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('.top-tracks-placeholder')).toHaveCount(0)
  })
})

test.describe('the Want list', () => {
  test('while the server re-matches the list it says so, and reloads the list once done', async ({ page }) => {
    const mock = await installApiMock(page)
    const want = await installWantListMock(page)
    want.entries.push({
      id: 1, kind: 'album', artist: 'David Bowie', title: 'Low', album: null, station_name: null,
      artist_mbid: null, release_group_mbid: null, recording_mbid: null, release_type: 'Album',
      excluded: false, media_item_id: null, owned_count: null, total_count: null, owned: false,
      created_at: '2026-01-01T00:00:00Z',
    })
    want.matching = true
    want.matchStatusAsks = 1
    const artistsCalls: number[] = []
    const statusCalls: number[] = []
    page.on('request', (request) => {
      const path = new URL(request.url()).pathname
      if (path === '/api/want-list/artists') artistsCalls.push(Date.now())
      if (path === '/api/want-list/match-status') statusCalls.push(Date.now())
    })
    await page.goto('/#/want-list')
    await expect(page.getByText('Checking for new information…')).toBeVisible()
    await expect(page.getByRole('button', { name: /David Bowie/ })).toBeVisible()
    const whileMatching = artistsCalls.length
    await expect(page.getByText('Checking for new information…')).toHaveCount(0, { timeout: 10_000 })
    expect(want.matching).toBe(false)
    // Reloaded once the re-match was done.
    expect(artistsCalls.length).toBe(whileMatching + 1)
    // Polled until the server said it was done (one "still going", one "done").
    expect(statusCalls.length).toBe(2)
    expect(artistsCalls[artistsCalls.length - 1]).toBeGreaterThanOrEqual(statusCalls[1])
    expect(mock.unmocked).toEqual([])
  })

  test('a catalog page shown before MusicBrainz has answered fills in by itself', async ({ page }) => {
    await installApiMock(page)
    const want = await installWantListMock(page)
    want.catalogPendingAsks = 2
    await page.goto('/#/want-list/catalog/mb-bowie')
    await expect(page.getByText('Loading the discography from MusicBrainz…')).toBeVisible()
    await expect(page.getByText('MusicBrainz lists no releases for this artist.')).toHaveCount(0)
    await expect(page.getByText('Ziggy Stardust')).toBeVisible({ timeout: 10_000 })
  })

  test('a catalog album shown before MusicBrainz has answered fills in by itself', async ({ page }) => {
    await installApiMock(page)
    const want = await installWantListMock(page)
    want.releaseGroupPendingAsks = 2
    await page.goto('/#/want-list/album/rg-ziggy')
    await expect(page.getByText('Loading the tracklist from MusicBrainz…')).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Ziggy Stardust' })).toBeVisible({ timeout: 10_000 })
    await expect(page.getByText('Starman')).toBeVisible()
  })
})

test.describe('Now Playing', () => {
  test('opens with a placeholder sheet while its code is still on its way', async ({ page }) => {
    await page.setViewportSize({ width: 1100, height: 800 })
    await installApiMock(page)
    const release = await hold(page, /\/src\/components\/NowPlayingView\.tsx(\?|$)/)
    await page.goto('/#/songs')
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    await expect(page.locator('.nowplaying-skeleton')).toBeVisible()
    await expect(page.getByRole('status', { name: 'Opening Now Playing' })).toBeVisible()
    release()
    await expect(page.locator(':is(.nowplaying-overlay, .nowplaying-page) .nowplaying-title').first()).toContainText('Bohemian Rhapsody')
    await expect(page.locator('.nowplaying-skeleton')).toHaveCount(0)
  })
})

test.describe('the viewport', () => {
  const viewportContent = (page: Page) =>
    page.locator('meta[name="viewport"]').getAttribute('content')

  test('a browser tab does not extend under the system bars', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/home')
    await expect(page.locator('.top-bar')).toBeVisible()
    await expect.poll(() => viewportContent(page)).toBe('width=device-width, initial-scale=1.0')
    // No padding for the system bars' insets, which some browsers report anyway.
    expect(await page.locator('html').getAttribute('data-own-window')).toBeNull()
    expect(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--safe-bottom').trim())).toBe('0px')
  })

  test('inside the Android app the page keeps viewport-fit=cover and pads for the system bars', async ({ page }) => {
    await page.addInitScript(() => {
      ;(window as unknown as { Capacitor: object }).Capacitor = {}
    })
    await installApiMock(page)
    await page.goto('/#/home')
    await expect(page.locator('.top-bar')).toBeVisible()
    expect(await viewportContent(page)).toContain('viewport-fit=cover')
    expect(await page.locator('html').getAttribute('data-own-window')).toBe('')
  })

  test('an installed web app pads for the system bars', async ({ page }) => {
    await page.addInitScript(() => {
      const real = window.matchMedia.bind(window)
      window.matchMedia = (query: string) =>
        query.includes('display-mode: standalone') ? ({ ...real(query), matches: true, media: query } as MediaQueryList) : real(query)
    })
    await installApiMock(page)
    await page.goto('/#/home')
    await expect(page.locator('.top-bar')).toBeVisible()
    expect(await page.locator('html').getAttribute('data-own-window')).toBe('')
    expect(await viewportContent(page)).toContain('viewport-fit=cover')
  })
})
