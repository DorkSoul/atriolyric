import { expect, test } from './fixtures'
import { type Page } from 'playwright/test'
import { installApiMock, type AppMockState } from './mockApi'
import { installWantListMock } from './wantListMock'
import { player, silentWav, chooseOption, selectMenu, nowPlayingView } from './app-helpers'

test.use({ viewport: { width: 375, height: 800 } })

async function open(page: Page, hash: string, options?: { realAudio?: boolean }): Promise<AppMockState> {
  const mock = await installApiMock(page)
  // The mock's stream is an empty file, which Firefox keeps re-requesting
  // while the page plays it, so a reload never finishes loading. A test that
  // plays a song and then reloads gets a second of real silence instead.
  if (options?.realAudio) {
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(1) }))
  }
  // The artist page's "Show missing" switch asks the catalog for missing releases.
  await installWantListMock(page)
  await page.goto(hash)
  return mock
}

function moreButton(page: Page, name: string) {
  return page.getByRole('button', { name: `More actions for ${name}`, exact: true })
}

async function openArtistInfo(page: Page) {
  await moreButton(page, 'Queen').click()
  await page.locator('.track-menu-panel').getByRole('menuitem', { name: 'Info', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Queen' })
  await expect(dialog).toBeVisible()
  return dialog
}

test.describe('MusicBrainz/ListenBrainz info', () => {
  test('artist Info shows the bio as plain text and links members to their page', async ({ page }) => {
    await open(page, '/#/artists')
    const dialog = await openArtistInfo(page)
    await expect(dialog).toContainText('UK rock group')
    await expect(dialog).toContainText('312M plays · 6.1M listeners on ListenBrainz')
    await expect(dialog).toContainText('Queen were a British rock band formed in London in 1970.')
    await expect(dialog).toContainText('Their classic line-up was Freddie Mercury, Brian May')
    // Markup in a bio is never rendered or shown as text.
    const bio = dialog.locator('.info-prose')
    await expect(bio.locator('a[href="https://example.org/queen-more"]')).toHaveCount(0)
    await expect(bio.locator('b')).toHaveCount(0)
    await expect(dialog).not.toContainText('<a')
    await expect(dialog).not.toContainText('Read more on Wikipedia')
    await expect(dialog).toContainText('Source: Wikipedia')
    // The Commons photo's credit, as its licence asks.
    await expect(dialog.locator('.info-image-credit')).toHaveText('Photo: Jane Photographer · CC BY-SA 4.0')
    await expect(dialog.getByRole('link', { name: 'Jane Photographer' })).toHaveAttribute(
      'href', 'https://commons.wikimedia.org/wiki/File:Queen.jpg',
    )
    await expect(dialog.getByRole('link', { name: 'ListenBrainz' })).toBeVisible()
    await expect(dialog).not.toContainText('Last.fm')
    await expect(dialog).toContainText('1970–present')
    await expect(dialog.getByRole('list', { name: 'Tags' })).toContainText('classic rock')

    const site = dialog.getByRole('link', { name: 'Official site' })
    await expect(site).toHaveAttribute('target', '_blank')
    await expect(site).toHaveAttribute('rel', 'noopener noreferrer')

    // Only library artists are links; the rest stay plain text.
    await expect(dialog.getByRole('button', { name: 'Freddie Mercury' })).toHaveCount(0)
    await expect(dialog.getByRole('button', { name: 'David Bowie' })).toHaveCount(0)
    await expect(dialog.getByRole('button', { name: 'M83' })).toBeVisible()
    await expect(dialog.locator('.info-technical')).toContainText('0383dadf-2a4e-4d10-a46a-e9e041da8eb3')

    // Fits a phone.
    const box = await dialog.boundingBox()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(375)

    await dialog.getByRole('button', { name: 'Brian May' }).click()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(page).toHaveURL(/#\/artists\/Brian%20May$/)
  })

  test('admins can fix and clear the MusicBrainz match', async ({ page }) => {
    const mock = await open(page, '/#/artists')
    const dialog = await openArtistInfo(page)
    await dialog.getByRole('button', { name: 'Wrong artist? Fix match' }).click()
    const tribute = dialog.locator('.info-candidate').filter({ hasText: 'tribute band' })
    await expect(tribute).toBeVisible()
    await tribute.click()
    await expect(dialog.locator('.modal-subtitle')).toHaveText('tribute band')
    await expect(dialog).not.toContainText('Queen were a British rock band')
    expect(mock.calls.find((c) => c.method === 'PUT' && c.path === '/api/library/artist-match')?.body).toEqual({
      name: 'Queen',
      mbid: '11111111-2222-3333-4444-555555555555',
    })

    await dialog.getByRole('button', { name: 'Wrong artist? Fix match' }).click()
    await dialog.getByRole('button', { name: 'Clear match' }).click()
    await expect(dialog.getByRole('button', { name: 'Clear match' })).toHaveCount(0)
    expect(mock.calls.filter((c) => c.method === 'PUT' && c.path === '/api/library/artist-match').at(-1)?.body).toEqual({
      name: 'Queen',
      mbid: null,
    })
  })

  test('non-admins get no fix-match button', async ({ page }) => {
    const mock = await installApiMock(page)
    mock.user.is_admin = false
    await page.goto('/#/artists')
    const dialog = await openArtistInfo(page)
    await expect(dialog).toContainText('Queen were a British rock band')
    await expect(dialog.getByRole('button', { name: 'Wrong artist? Fix match' })).toHaveCount(0)
  })

  test('album Info shows release facts and lets admins pick the edition', async ({ page }) => {
    const mock = await open(page, '/#/albums')
    await moreButton(page, 'A Night at the Opera').click()
    await page.locator('.track-menu-panel').getByRole('menuitem', { name: 'Info', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: 'A Night at the Opera' })
    await expect(dialog).toContainText('Albums')
    await expect(dialog).toContainText('1975-11-21')
    await expect(dialog).toContainText('EMI · EMTC 103')
    await expect(dialog).toContainText('45M plays · 1.5M listeners on ListenBrainz')
    await expect(dialog.getByRole('link', { name: 'View on ListenBrainz' })).toHaveAttribute(
      'href', 'https://listenbrainz.org/album/rg-opera/',
    )
    await expect(dialog).toContainText('Editions (2)')

    const edition = selectMenu(dialog, 'Edition')
    await expect(edition).toHaveAccessibleName(/Automatic/)
    await chooseOption(page, edition, /2011 remaster/)
    await expect(dialog).toContainText('Island · 2764561')
    await expect(dialog).toContainText('602527645611')
    await expect(edition).toHaveAccessibleName(/2011 remaster/)
    expect(mock.calls.find((c) => c.method === 'PUT' && c.path === '/api/library/album-edition')?.body).toEqual({
      artist: 'Queen',
      album: 'A Night at the Opera',
      release_mbid: 'rel-2011-remaster',
    })

    await chooseOption(page, edition, 'Automatic')
    await expect(dialog).toContainText('EMI · EMTC 103')
    expect(mock.calls.filter((c) => c.method === 'PUT' && c.path === '/api/library/album-edition').at(-1)?.body)
      .toMatchObject({ release_mbid: null })
  })

  test('song Info shows stats, credits grouped by role and identifiers', async ({ page }) => {
    await open(page, '/#/songs')
    const row = page.locator('.track-row').filter({ hasText: 'Bohemian Rhapsody' })
    await row.getByRole('button', { name: 'More actions' }).click()
    await page.locator('.track-menu-panel').getByRole('menuitem', { name: 'Info', exact: true }).click()
    const dialog = nowPlayingView(page)
    await expect(dialog).toContainText('1.2M plays · 340K listeners on ListenBrainz')
    await expect(dialog).toContainText('17 here')
    await expect(dialog).not.toContainText('Last.fm')
    const credits = dialog.getByRole('region', { name: 'Credits' })
    await expect(credits).toContainText('Composer')
    await expect(credits).toContainText('Roy Thomas Baker, Queen')
    await expect(credits).toContainText('Brian May (guitar)')
    await expect(dialog).toContainText('GBUM71029604')
    await expect(dialog).toContainText('1975-10-31')
    await dialog.locator('.info-technical summary').click()
    await expect(dialog.locator('.info-technical')).toContainText('rec-bohemian')
  })

  test('Now Playing shows the ListenBrainz plays line', async ({ page }) => {
    await open(page, '/#/songs')
    await page
      .locator('.track-row')
      .filter({ hasText: 'Bohemian Rhapsody' })
      .getByRole('button', { name: 'Play Bohemian Rhapsody' })
      .click()
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    await expect(page.locator(':is(.nowplaying-overlay, .nowplaying-page) .nowplaying-stats')).toHaveText(
      '1.2M plays · 340K listeners on ListenBrainz',
    )
  })

  test('Top tracks shows owned songs, and missing ones on request', async ({ page }) => {
    await open(page, '/#/artists/Queen', { realAudio: true })
    const section = page.getByRole('region', { name: 'Top tracks' })
    await expect(section.getByRole('heading', { name: 'Top tracks' })).toBeVisible()
    const rows = section.locator('.top-track-row')
    await expect(rows).toHaveCount(2)
    await expect(rows.nth(0)).toContainText('Bohemian Rhapsody')
    await expect(rows.nth(0)).toContainText('4.8M')
    await expect(section.locator('.is-missing')).toHaveCount(0)
    await expect(section.getByRole('button', { name: 'View more' })).toHaveCount(0)

    await section.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')

    // The artist page's one Show missing switch also covers Top tracks.
    const toggle = page.getByLabel('Show missing')
    await toggle.check()
    await expect(rows).toHaveCount(5)
    const missing = section.locator('.top-track-row.is-missing')
    await expect(missing.first()).toContainText('Another One Bites the Dust')
    await expect(missing.first().locator('[aria-disabled="true"]')).toBeVisible()
    // A missing song can't play, only be wanted.
    await expect(missing.first().getByRole('button')).toHaveCount(1)
    await expect(missing.first().getByRole('button', { name: 'Add to want list' })).toBeVisible()
    // Rank order, owned and missing mixed.
    await expect(rows.nth(2)).toContainText("Don't Stop Me Now")

    const viewMore = section.getByRole('button', { name: 'View more' })
    await viewMore.click()
    await expect(rows).toHaveCount(8)
    await section.getByRole('button', { name: 'View less' }).click()
    await expect(rows).toHaveCount(5)

    await page.reload()
    await expect(page.getByLabel('Show missing')).toBeChecked()
    await expect(page.locator('.top-track-row.is-missing').first()).toBeVisible()
  })

  test('Top tracks is hidden for artists ListenBrainz has none for', async ({ page }) => {
    const answered = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/library/artist-top-tracks')
    await open(page, '/#/artists/M83')
    await expect(page.getByRole('heading', { name: 'M83' })).toBeVisible()
    // After the answer, not before it: the placeholder is not a region either.
    await answered
    await expect(page.locator('.top-tracks-placeholder')).toHaveCount(0)
    await expect(page.getByRole('region', { name: 'Top tracks' })).toHaveCount(0)
  })

  test('admins prefetch artist info', async ({ page }) => {
    const mock = await open(page, '/#/admin')
    const root = page.locator('.admin-root-row').filter({ hasText: '/media/music' })
    await root.getByText('Run a single step instead').click()
    await root.getByRole('button', { name: 'Artist info', exact: true }).click()
    // The mock finishes the task at once, so the next poll swaps in its summary.
    await expect(root).toContainText(/Artist info (started\.|: saved info for 2 of 3 artists)/)
    expect(mock.calls.find((c) => c.method === 'POST' && c.path === '/api/admin/roots/1/enrich')?.body).toEqual({
      mode: 'artist_info',
    })
  })
})
