import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'
import { nowPlayingView, player } from './app-helpers'

// Now Playing and the info dialogs load their code on demand. When that code
// cannot be fetched (a deploy replaced the file an open app asks for, or the
// phone is offline), the error must not reach the root boundary, which would
// replace the whole app and stop the music.

test.use({ viewport: { width: 390, height: 844 } })

async function playSomething(page: import('playwright/test').Page) {
  await installApiMock(page)
  await page.route('**/api/client-log', (route) => route.fulfill({ json: { ok: true } }))
  await page.goto('/#/songs')
}

test('Now Playing whose code cannot be loaded closes with a message and the music carries on', async ({ page }) => {
  await playSomething(page)
  await page.route(/\/src\/components\/NowPlayingView\.tsx/, (route) => route.abort())
  await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
  await expect(player(page)).toContainText('Bohemian Rhapsody')

  await player(page).getByRole('button', { name: 'Open now playing' }).click()

  await expect(page.locator('.toast-stack')).toContainText("Couldn't load Now Playing")
  // The app is still the app, with the player and its track.
  await expect(page.getByText('Something went wrong')).toHaveCount(0)
  await expect(player(page)).toContainText('Bohemian Rhapsody')
  await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
  await expect(page.locator('audio').first()).toBeAttached()

  // Now Playing is closed again, not left half open over the app.
  await expect(nowPlayingView(page)).toHaveCount(0)
  await expect(player(page).getByRole('button', { name: 'Open now playing' })).toBeVisible()
})

test('an info dialog whose code cannot be loaded leaves the app and the player alone', async ({ page }) => {
  await playSomething(page)
  await page.route(/\/src\/components\/TrackInfoModal\.tsx/, (route) => route.abort())
  await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
  await expect(player(page)).toContainText('Bohemian Rhapsody')

  await page.getByRole('button', { name: 'More actions for Bohemian Rhapsody' }).click()
  await page.getByRole('menuitem', { name: 'Info', exact: true }).click()

  await expect(page.locator('.toast-stack')).toContainText("Couldn't load that information")
  await expect(page.getByText('Something went wrong')).toHaveCount(0)
  await expect(player(page)).toContainText('Bohemian Rhapsody')
})
