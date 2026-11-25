import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'

const PIXEL =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='

test('choose artwork from the candidates offered for an album', async ({ page }) => {
  await installApiMock(page)
  const chosen: string[] = []
  await page.route(/\/api\/art\/album\/[^/]+\/candidates$/, async (route) => {
    if (route.request().method() === 'POST') {
      chosen.push(JSON.parse(route.request().postData() ?? '{}').candidate_id)
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, custom_art: true, art_version: 5 }) })
    }
    return route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify([
        { id: 'embedded:1', source: 'Embedded', label: 'In “Bohemian Rhapsody”', url: '/x.png', thumb_url: '/x.png' },
        { id: 'url:https://coverartarchive.org/f.jpg', source: 'Cover Art Archive', label: 'Front · 1975 · GB', url: '/y.png', thumb_url: '/y.png' },
        { id: 'url:https://upload.wikimedia.org/c.jpg', source: 'Wikimedia Commons', label: 'Jane Photographer · CC BY-SA 4.0', url: '/z.png', thumb_url: '/z.png' },
      ]),
    })
  })
  await page.route(/\/(x|y|z)\.png$/, (route) => route.fulfill({ contentType: 'image/png', body: Buffer.from(PIXEL, 'base64') }))
  await page.goto('/#/albums')
  const tile = page.locator('.more-card-wrap').filter({ hasText: 'A Night at the Opera' })
  await tile.locator('.more-btn').click()
  await page.getByRole('menuitem', { name: 'Choose from found artwork…' }).click()
  const picker = page.getByRole('dialog', { name: 'Choose artwork' })
  await expect(picker.getByRole('region', { name: 'Embedded' })).toBeVisible()
  await expect(picker.getByRole('region', { name: 'Cover Art Archive' })).toContainText('Front · 1975 · GB')
  await expect(picker.getByRole('region', { name: 'Wikimedia Commons' })).toBeVisible()
  await picker.getByRole('button', { name: 'Front · 1975 · GB' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(chosen).toEqual(['url:https://coverartarchive.org/f.jpg'])
  await expect(page.locator('.toast-stack')).toContainText('Artwork updated')
})
