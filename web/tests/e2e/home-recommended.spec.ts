// The Home "Recommended from your library" rail shows the Discovery playlist,
// picked by its auto kind - not whichever auto-generated playlist (Favourites,
// a genre mix, Song Mix) happens to come first in the playlist list.

import { expect, test } from './fixtures'
import { openApp, sawCall } from './app-helpers'
import { installApiMock } from './mockApi'

test.describe('Home recommended rail', () => {
  test('shows the Discovery playlist even when another auto playlist comes first', async ({ page }) => {
    const mock = await openApp(page)
    // The mocked list starts with Favourites (auto-generated, id 1), then
    // Workout, then Discovery (id 3) - the order a real server can return.
    expect(mock.playlists[0].auto_kind).toBe('favorites')

    const rail = page.locator('.home-rail').filter({ hasText: 'Recommended from your library' })
    const titles = rail.locator('.rail-card-title')
    await expect(titles).toHaveText(["Don't Stop Me Now", 'Midnight City'])
    expect(sawCall(mock, '/api/playlists/3')).toBe(true)
    expect(sawCall(mock, '/api/playlists/1')).toBe(false)

    // Regenerating fetches the Discovery playlist again, not Favourites.
    const discoveryFetches = () => mock.calls.filter((call) => call.path === '/api/playlists/3').length
    const before = discoveryFetches()
    await page.getByRole('button', { name: 'Regenerate Recommended from your library' }).click()
    await expect.poll(() => sawCall(mock, '/api/library/discovery/refresh', 'POST')).toBe(true)
    await expect.poll(discoveryFetches).toBeGreaterThan(before)
    await expect(titles).toHaveText(["Don't Stop Me Now", 'Midnight City'])
    expect(sawCall(mock, '/api/playlists/1')).toBe(false)
  })
})

test.describe('Home says what failed', () => {
  const rail = (page: Parameters<typeof installApiMock>[0], title: string) => page.locator('.home-rail').filter({ hasText: title })

  test('a section that could not load shows an error with Retry, not its "nothing yet" text; Retry reloads only it', async ({ page }) => {
    const mock = await installApiMock(page)
    let broken = true
    await page.route(/\/api\/library\/recently-played-sources/, (route) =>
      broken ? route.fulfill({ status: 500, json: { detail: 'Something broke on the server.' } }) : route.fallback(),
    )
    await page.goto('/#/home')
    const recent = rail(page, 'Recently played')
    await expect(recent.locator('.error-state')).toContainText('Something broke on the server.')
    await expect(recent.locator('.error-state')).toHaveAttribute('role', 'alert')
    await expect(recent).not.toContainText('will show up here')
    // The other sections are there as usual.
    await expect(rail(page, 'Mixes for you').locator('.mix-card').first()).toBeVisible()
    await expect(page.locator('.home-view > .error-state')).toHaveCount(0)

    const requests = (path: string) => mock.calls.filter((call) => call.path === path).length
    const mixesBefore = requests('/api/library/mixes')
    const stationsBefore = requests('/api/radio/stations')
    broken = false
    await recent.getByRole('button', { name: 'Retry' }).click()
    await expect(recent.locator('.play-source-card').first()).toBeVisible()
    await expect(recent.locator('.error-state')).toHaveCount(0)
    expect(requests('/api/library/mixes')).toBe(mixesBefore)
    expect(requests('/api/radio/stations')).toBe(stationsBefore)
  })

  test('a retry that fails again keeps the error on that section', async ({ page }) => {
    await installApiMock(page)
    let hits = 0
    await page.route(/\/api\/library\/mixes$/, (route) => {
      hits += 1
      return route.fulfill({ status: 500, json: { detail: 'Something broke on the server.' } })
    })
    await page.goto('/#/home')
    const mixes = rail(page, 'Mixes for you')
    await expect(mixes.locator('.error-state')).toBeVisible()
    const before = hits
    await mixes.getByRole('button', { name: 'Retry' }).click()
    await expect.poll(() => hits).toBeGreaterThan(before)
    await expect(mixes.locator('.error-state')).toBeVisible()
    await expect(rail(page, 'Recently played').locator('.play-source-card').first()).toBeVisible()
  })

  test('Regenerate mixes says so when it fails, and can be pressed again', async ({ page }) => {
    await openApp(page)
    let refuse = true
    let posts = 0
    await page.route(/\/api\/library\/mixes\/refresh$/, (route) => {
      posts += 1
      return refuse ? route.fulfill({ status: 500, json: { detail: 'The mixes could not be rebuilt.' } }) : route.fallback()
    })
    const regenerate = page.getByRole('button', { name: 'Regenerate Mixes for you' })
    await regenerate.click()
    await expect(page.locator('.toast-error')).toContainText('The mixes could not be rebuilt.')
    await expect(regenerate).toBeEnabled()
    // The mixes that were there stay.
    await expect(rail(page, 'Mixes for you').locator('.mix-card').first()).toBeVisible()
    refuse = false
    await regenerate.click()
    await expect.poll(() => posts).toBe(2)
    await expect(regenerate).toBeEnabled()
  })
})
