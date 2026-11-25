import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'
import { player, nowPlayingView } from './app-helpers'

// Now Playing's "Playing from" link: names what the queue was started from
// and opens it, however far the user has wandered since.

for (const viewport of [
  { name: 'phone', width: 390, height: 844 },
  { name: 'desktop', width: 1440, height: 900 },
]) {
  test.describe(`Playing from (${viewport.name})`, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } })

    test('names the mix the queue started from and opens it from another tab', async ({ page }) => {
      await installApiMock(page)
      await page.goto('/#/playlists')
      await page.locator('.browse-card-main').filter({ hasText: 'Rock Mix' }).click()
      await expect(page.getByRole('heading', { name: 'Rock Mix', exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Play', exact: true }).click()
      await expect(player(page)).toContainText('Bohemian Rhapsody')

      // Wander off somewhere else, then open Now Playing.
      await page.goto('/#/songs')
      await player(page).getByRole('button', { name: 'Open now playing' }).click()
      const view = nowPlayingView(page)
      await expect(view).toBeVisible()

      const origin = view.getByRole('button', { name: /^Playing from mix Rock Mix/ })
      await expect(origin).toContainText('Playing from mix')
      await expect(origin).toContainText('Rock Mix')
      // It sits above the artwork.
      const originBox = await origin.boundingBox()
      const artBox = await view.locator('.art-nowplaying').boundingBox()
      expect(originBox!.y + originBox!.height).toBeLessThanOrEqual(artBox!.y + 1)

      await origin.click()
      await expect(view).toHaveCount(0)
      await expect(page.getByRole('heading', { name: 'Rock Mix', exact: true })).toBeVisible()
    })

    test('names the Songs tab when a song there started the queue', async ({ page }) => {
      await installApiMock(page)
      await page.goto('/#/songs')
      await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
      await player(page).getByRole('button', { name: 'Open now playing' }).click()
      const view = nowPlayingView(page)
      await expect(view).toBeVisible()
      await expect(view.getByRole('button', { name: /^Playing from songs All Songs/ })).toBeVisible()
    })
  })
}
