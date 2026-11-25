import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'
import { installWantListMock } from './wantListMock'

// No screen scrolls sideways at any of the widths checked here:
// neither the page nor the scrolling <main>.

const SCREENS = [
  '/#/home',
  '/#/home/queen',
  '/#/artists',
  '/#/artists/Queen',
  '/#/albums',
  '/#/albums/Queen/A%20Night%20at%20the%20Opera',
  '/#/songs',
  '/#/playlists',
  '/#/playlists/1',
  '/#/radio',
  '/#/videos',
  '/#/files/1',
  '/#/want-list',
  '/#/recent-played',
  '/#/library-info',
]

for (const width of [1440, 1024, 800, 390, 320]) {
  test(`no horizontal overflow at ${width}px`, async ({ page }) => {
    test.setTimeout(90_000)
    await page.setViewportSize({ width, height: width < 640 ? 800 : 900 })
    await installApiMock(page)
    await installWantListMock(page)
    for (const hash of SCREENS) {
      await page.goto(hash)
      await expect(page.locator('.app-main')).toBeVisible()
      // Let the view fill in (lists, art, lazy files).
      await page.waitForLoadState('networkidle')
      const sizes = await page.evaluate(() => {
        const main = document.querySelector('.app-main') as HTMLElement
        return {
          page: document.documentElement.scrollWidth - window.innerWidth,
          main: main.scrollWidth - main.clientWidth,
        }
      })
      expect(sizes.page, `${hash}: the page is wider than the window`).toBeLessThanOrEqual(1)
      expect(sizes.main, `${hash}: the page content is wider than its column`).toBeLessThanOrEqual(1)
    }
  })
}
