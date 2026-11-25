import type { Page } from 'playwright/test'
import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'
import { installWantListMock } from './wantListMock'
import { player } from './app-helpers'

// The app shell: the desktop sidebar, the top bar, the phone's bottom tab bar
// and its More sheet.

const DEFAULT_TABS = ['Artists', 'Albums', 'Songs', 'Playlists', 'Now playing', 'Radio', 'Videos', 'Files', 'Want list']

async function open(page: Page, hash = '/#/home') {
  await installApiMock(page)
  await installWantListMock(page)
  await page.goto(hash)
  // Admin appears once the account has loaded.
  await expect(page.locator('.app-shell')).toBeVisible()
}

function phoneBar(page: Page) {
  return page.getByRole('navigation', { name: 'Quick tab navigation' })
}

test.describe('desktop sidebar', () => {
  test.use({ viewport: { width: 1280, height: 800 } })

  test('lists Home, the tabs in order, then Admin, and marks the current one', async ({ page }) => {
    await open(page, '/#/albums')
    const sidebar = page.locator('nav.tabs')
    await expect(sidebar.getByRole('button', { name: 'Admin' })).toBeVisible()
    expect(await sidebar.locator('.sidebar-label').allTextContents()).toEqual(['Home', ...DEFAULT_TABS, 'Library info', 'Admin'])
    await expect(sidebar.getByRole('button', { name: 'Albums' })).toHaveAttribute('aria-current', 'page')
    await expect(sidebar.locator('[aria-current]')).toHaveCount(1)
    await sidebar.getByRole('button', { name: 'Radio' }).click()
    await expect(page).toHaveURL(/#\/radio$/)
    await expect(sidebar.getByRole('button', { name: 'Radio' })).toHaveAttribute('aria-current', 'page')
    await expect(sidebar.getByRole('button', { name: 'Albums' })).not.toHaveAttribute('aria-current', 'page')
    // The phone bar is not there at this width.
    await expect(phoneBar(page)).toBeHidden()
  })

  test('shows icons only under 1000px wide, and the buttons keep their names', async ({ page }) => {
    await page.setViewportSize({ width: 800, height: 800 })
    await open(page)
    const sidebar = page.locator('.sidebar')
    expect((await sidebar.boundingBox())!.width).toBeLessThanOrEqual(64.5)
    await expect(page.locator('.sidebar-brand-name')).toBeHidden()
    const songs = page.getByRole('button', { name: 'Songs', exact: true })
    await expect(songs).toHaveAttribute('title', 'Songs')
    const box = (await songs.boundingBox())!
    expect(box.width).toBeGreaterThanOrEqual(43.5)
    expect(box.height).toBeGreaterThanOrEqual(43.5)
    // The label is in the DOM as the name, not on screen.
    const label = (await songs.locator('.sidebar-label').boundingBox())!
    expect(label.width).toBeLessThanOrEqual(1)
    await songs.click()
    await expect(page).toHaveURL(/#\/songs$/)
    await expect(songs).toHaveAttribute('aria-current', 'page')

    await page.setViewportSize({ width: 1100, height: 800 })
    await expect(page.locator('.sidebar-brand-name')).toBeVisible()
    // Polled: the width change is animated.
    await expect.poll(async () => (await sidebar.boundingBox())!.width).toBeGreaterThan(200)
  })
})

test.describe('phone bottom bar', () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test('has Home, the first three tabs and More, and follows the tab order', async ({ page }) => {
    await open(page)
    const bar = phoneBar(page)
    await expect(bar.getByRole('button')).toHaveText(['Home', 'Artists', 'Albums', 'Songs', 'More'])
    await expect(bar.getByRole('button', { name: 'Home' })).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.sidebar')).toBeHidden()
    // Below the player bar, along the bottom edge.
    const barBox = (await bar.boundingBox())!
    const playerBox = (await player(page).boundingBox())!
    expect(barBox.y).toBeGreaterThanOrEqual(playerBox.y + playerBox.height - 1)
    expect(Math.round(barBox.y + barBox.height)).toBe(844)
    for (const button of await bar.getByRole('button').all()) {
      const box = (await button.boundingBox())!
      expect(box.height).toBeGreaterThanOrEqual(43.5)
    }
    await bar.getByRole('button', { name: 'Songs' }).click()
    await expect(page).toHaveURL(/#\/songs$/)
    await expect(bar.getByRole('button', { name: 'Songs' })).toHaveAttribute('aria-current', 'page')

    // A changed order (and a hidden tab) changes the bar.
    await page.evaluate(() => {
      localStorage.setItem(
        'atriolyric.settings',
        JSON.stringify({ tabOrder: ['radio', 'artists', 'playlists', 'songs', 'albums', 'videos', 'files', 'want-list'], hiddenTabs: ['artists'] }),
      )
    })
    await page.reload()
    await expect(bar.getByRole('button')).toHaveText(['Home', 'Radio', 'Playlists', 'Now playing', 'More'])
  })

  test('More lists every other tab, Settings and Admin, and opens them', async ({ page }) => {
    await open(page)
    const more = phoneBar(page).getByRole('button', { name: 'More' })
    await more.click()
    const sheet = page.getByRole('dialog', { name: 'More' })
    await expect(sheet).toBeVisible()
    await expect(sheet.locator('.more-sheet-item')).toHaveText(['Playlists', 'Now playing', 'Radio', 'Videos', 'Files', 'Want list', 'Library info', 'Admin'])
    // Inside the screen, along the bottom.
    const box = (await sheet.boundingBox())!
    expect(box.x).toBeGreaterThanOrEqual(0)
    expect(box.x + box.width).toBeLessThanOrEqual(390.5)
    expect(Math.round(box.y + box.height)).toBe(844)

    // Escape closes it, and focus goes back to More.
    await page.keyboard.press('Escape')
    await expect(sheet).toHaveCount(0)
    await expect(more).toBeFocused()

    await more.click()
    await sheet.getByRole('button', { name: 'Radio' }).click()
    await expect(sheet).toHaveCount(0)
    await expect(page).toHaveURL(/#\/radio$/)
    await expect(page.getByRole('heading', { name: 'Radio', level: 1 })).toBeVisible()
    // The tab under More is current: More is highlighted, no bar tab is.
    await expect(more).toHaveClass(/active/)
    await expect(phoneBar(page).locator('[aria-current]')).toHaveCount(0)
    // One Back press returns to where More was opened (no history entry is
    // left behind by the sheet).
    await page.goBack()
    await expect(page).toHaveURL(/#\/home$/)

    await more.click()
    await page.getByRole('dialog', { name: 'More' }).getByRole('button', { name: 'Admin' }).click()
    await expect(page).toHaveURL(/#\/admin$/)
    await expect(page.getByRole('heading', { name: 'Admin', level: 1 })).toBeVisible()
  })

  test('Back closes the More sheet without leaving the page', async ({ page }) => {
    await open(page, '/#/songs')
    await phoneBar(page).getByRole('button', { name: 'More' }).click()
    await expect(page.getByRole('dialog', { name: 'More' })).toBeVisible()
    await page.goBack()
    await expect(page.getByRole('dialog', { name: 'More' })).toHaveCount(0)
    await expect(page).toHaveURL(/#\/songs$/)
  })
})

test.describe('top bar', () => {
  for (const [label, viewport] of [
    ['desktop', { width: 1280, height: 800 }],
    ['phone', { width: 390, height: 844 }],
  ] as const) {
    test(`keeps one height and title position across tabs (${label})`, async ({ page }) => {
      await page.setViewportSize(viewport)
      await open(page, '/#/songs')
      const measure = async () => {
        const bar = (await page.locator('.top-bar-title').boundingBox())!
        const title = (await page.locator('.top-bar-heading').boundingBox())!
        return { barY: bar.y, barHeight: bar.height, titleX: title.x, titleY: title.y, titleHeight: title.height }
      }
      await expect(page.locator('.top-bar-heading')).toHaveText('Songs')
      const first = await measure()
      for (const hash of ['albums', 'artists', 'radio', 'playlists', 'files', 'want-list']) {
        await page.goto(`/#/${hash}`)
        await expect(page.locator('.top-bar-heading')).not.toHaveText('Songs')
        await expect(page.locator('.top-bar-heading')).toBeVisible()
        expect(await measure(), hash).toEqual(first)
      }
      // Home's search fills the same row.
      await page.goto('/#/home')
      const search = (await page.getByRole('searchbox', { name: 'Search your library' }).boundingBox())!
      expect(search.y).toBeGreaterThanOrEqual(first.barY)
      expect(search.y + search.height).toBeLessThanOrEqual(first.barY + first.barHeight + 0.5)
    })
  }

  test('has exactly one visible h1 on every kind of page', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 })
    await open(page)
    for (const [hash, name] of [
      ['home', 'Home'],
      ['songs', 'Songs'],
      ['radio', 'Radio'],
      ['albums/Queen/A%20Night%20at%20the%20Opera', 'Albums'],
      ['artists/Queen', 'Artists'],
    ] as const) {
      await page.goto(`/#/${hash}`)
      await expect(page.getByRole('heading', { level: 1 })).toHaveText(name)
      await expect(page.locator('h1:visible')).toHaveCount(1)
    }
    // A detail page shows its way back where the title would be.
    await page.goto('/#/albums/Queen/A%20Night%20at%20the%20Opera')
    await page.locator('.top-bar').getByRole('button', { name: 'Albums' }).click()
    await expect(page).toHaveURL(/#\/albums$/)
    await expect(page.locator('.top-bar-heading')).toBeVisible()
  })

  test('the Home search clears from its own button', async ({ page }) => {
    await open(page)
    const box = page.getByRole('searchbox', { name: 'Search your library' })
    await box.fill('queen')
    await expect(page).toHaveURL(/#\/home\/queen$/)
    await page.getByRole('button', { name: 'Clear search' }).click()
    await expect(box).toHaveValue('')
    await expect(box).toBeFocused()
    await expect(page).toHaveURL(/#\/home$/)
  })
})

test.describe('no horizontal overflow', () => {
  for (const width of [1440, 1024, 800, 390, 320]) {
    test(`at ${width}px wide`, async ({ page }) => {
      await page.setViewportSize({ width, height: 800 })
      await open(page)
      for (const hash of ['home', 'songs', 'albums', 'radio', 'albums/Queen/A%20Night%20at%20the%20Opera', 'admin']) {
        await page.goto(`/#/${hash}`)
        await expect(page.locator('.app-main')).toBeVisible()
        await page.waitForTimeout(150)
        const overflow = await page.evaluate(() => {
          const doc = document.documentElement
          const main = document.querySelector('.app-main')!
          const bar = document.querySelector('.top-bar')!
          return {
            page: doc.scrollWidth - doc.clientWidth,
            main: main.scrollWidth - main.clientWidth,
            bar: bar.scrollWidth - bar.clientWidth,
          }
        })
        expect(overflow, `${hash} at ${width}`).toEqual({ page: 0, main: 0, bar: 0 })
      }
    })
  }
})
