import { expect, test } from './fixtures'
import { type Page } from 'playwright/test'
import { installApiMock, type MediaItem } from './mockApi'
import { player } from './app-helpers'

function track(id: number, values: Partial<MediaItem>): MediaItem {
  return {
    id,
    library_root_id: 1,
    kind: 'track',
    title: `Track ${id}`,
    artist: 'Artist',
    album: 'Album',
    genre: 'Rock',
    track_number: 1,
    performer: null,
    description: null,
    duration_seconds: 200,
    is_favorite: false,
    is_disliked: false,
    excluded_by: null,
    format_label: null,
    ...values,
  }
}

async function openWithCollab(page: Page) {
  const state = await installApiMock(page)
  state.tracks.push(
    track(60, {
      title: 'Under Pressure',
      artist: 'Queen feat. David Bowie',
      album: 'Hot Space',
      artists: [
        { name: 'Queen', role: 'main' },
        { name: 'David Bowie', role: 'featured' },
      ],
      album_artist: 'Queen',
    }),
    track(61, {
      title: 'Space Oddity',
      artist: 'David Bowie',
      album: 'Space Oddity',
      artists: [{ name: 'David Bowie', role: 'main' }],
      section: 'Single',
    }),
  )
  return state
}

test('a multi-artist song opens a picker with each artist and their role', async ({ page }) => {
  await openWithCollab(page)
  await page.goto('/#/songs')
  const row = page.locator('.track-row').filter({ has: page.getByRole('button', { name: 'Play Under Pressure' }) })
  await row.getByRole('link', { name: 'Queen feat. David Bowie' }).click()
  const picker = page.getByRole('dialog', { name: 'Choose an artist' })
  await expect(picker).toBeVisible()
  await expect(picker.getByRole('button')).toHaveCount(3) // two artists + close
  await expect(picker.getByRole('button', { name: /David Bowie/ })).toContainText('Featured')
  await expect(picker.getByRole('button', { name: /^Queen/ })).toContainText('Artist')
  await picker.getByRole('button', { name: /David Bowie/ }).click()
  await expect(page).toHaveURL(/#\/artists\/David%20Bowie$/)
  await expect(page.getByRole('dialog')).toHaveCount(0)
  // Nothing started playing from the row the picker sat in.
  await expect(player(page)).not.toContainText('Under Pressure')
})

test('a single-artist song links straight to the artist', async ({ page }) => {
  await openWithCollab(page)
  await page.goto('/#/songs')
  await page
    .locator('.track-row')
    .filter({ has: page.getByRole('button', { name: 'Play Space Oddity' }) })
    .getByRole('link', { name: 'David Bowie' })
    .click()
  await expect(page).toHaveURL(/#\/artists\/David%20Bowie$/)
  await expect(page.getByRole('dialog')).toHaveCount(0)
})

test('artist pages split albums by type and list guest spots under Appears on', async ({ page }) => {
  await openWithCollab(page)
  await page.goto('/#/artists/David%20Bowie')
  const singles = page.getByRole('region', { name: 'Singles' })
  await expect(singles).toContainText('Space Oddity')
  const appears = page.getByRole('region', { name: 'Appears on' })
  await expect(appears).toContainText('Hot Space')
  // The guest-spot album opens by its owner, not the guest.
  await appears.locator('.browse-card').filter({ hasText: 'Hot Space' }).click()
  await expect(page.getByRole('button', { name: 'Play Under Pressure' })).toBeVisible()
})

test('an album tile links its artist', async ({ page }) => {
  await openWithCollab(page)
  await page.goto('/#/albums')
  const tile = page.locator('.more-card-wrap').filter({ hasText: 'Hot Space' })
  await tile.locator('.browse-subtitle').getByRole('link', { name: 'Queen' }).click()
  await expect(page).toHaveURL(/#\/artists\/Queen$/)
})

test('queue rows link their artist', async ({ page }) => {
  // Below the side panel's 1200px, so Now Playing (a page) carries Up next.
  await page.setViewportSize({ width: 1100, height: 800 })
  await openWithCollab(page)
  await page.goto('/#/songs')
  await page.getByRole('button', { name: 'Play Space Oddity' }).click()
  await player(page).getByRole('button', { name: 'Open now playing' }).click()
  // Up next is in view beside the controls.
  const queueRow = page.locator(':is(.nowplaying-overlay, .nowplaying-page) .queue-row-info').filter({ hasText: 'Space Oddity' }).first()
  await queueRow.getByRole('link', { name: 'David Bowie' }).click()
  await expect(page).toHaveURL(/#\/artists\/David%20Bowie$/)
})

test.describe('scrolling text', () => {
  test.use({ viewport: { width: 375, height: 800 } })

  test('only scrolls when the text overflows', async ({ page }) => {
    const state = await openWithCollab(page)
    state.tracks.push(track(62, { title: 'Short', artist: 'Queen', album: 'X' }))
    state.tracks.push(
      track(63, {
        title: 'An extraordinarily long song title that cannot possibly fit on a phone screen',
        artist: 'Queen',
        album: 'X',
      }),
    )
    await page.goto('/#/songs')
    const short = page.locator('.track-row').filter({ has: page.getByRole('button', { name: 'Play Short' }) }).locator('.track-title')
    await expect(short).toHaveAttribute('data-marquee', 'static')
    const long = page.locator('.track-row').filter({ has: page.getByRole('button', { name: /Play An extraordinarily/ }) }).locator('.track-title')
    await long.scrollIntoViewIfNeeded()
    // Only the playing track's text moves by itself; the rest stays still...
    await expect(long).toHaveAttribute('data-marquee', 'static')
    // ...and scrolls while the row is hovered or focused, to be read in full.
    await page.locator('.track-row').filter({ has: page.getByRole('button', { name: /Play An extraordinarily/ }) }).hover()
    await expect(long).toHaveAttribute('data-marquee', 'scroll')
  })

  test('stays still with reduced motion', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' })
    const state = await openWithCollab(page)
    state.tracks.push(
      track(63, {
        title: 'An extraordinarily long song title that cannot possibly fit on a phone screen',
        artist: 'Queen',
        album: 'X',
      }),
    )
    await page.goto('/#/songs')
    const long = page.locator('.track-row').filter({ has: page.getByRole('button', { name: /Play An extraordinarily/ }) }).locator('.track-title')
    await long.scrollIntoViewIfNeeded()
    await expect(long).toHaveAttribute('data-marquee', 'static')
  })
})

test('admins edit the artist splitting rules', async ({ page }) => {
  await installApiMock(page)
  await page.goto('/#/admin')
  const section = page.getByRole('region', { name: 'Artist splitting' })
  const separators = section.getByLabel('Separators')
  await expect(separators).toHaveValue('";", "/"')
  await separators.fill('";", "/", " & "')
  await section.getByLabel('Never split (one name per line)').fill('AC/DC\nSimon & Garfunkel')
  await section.getByRole('button', { name: 'Save and re-link' }).click()
  await expect(section.getByRole('status')).toContainText('Re-linked')
  await separators.fill('not quoted')
  await section.getByRole('button', { name: 'Save and re-link' }).click()
  await expect(section).toContainText('Separators must be quoted')
})
