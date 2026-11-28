import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'
import { installWantListMock } from './wantListMock'
import { player, openSettings } from './app-helpers'

async function openWantList(page: import('playwright/test').Page) {
  await installApiMock(page)
  const want = await installWantListMock(page)
  await page.goto('/#/want-list')
  await expect(page.getByRole('heading', { name: 'Want list', level: 1 })).toBeVisible()
  return want
}

test('the Want list tab sits between Files and Admin and can be hidden', async ({ page }) => {
  await installApiMock(page)
  await installWantListMock(page)
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.goto('/#/home')
  // Admin only appears once the account has loaded.
  await expect(page.locator('nav.tabs').getByRole('button', { name: 'Admin' })).toBeVisible()
  // The labels only: a sidebar entry also shows its count (the button's
  // description, not its name), which would run into the text content.
  const labels = await page.locator('nav.tabs .sidebar-label').allTextContents()
  const want = labels.indexOf('Want list')
  expect(want).toBeGreaterThan(labels.indexOf('Files'))
  // After the divider: Library info, then Admin.
  expect(labels.indexOf('Library info')).toBe(want + 1)
  expect(labels.indexOf('Admin')).toBe(want + 2)
  await openSettings(page, 'Navigation')
  const row = page.locator('.tab-order-row').filter({ hasText: 'Want list' })
  await row.getByRole('switch').uncheck()
  await expect(page.locator('nav.tabs').getByRole('button', { name: 'Want list' })).toHaveCount(0)
})

test('phones list the Want list under More', async ({ page }) => {
  await installApiMock(page)
  await installWantListMock(page)
  await page.setViewportSize({ width: 375, height: 800 })
  await page.goto('/#/home')
  await page.getByRole('navigation', { name: 'Quick tab navigation' }).getByRole('button', { name: 'More' }).click()
  await page.getByRole('dialog', { name: 'More' }).getByRole('button', { name: 'Want list' }).click()
  await expect(page.getByRole('heading', { name: 'Want list', level: 1 })).toBeVisible()
})

