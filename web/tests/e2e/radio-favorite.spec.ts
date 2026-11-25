import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'

// A station's heart uses the same optimistic change as the song, album and
// artist hearts (useOptimisticChange): it turns at once, a refused save turns
// it back, and it cannot be pressed again while the save is out.

test('a station heart turns at once and back when the save is refused', async ({ page }) => {
  const mock = await installApiMock(page)
  // Each favourite request waits for its own release; `refuse` fails it.
  let release = () => {}
  let refuse = false
  let arrived = 0
  await page.route(/\/api\/radio\/stations\/11\/favorite$/, async (route) => {
    await new Promise<void>((resolve) => {
      release = resolve
      arrived += 1
    })
    if (refuse) await route.fulfill({ status: 500, json: { detail: 'Something broke on the server.' } })
    else await route.fallback()
  })
  await page.goto('/#/radio')
  const jazz = page.locator('.radio-station-row').filter({ hasText: 'Local Jazz' })
  const favorite = jazz.getByRole('button', { name: 'Favourite', exact: true })
  const unfavorite = jazz.getByRole('button', { name: 'Unfavourite', exact: true })

  await favorite.click()
  await expect(unfavorite).toBeDisabled()
  await expect.poll(() => arrived).toBe(1)
  release()
  await expect(unfavorite).toBeEnabled()
  expect(mock.stations.find((station) => station.id === 11)?.is_favorite).toBe(true)

  refuse = true
  await unfavorite.click()
  await expect(favorite).toBeDisabled()
  await expect.poll(() => arrived).toBe(2)
  release()
  await expect(unfavorite).toBeEnabled()
  expect(mock.stations.find((station) => station.id === 11)?.is_favorite).toBe(true)
  // The heart going back is explained.
  await expect(page.locator('.toast-error')).toContainText('Something broke on the server.')
})

test('a favourite that cannot reach the server says which station it was', async ({ page }) => {
  await installApiMock(page)
  await page.route(/\/api\/radio\/stations\/11\/favorite$/, (route) => route.abort('failed'))
  await page.goto('/#/radio')
  const jazz = page.locator('.radio-station-row').filter({ hasText: 'Local Jazz' })
  await jazz.getByRole('button', { name: 'Favourite', exact: true }).click()
  await expect(page.locator('.toast-error')).toBeVisible()
  await expect(jazz.getByRole('button', { name: 'Favourite', exact: true })).toBeEnabled()
})
