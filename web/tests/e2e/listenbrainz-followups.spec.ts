// Explore previews arrive in the background,
// the ListenBrainz link explains a refused token, Last.fm users are told once,
// photo credits sit beside Commons pictures, and Admin can clear art saved
// before sources were recorded.
import { expect, test } from './fixtures'
import { answerDialog } from './dialogs'
import { installApiMock, mockCustomArt } from './mockApi'
import { installWantListMock } from './wantListMock'
import { mockArtistInfo } from './mock/info'
import { openSettings } from './app-helpers'

test('Home asks again while Explore previews are still being looked up', async ({ page }) => {
  const mock = await installApiMock(page)
  let asks = 0
  const full = mock.discoveryState.explore_results
  await page.route('**/api/library/discovery/state', async (route) => {
    asks += 1
    const pending = asks < 3
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ ...mock.discoveryState, explore_results: pending ? [] : full, explore_pending: pending }),
    })
  })
  await page.goto('/#/')
  await expect(page.getByText(full[0].title as string).first()).toBeVisible({ timeout: 15_000 })
  expect(asks).toBe(3) // and no more once the answer is complete
  await page.waitForTimeout(3500)
  expect(asks).toBe(3)
})

test('a token ListenBrainz no longer accepts asks for a new one', async ({ page }) => {
  const mock = await installApiMock(page)
  mock.integrations[0].needs_reconnect = true
  await page.goto('/#/')
  const settings = await openSettings(page, 'Scrobbling')
  const row = settings.locator('[data-provider="listenbrainz"]')
  await expect(row.getByText('ListenBrainz no longer accepts your token - paste a new one.')).toBeVisible()
  await expect(row.getByPlaceholder('ListenBrainz user token')).toBeVisible()
})

test('users whose Last.fm link was removed are told once, until they dismiss it', async ({ page }) => {
  const mock = await installApiMock(page)
  mock.user.dismissed_notices = ['lastfm_was_linked']
  await page.goto('/#/')
  const settings = await openSettings(page, 'Scrobbling')
  const notice = settings.getByText(/Last\.fm was removed \(its API now needs a paid subscription\)/)
  await expect(notice).toBeVisible()
  await settings.getByRole('button', { name: 'Got it' }).click()
  await expect(notice).toHaveCount(0)
  expect(mock.calls.find((c) => c.method === 'POST' && c.path === '/api/auth/dismissed-notices')?.body).toEqual({
    notice: 'lastfm_removed',
  })
})

test('users who never had Last.fm see no notice', async ({ page }) => {
  await installApiMock(page)
  await page.goto('/#/')
  const settings = await openSettings(page, 'Scrobbling')
  await expect(settings.locator('[data-provider="listenbrainz"]')).toBeVisible()
  await expect(settings.getByText(/Last\.fm was removed/)).toHaveCount(0)
})

test('the artist info credit links only web addresses', async ({ page }) => {
  const mock = await installApiMock(page)
  await installWantListMock(page)
  // Answered here from the mock's own data (there is no server behind the
  // mocked API to fetch from), with a credit whose link is not a web address.
  await page.route('**/api/library/artist-info?**', async (route) => {
    await route.fulfill({
      json: {
        ...mockArtistInfo(mock, 'Queen'),
        pending: false,
        image_credit: { author: 'Jane Photographer', license: 'CC BY 4.0', license_url: 'intent://evil', page_url: null },
      },
    })
  })
  await page.goto('/#/artists')
  await page.getByRole('button', { name: 'More actions for Queen', exact: true }).click()
  await page.locator('.track-menu-panel').getByRole('menuitem', { name: 'Info', exact: true }).click()
  const credit = page.getByRole('dialog', { name: 'Queen' }).locator('.info-image-credit')
  await expect(credit).toHaveText('Photo: Jane Photographer · CC BY 4.0')
  await expect(credit.locator('a')).toHaveCount(0)
})

test('an artist picture from Wikimedia Commons shows its credit on the artist page', async ({ page }) => {
  const mock = await installApiMock(page)
  mockCustomArt.set('artist:artist-1', 7)
  mock.artCredits['artist:artist-1'] = {
    author: 'Jane Photographer',
    license: 'CC BY-SA 4.0',
    license_url: 'https://creativecommons.org/licenses/by-sa/4.0',
    page_url: 'https://commons.wikimedia.org/wiki/File:Queen.jpg',
  }
  await page.goto('/#/artists/Queen')
  const credit = page.locator('.collection-detail .info-image-credit')
  await expect(credit).toHaveText('Photo: Jane Photographer · CC BY-SA 4.0')
  await expect(credit.getByRole('link', { name: 'Jane Photographer' })).toHaveAttribute(
    'href',
    'https://commons.wikimedia.org/wiki/File:Queen.jpg',
  )
  await expect(credit.getByRole('link', { name: 'CC BY-SA 4.0' })).toHaveAttribute('rel', 'noopener noreferrer')
})

test('an artist without a credited picture shows no credit line', async ({ page }) => {
  const mock = await installApiMock(page)
  await page.goto('/#/artists/Queen')
  await expect(page.getByRole('heading', { name: 'Queen' }).first()).toBeVisible()
  await expect(page.locator('.collection-detail .info-image-credit')).toHaveCount(0)
  // No custom art: the credit is not even asked for.
  expect(mock.calls.some((c) => c.path.endsWith('/credit'))).toBe(false)
})

test('a wanted artist page credits its Commons picture', async ({ page }) => {
  await installApiMock(page)
  const want = await installWantListMock(page)
  want.artistArtCredit = { author: 'Pat Lens', license: 'CC0', license_url: null, page_url: null }
  want.entries.push({
    id: 501, kind: 'artist', artist: 'David Bowie', title: 'David Bowie', album: null, station_name: null,
    artist_mbid: 'mb-bowie', release_group_mbid: null, recording_mbid: null, release_type: null, excluded: false,
    media_item_id: null, owned_count: null, total_count: null, owned: false, created_at: '2026-10-29T00:00:00Z',
  })
  await page.goto('/#/want-list/artist/David%20Bowie/mb-bowie')
  await expect(page.locator('.want-artist-hero .info-image-credit')).toHaveText('Photo: Pat Lens · CC0')
})

test('Admin offers to remove artwork saved before sources were recorded', async ({ page }) => {
  const mock = await installApiMock(page)
  mock.unrecordedArt = 3
  await page.goto('/#/admin')
  const section = page.getByRole('region', { name: 'Older artwork' })
  await expect(section).toContainText('3 artworks were saved before 0.23.0 without a recorded source')
  await section.getByRole('button', { name: 'Remove them' }).click()
  await answerDialog(page, 'accept')
  await expect(section.getByRole('status')).toHaveText('Removed 3 artworks.')
  expect(mock.calls.some((c) => c.method === 'POST' && c.path === '/api/admin/custom-art/unrecorded/remove')).toBe(true)
})

test('Admin shows no clean-up when there is nothing to remove', async ({ page }) => {
  const mock = await installApiMock(page)
  await page.goto('/#/admin')
  await expect.poll(() => mock.calls.some((c) => c.path === '/api/admin/custom-art/unrecorded')).toBe(true)
  await expect(page.getByRole('region', { name: 'Older artwork' })).toHaveCount(0)
})