test('search runs only on Enter and shows a loading box', async ({ page }) => {
  await page.clock.install()
  const want = await openWantList(page)
  const box = page.getByRole('searchbox', { name: /Search MusicBrainz/ })
  await box.fill('bowie')
  // Move the clock well past any typing debounce: nothing may have searched.
  await page.clock.fastForward(5_000)
  expect(want.searches).toEqual([])
  await box.press('Enter')
  await expect(page.getByRole('status')).toContainText('Searching MusicBrainz')
  await expect(page.getByRole('region', { name: 'Artists' })).toContainText('David Bowie')
  expect(want.searches).toEqual(['bowie'])
  await expect(page).toHaveURL(/#\/want-list\/search\/bowie$/)
  await expect(page.getByRole('region', { name: 'Songs' })).toContainText('Starman')
})

test('add songs and albums from search, then see them in the list', async ({ page }) => {
  await openWantList(page)
  await page.getByRole('searchbox', { name: /Search MusicBrainz/ }).fill('bowie')
  await page.keyboard.press('Enter')
  const songs = page.getByRole('region', { name: 'Songs' })
  await songs.getByRole('button', { name: 'Add to want list' }).click()
  await expect(songs.getByRole('button', { name: 'Remove from want list' }).or(songs.getByTitle('On your want list'))).toBeVisible()
  const albums = page.getByRole('region', { name: 'Albums' })
  await albums.getByRole('button', { name: 'Add to want list' }).click()
  const wanted = page.getByRole('region', { name: 'Wanted' }).or(page.locator('.want-section').filter({ hasText: 'Wanted' }))
  await expect(wanted.locator('.want-artist-card')).toContainText('David Bowie')
  await wanted.locator('.want-artist-card').filter({ hasText: 'David Bowie' }).click()
  await expect(page).toHaveURL(/#\/want-list\/artist\/David%20Bowie/)
  await expect(page.getByRole('region', { name: 'Albums' })).toContainText('Ziggy Stardust')
  await expect(page.getByRole('region', { name: 'Albums' })).toContainText('1/3')
  await expect(page.getByRole('region', { name: 'Songs' })).toContainText('Starman')
})

test('a catalog artist page greys missing releases and wants the whole artist', async ({ page }) => {
  await openWantList(page)
  await page.getByRole('searchbox', { name: /Search MusicBrainz/ }).fill('bowie')
  await page.keyboard.press('Enter')
  await page.getByRole('region', { name: 'Artists' }).getByRole('button', { name: /^David Bowie/ }).click()
  await expect(page).toHaveURL(/#\/want-list\/catalog\/mb-bowie/)
  // No play controls on a catalog page.
  await expect(page.locator('main').getByRole('button', { name: /^Play/ })).toHaveCount(0)
  const albums = page.getByRole('region', { name: 'Albums' })
  await expect(albums.locator('.catalog-tile.is-missing')).toContainText('Ziggy Stardust')
  await expect(albums.locator('.catalog-tile:not(.is-missing)')).toContainText('Hunky Dory')
  await expect(albums).toContainText('1/3')
  await expect(page.getByRole('region', { name: 'Singles' })).toContainText('Heroes')

  await page.getByRole('button', { name: 'Want everything' }).click()
  await expect(page.getByRole('button', { name: 'Wanted' })).toBeVisible()
  // Wanted through the artist; tapping a release's ✓ leaves it out.
  const single = page.getByRole('region', { name: 'Singles' }).locator('.catalog-tile')
  await single.getByTitle('Remove from want list').click()
  await expect(single.getByTitle('Add to want list')).toBeVisible()

  // The switch hides what is not wanted: owned and left-out releases.
  await page.getByRole('switch', { name: 'Show releases not wanted' }).click()
  await expect(albums.locator('.catalog-tile')).toHaveCount(1)
  await expect(albums).toContainText('Ziggy Stardust')
  await expect(page.getByRole('region', { name: 'Singles' })).toHaveCount(0)
})

test('a catalog album lists owned songs and wants missing ones', async ({ page }) => {
  await installApiMock(page)
  await installWantListMock(page)
  await page.goto('/#/want-list/album/rg-ziggy/David%20Bowie')
  const list = page.getByRole('list', { name: 'Tracklist' })
  await expect(list.locator('.catalog-track')).toHaveCount(3)
  await expect(list.locator('.catalog-track').first()).toContainText('In library')
  await expect(list.locator('.catalog-track.is-missing')).toHaveCount(2)
  await list.locator('.catalog-track').filter({ hasText: 'Starman' }).getByTitle('Add to want list').click()
  await expect(list.locator('.catalog-track').filter({ hasText: 'Starman' }).getByRole('button', { pressed: true })).toBeVisible()
  await page.getByRole('button', { name: 'Want album' }).click()
  await expect(page.getByRole('button', { name: 'Wanted' })).toBeVisible()
})

test('an owned artist from search opens their library page with missing shown', async ({ page }) => {
  await openWantList(page)
  await page.getByRole('searchbox', { name: /Search MusicBrainz/ }).fill('queen')
  await page.keyboard.press('Enter')
  await page.getByRole('region', { name: 'Artists' }).getByRole('button', { name: /^Queen/ }).click()
  await expect(page).toHaveURL(/#\/artists\/Queen$/)
  expect(await page.evaluate(() => localStorage.getItem('atriolyric.showMissing'))).toBe('1')
})

test('the old Settings want-list section is gone', async ({ page }) => {
  await installApiMock(page)
  await page.goto('/#/home')
  await page.getByRole('button', { name: 'Settings', exact: true }).click()
  const menu = page.locator('.settings-menu-panel')
  await expect(menu.getByRole('menuitem', { name: 'Account', exact: true })).toBeVisible()
  await expect(menu.getByRole('menuitem', { name: 'Want list', exact: true })).toHaveCount(0)
})

test('radio bookmarks show under their artist, playable once in the library', async ({ page }) => {
  await installApiMock(page)
  const want = await installWantListMock(page)
  const base = { album: null, artist_mbid: null, release_group_mbid: null, recording_mbid: null, release_type: null, excluded: false, owned_count: null, total_count: null, created_at: '2026-10-01T00:00:00Z' }
  want.entries.push(
    { ...base, id: 50, kind: 'track', artist: 'Queen', title: 'Bohemian Rhapsody', station_name: 'Local Rock', media_item_id: 1, owned: true },
    { ...base, id: 51, kind: 'track', artist: 'Queen', title: 'Unreleased', station_name: 'Local Rock', media_item_id: null, owned: false },
  )
  await page.goto('/#/want-list')
  await page.locator('.want-artist-card').filter({ hasText: 'Queen' }).click()
  const songs = page.getByRole('region', { name: 'Songs' })
  await expect(songs).toContainText('From radio: Local Rock')
  const owned = songs.locator('.want-result').filter({ hasText: 'Bohemian Rhapsody' })
  await expect(owned).toContainText('In library')
  await expect(songs.locator('.want-result').filter({ hasText: 'Unreleased' }).getByRole('button', { name: /^Play/ })).toHaveCount(0)
  await owned.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
  await expect(player(page)).toContainText('Bohemian Rhapsody')
  await songs.locator('.want-result').filter({ hasText: 'Unreleased' }).getByRole('button', { name: 'Remove from want list' }).click()
  await expect(songs.locator('.want-result').filter({ hasText: 'Unreleased' })).toHaveCount(0)
})
