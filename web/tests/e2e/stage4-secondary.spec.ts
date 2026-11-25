import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'
import { installWantListMock } from './wantListMock'

// Secondary screens: the search boxes they have (Files, Want list look-up)
// work, and Playlists has no search box of its own - Home's "View full
// search" shows its query as a removable chip.

test.describe('Playlists search chip', () => {
  test.use({ viewport: { width: 1280, height: 900 } })

  test('the Playlists tab has no search box', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/playlists')
    await expect(page.getByRole('heading', { name: 'Your playlists' })).toBeVisible()
    await expect(page.getByRole('searchbox')).toHaveCount(0)
    await expect(page.getByPlaceholder(/Search playlists/)).toHaveCount(0)
    // The create row stays.
    await expect(page.getByRole('textbox', { name: 'New playlist name' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Create', exact: true })).toBeVisible()
  })

  test('View full search opens Playlists filtered, shown as a chip that clears it', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/home/work')
    const results = page.getByRole('region', { name: 'Playlists' })
    await expect(results).toContainText('Workout')
    await results.getByRole('button', { name: /View full/ }).click()
    await expect(page).toHaveURL(/#\/playlists$/)

    const chip = page.getByRole('button', { name: 'Clear search work' })
    await expect(chip).toBeVisible()
    await expect(chip).toContainText('Search: work')
    const cards = page.locator('.browse-card-playlist')
    await expect(cards.filter({ hasText: 'Workout' })).toBeVisible()
    await expect(cards.filter({ hasText: 'Favourites' })).toHaveCount(0)

    await chip.click()
    await expect(chip).toHaveCount(0)
    await expect(cards.filter({ hasText: 'Favourites' })).toBeVisible()
    await expect(cards.filter({ hasText: 'Workout' })).toBeVisible()
  })
})

test.describe('Files search', () => {
  test.use({ viewport: { width: 1280, height: 900 } })

  test('filters by name, and the clear button inside the box clears it', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/files/1')
    const box = page.getByRole('searchbox', { name: 'Search files and folders' })
    await expect(box).toBeVisible()
    await expect(page.getByRole('button', { name: 'Clear file search' })).toHaveCount(0)

    await box.fill('Bohemian')
    await expect(page.locator('.files-search-path')).toHaveText('Disc 1/01 - Bohemian Rhapsody.flac')
    const clear = page.getByRole('button', { name: 'Clear file search' })
    await expect(clear).toBeVisible()
    // The clear button sits inside the search box.
    const boxRect = (await box.boundingBox())!
    const clearRect = (await clear.boundingBox())!
    expect(clearRect.x).toBeGreaterThanOrEqual(boxRect.x)
    expect(clearRect.x + clearRect.width).toBeLessThanOrEqual(boxRect.x + boxRect.width + 1)
    expect(clearRect.y).toBeGreaterThanOrEqual(boxRect.y - 1)
    expect(clearRect.y + clearRect.height).toBeLessThanOrEqual(boxRect.y + boxRect.height + 1)

    await clear.click()
    await expect(box).toHaveValue('')
    await expect(clear).toHaveCount(0)
    await expect(page.locator('.files-search-path')).toHaveCount(0)
    await expect(page.locator('.files-list .files-row-btn').filter({ hasText: 'Disc 1' })).toBeVisible()
  })

  test('sits directly under the top bar', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/files/1')
    const box = page.getByRole('searchbox', { name: 'Search files and folders' })
    await expect(box).toBeVisible()
    const header = (await page.locator('header.top-bar').boundingBox())!
    const boxRect = (await box.boundingBox())!
    // Nothing (the admin note, the breadcrumbs) comes between the two.
    expect(boxRect.y - (header.y + header.height)).toBeLessThan(40)
    const crumbs = (await page.locator('.files-breadcrumb').boundingBox())!
    expect(crumbs.y).toBeGreaterThan(boxRect.y)
  })
})

test.describe('Want list look-up', () => {
  test.use({ viewport: { width: 1280, height: 900 } })

  test('looks things up on MusicBrainz from its own box', async ({ page }) => {
    await installApiMock(page)
    const want = await installWantListMock(page)
    await page.goto('/#/want-list')
    const box = page.getByRole('searchbox', { name: /Search MusicBrainz/ })
    await expect(box).toBeVisible()
    await box.fill('bowie')
    await page.getByRole('button', { name: 'Look up' }).click()
    await expect(page.getByRole('region', { name: 'Artists' })).toContainText('David Bowie')
    // An artist already owned carries the In library badge.
    const queen = page.getByRole('region', { name: 'Artists' }).locator('.want-result').filter({ hasText: 'Queen' })
    await expect(queen.locator('.want-badge')).toHaveText('In library')
    expect(want.searches).toEqual(['bowie'])
    await expect(page).toHaveURL(/#\/want-list\/search\/bowie$/)
  })
})

test.describe('no sideways overflow on the secondary screens', () => {
  for (const width of [1440, 1024, 800, 390, 320]) {
    test(`at ${width}px wide`, async ({ page }) => {
      await page.setViewportSize({ width, height: 844 })
      await installApiMock(page)
      await installWantListMock(page)
      for (const hash of ['/#/playlists', '/#/playlists/4', '/#/radio', '/#/videos', '/#/videos/Shows', '/#/files/1', '/#/want-list', '/#/want-list/search/bowie']) {
        await page.goto(hash)
        await expect(page.locator('.app-main')).toBeVisible()
        await page.waitForTimeout(300)
        const overflow = await page.evaluate(() => {
          const main = document.querySelector('.app-main') as HTMLElement
          return {
            page: document.documentElement.scrollWidth - window.innerWidth,
            main: main.scrollWidth - main.clientWidth,
          }
        })
        expect(overflow.page, `${hash} page`).toBeLessThanOrEqual(1)
        expect(overflow.main, `${hash} main`).toBeLessThanOrEqual(1)
      }
    })
  }
})
