import { nowPlayingView } from './app-helpers'
import type { Page } from 'playwright/test'
import { expect, test } from './fixtures'
import { installApiMock, type AppMockState } from './mockApi'
import { buildStats } from './mock/stats'

// The Library info screen (#/library-info), its entries in the sidebar and the
// phone's More sheet, and the counts the same numbers give the sidebar.

async function open(page: Page, hash = '/#/library-info', setup?: (mock: AppMockState) => void): Promise<AppMockState> {
  const mock = await installApiMock(page)
  setup?.(mock)
  await page.goto(hash)
  await expect(page.locator('.app-shell')).toBeVisible()
  return mock
}

function manyAlbums(count: number) {
  return (stats: ReturnType<typeof buildStats>) => ({
    ...stats,
    recently_added: Array.from({ length: count }, (_, index) => ({
      ...stats.recently_added[0],
      album: `Album ${index + 1}`,
      artist: `Artist ${index + 1}`,
    })),
  })
}

const view = (page: Page) => page.locator('.li-body')

test.describe('desktop', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('shows every section from the stats', async ({ page }) => {
    await open(page)
    await expect(page.getByRole('heading', { name: 'Library info', level: 1 })).toBeVisible()
    const tiles = page.locator('.li-stat')
    await expect(tiles).toHaveCount(6)
    await expect(tiles.nth(0)).toContainText('Albums')
    await expect(tiles.nth(1)).toContainText('Songs')
    await expect(tiles.nth(4)).toContainText('412 GB')
    await expect(tiles.nth(5)).toContainText('51 d')
    await expect(page.locator('.top-bar-count')).toContainText('Last scan')

    const recent = page.getByRole('region', { name: 'Recently added' })
    await expect(recent.getByRole('button')).toHaveCount(3)
    await expect(recent.getByRole('button').first()).toContainText('A Night at the Opera')
    await expect(recent.getByRole('button').first()).toContainText('FLAC 16/44')
    await expect(page.getByRole('button', { name: /^All/ })).toHaveCount(0)

    const contents = page.getByRole('region', { name: "What's in it" })
    await expect(contents).toContainText('FLAC (lossless)')
    await expect(contents).toContainText('With lyrics')
    await expect(contents).toContainText('Loudness measured')
    await expect(contents).toContainText('100%')
    await expect(contents).toContainText('Liked songs')
    await expect(contents).toContainText('Genres')

    const more = page.getByRole('region', { name: 'Listening and make-up' })
    await expect(more).toContainText('Plays this year')
    await expect(more).toContainText('9,480')
    await expect(more).toContainText('Most played artist')
    await expect(more).toContainText('Queen')
    await expect(more.locator('.li-bar-name').filter({ hasText: 'Rock' })).toBeVisible()
    await expect(more.locator('.li-bar-name').filter({ hasText: '1990s' })).toBeVisible()
    await expect(more.locator('.li-bar-name').filter({ hasText: 'Unknown' })).toBeVisible()
    // Newest decade first, Unknown last (the server sends them oldest first).
    const decades = more.locator('.li-card').filter({ hasText: 'Songs by decade' }).locator('.li-bar-name')
    await expect(decades).toHaveText(['1990s', '1970s', 'Unknown'])
    await expect(more).toContainText('Average MP3 bitrate')
    await expect(more).toContainText('287 kbps')
    await expect(more).toContainText('Added this year')
    await expect(more).toContainText('1.3 TB of 4.0 TB')
    await expect(more).not.toContainText('based on stored history')
    await expect(more).not.toContainText('Audio details are known')
  })

  test('opens from the sidebar and marks it current', async ({ page }) => {
    await open(page, '/#/home')
    const item = page.locator('nav.tabs').getByRole('button', { name: 'Library info' })
    await item.click()
    await expect(page).toHaveURL(/#\/library-info$/)
    await expect(item).toHaveAttribute('aria-current', 'page')
    await expect(page.getByRole('region', { name: "What's in it" })).toBeVisible()
  })

  test('#/recent-added redirects to Library info without an extra history entry', async ({ page }) => {
    await open(page, '/#/home')
    await page.evaluate(() => {
      window.location.hash = '#/recent-added'
    })
    await expect(page).toHaveURL(/#\/library-info$/)
    await expect(page.getByRole('heading', { name: 'Library info', level: 1 })).toBeVisible()
    await page.goBack()
    await expect(page).toHaveURL(/#\/home$/)

    await page.goto('/#/recent-added')
    await expect(page).toHaveURL(/#\/library-info$/)
  })

  test('a recently added row opens its album', async ({ page }) => {
    await open(page)
    await page.getByRole('region', { name: 'Recently added' }).getByRole('button', { name: /A Night at the Opera/ }).click()
    await expect(page).toHaveURL(/#\/albums\/Queen\/A%20Night%20at%20the%20Opera$/)
  })

  test('All N expands the list past the first seven rows', async ({ page }) => {
    await open(page, '/#/library-info', (mock) => {
      mock.libraryStatsPatch = manyAlbums(12)
    })
    const rows = page.getByRole('region', { name: 'Recently added' }).locator('ul button')
    await expect(rows).toHaveCount(7)
    const all = page.getByRole('button', { name: 'All 12' })
    await all.click()
    await expect(rows).toHaveCount(12)
    await expect(page.getByRole('button', { name: 'Show fewer' })).toBeVisible()
    await page.getByRole('button', { name: 'Show fewer' }).click()
    await expect(rows).toHaveCount(7)
  })

  test('a failed load shows Retry, and Retry recovers', async ({ page }) => {
    const mock = await open(page, '/#/home')
    await expect(page.locator('.sidebar-count').first()).toBeVisible()
    mock.libraryStatsFailures = 99
    await page.locator('nav.tabs').getByRole('button', { name: 'Library info' }).click()
    await expect(page.getByRole('alert')).toContainText('Could not read the library stats.')
    mock.libraryStatsFailures = 0
    await page.getByRole('alert').getByRole('button', { name: 'Retry' }).click()
    await expect(page.getByRole('region', { name: "What's in it" })).toBeVisible()
    await expect(page.getByRole('alert')).toHaveCount(0)
  })

  test('says when audio details are only partly known, and when history is capped', async ({ page }) => {
    await open(page, '/#/library-info', (mock) => {
      mock.libraryStatsPatch = (stats) => ({
        ...stats,
        quality: { ...stats.quality, quality_scanned: 2, quality_total: 3 },
        listening: { ...stats.listening, listening_capped: true },
      })
    })
    await expect(page.getByText('Audio details are known for 2 of 3 songs. Run a scan from Admin to fill in the rest.')).toBeVisible()
    await expect(page.getByText('based on stored history')).toBeVisible()
  })

  test('an empty library says so', async ({ page }) => {
    await open(page, '/#/library-info', (mock) => {
      mock.libraryStatsPatch = (stats) => ({
        ...stats,
        totals: { ...stats.totals, albums: 0, songs: 0, videos: 0, artists: 0 },
      })
    })
    await expect(page.getByText(/Your library is empty/)).toBeVisible()
  })

  test('the sidebar and the top bar show counts', async ({ page }) => {
    await open(page, '/#/songs')
    const sidebar = page.locator('nav.tabs')
    await expect(sidebar.getByRole('button', { name: 'Songs', exact: true }).locator('.sidebar-count')).toHaveText('3')
    await expect(sidebar.getByRole('button', { name: 'Albums', exact: true }).locator('.sidebar-count')).toHaveText('3')
    await expect(sidebar.getByRole('button', { name: 'Videos', exact: true }).locator('.sidebar-count')).toHaveText('2')
    await expect(page.locator('.top-bar-count')).toHaveText('3')
  })

  test('counts are asked for once, not on every tab change, and without the full stats', async ({ page }) => {
    const mock = await open(page, '/#/home')
    await expect(page.locator('.sidebar-count').first()).toBeVisible()
    for (const name of ['Songs', 'Albums', 'Artists']) {
      await page.locator('nav.tabs').getByRole('button', { name, exact: true }).click()
    }
    expect(mock.calls.filter((call) => call.path === '/api/library/stats/nav-counts')).toHaveLength(1)
    // The heavy stats are Library info's alone.
    expect(mock.calls.filter((call) => call.path === '/api/library/stats')).toHaveLength(0)
  })

  test('Library info hands its fresher counts to the sidebar', async ({ page }) => {
    const mock = await open(page, '/#/home')
    const songs = page.locator('nav.tabs').getByRole('button', { name: 'Songs', exact: true }).locator('.sidebar-count')
    await expect(songs).toHaveText('3')
    mock.libraryStatsPatch = (stats) => ({ ...stats, nav_counts: { ...stats.nav_counts, songs: 1234 } })
    await page.locator('nav.tabs').getByRole('button', { name: 'Library info', exact: true }).click()
    await expect(songs).toHaveText('1,234')
    expect(mock.calls.filter((call) => call.path === '/api/library/stats/nav-counts')).toHaveLength(1)
  })

  test('a sidebar button is named by its tab alone and described by its count', async ({ page }) => {
    await open(page, '/#/home')
    const songs = page.locator('nav.tabs').getByRole('button', { name: 'Songs', exact: true })
    await expect(songs.locator('.sidebar-count')).toHaveText('3')
    await expect(songs).toHaveAccessibleName('Songs')
    await expect(songs).toHaveAccessibleDescription('3')
  })

  test('the shell works without counts when the stats request fails', async ({ page }) => {
    const mock = await open(page, '/#/home', (state) => {
      state.navCountsFailures = 99
    })
    await page.locator('nav.tabs').getByRole('button', { name: 'Songs', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
    await expect.poll(() => mock.calls.some((call) => call.path === '/api/library/stats/nav-counts')).toBe(true)
    await expect(page.locator('.sidebar-count')).toHaveCount(0)
    await expect(page.getByRole('alert')).toHaveCount(0)
  })

  test('the page is fully shown while free space is still being checked', async ({ page }) => {
    const mock = await open(page, '/#/library-info', (state) => {
      state.libraryStorage = 'hold'
    })
    await expect(page.getByRole('region', { name: "What's in it" })).toBeVisible()
    await expect(page.getByRole('region', { name: 'Recently added' }).getByRole('button').first()).toBeVisible()
    const growth = page.locator('.li-card').filter({ hasText: 'Growth and storage' })
    await expect(growth).toContainText('Added this year')
    await expect(growth).toContainText('checking…')
    await expect.poll(() => mock.releaseStorage !== undefined).toBe(true)
    mock.releaseStorage!()
    await expect(growth).toContainText('1.3 TB of 4.0 TB')
    await expect(growth).not.toContainText('checking…')
  })

  test('a failed free-space check leaves the rest of the page alone', async ({ page }) => {
    await open(page, '/#/library-info', (state) => {
      state.libraryStorage = 'fail'
    })
    const growth = page.locator('.li-card').filter({ hasText: 'Growth and storage' })
    await expect(growth).toContainText('Free space left')
    await expect(growth).toContainText('unknown')
    await expect(growth).toContainText('Added this year')
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.getByRole('region', { name: "What's in it" })).toBeVisible()
  })

  test('a folder with no free-space reading says unknown', async ({ page }) => {
    await open(page, '/#/library-info', (state) => {
      state.libraryStorage = [{ roots: ['Music'], free_bytes: null, total_bytes: null }]
    })
    await expect(page.locator('.li-card').filter({ hasText: 'Growth and storage' })).toContainText('unknown')
  })
})

test.describe('phone', () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test('opens from the More sheet, which lists it before Settings', async ({ page }) => {
    await open(page, '/#/home')
    await page.getByRole('navigation', { name: 'Quick tab navigation' }).getByRole('button', { name: 'More' }).click()
    const sheet = page.getByRole('dialog', { name: 'More' })
    const items = await sheet.locator('.more-sheet-item').allTextContents()
    expect(items).not.toContain('Settings')
    expect(items.indexOf('Library info')).toBe(items.indexOf('Admin') - 1)
    await sheet.getByRole('button', { name: 'Library info' }).click()
    await expect(page).toHaveURL(/#\/library-info$/)
    await expect(page.getByRole('region', { name: 'Recently added' })).toBeVisible()
  })

  test('recently added rows are compact and open the album', async ({ page }) => {
    await open(page)
    const row = page.getByRole('region', { name: 'Recently added' }).getByRole('button', { name: /Hurry Up/ })
    await expect(row.locator('.li-c-year')).toBeHidden()
    await expect(row.locator('.li-c-artist')).toBeVisible()
    await row.click()
    await expect(page).toHaveURL(/#\/albums\/M83\/Hurry/)
  })
})

test.describe('layout', () => {
  for (const width of [1440, 1024, 800, 390, 320]) {
    test(`has no horizontal overflow at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 })
      await open(page, '/#/library-info', (mock) => {
        mock.libraryStatsPatch = manyAlbums(12)
      })
      await expect(view(page)).toBeVisible()
      await expect(page.getByRole('region', { name: 'Recently added' }).locator('ul button').first()).toBeVisible()
      await page.getByRole('button', { name: 'All 12' }).click()
      await expect(page.locator('.li-bar-name').first()).toBeVisible()
      const overflow = await page.evaluate(() => {
        const main = document.querySelector('main.app-main')!
        return {
          page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          main: main.scrollWidth - main.clientWidth,
        }
      })
      expect(overflow.page).toBeLessThanOrEqual(0)
      expect(overflow.main).toBeLessThanOrEqual(0)
    })
  }

  test('Recently added and What\'s in it sit side by side only on a wide page', async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 900 })
    await open(page)
    const recent = (await page.getByRole('region', { name: 'Recently added' }).boundingBox())!
    const contents = (await page.getByRole('region', { name: "What's in it" }).boundingBox())!
    expect(contents.x).toBeGreaterThan(recent.x + recent.width - 1)
    await page.setViewportSize({ width: 1100, height: 900 })
    await expect(async () => {
      const r = (await page.getByRole('region', { name: 'Recently added' }).boundingBox())!
      const c = (await page.getByRole('region', { name: "What's in it" }).boundingBox())!
      expect(c.y).toBeGreaterThan(r.y + r.height - 1)
    }).toPass()
  })
})

test('the song Info dialog shows its format', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 })
  await open(page, '/#/songs', (mock) => {
    mock.tracks[0].format_label = 'FLAC 16/44'
  })
  const row = page.locator('.track-row').filter({ hasText: 'Bohemian Rhapsody' })
  await row.getByRole('button', { name: 'More actions' }).click()
  await page.locator('.track-menu-panel').getByRole('menuitem', { name: 'Info', exact: true }).click()
  const dialog = nowPlayingView(page)
  await expect(dialog).toContainText('Format')
  await expect(dialog).toContainText('FLAC 16/44')
})
