import type { Page } from 'playwright/test'
import { expect, test } from './fixtures'
import { installApiMock, type AppMockState, type MediaItem } from './mockApi'
import { installWantListMock } from './wantListMock'

// The browse screens: which of them have no search box, the
// History link, the Show missing switch, the A-Z jump, the Songs Table /
// Cards and Compact choices, and Customise Home.

const main = (page: Page) => page.locator('.app-main')

test.describe('search boxes', () => {
  for (const width of [1280, 390]) {
    test(`Songs, Albums and Artists have no search box at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 800 })
      await installApiMock(page)
      for (const hash of ['/#/songs', '/#/albums', '/#/artists']) {
        await page.goto(hash)
        await expect(page.getByRole('button', { name: /^Genre/ })).toBeVisible()
        await expect(page.getByRole('textbox')).toHaveCount(0)
        await expect(page.getByRole('searchbox')).toHaveCount(0)
        await expect(page.getByRole('button', { name: 'Search and filter' })).toHaveCount(0)
      }
    })
  }

  test('a genre picked on Songs filters the list', async ({ page }) => {
    const mock = await installApiMock(page)
    await page.goto('/#/songs')
    await page.getByRole('button', { name: /^Genre/ }).click()
    await page.getByRole('menuitemradio', { name: 'Rock', exact: true }).click()
    await expect.poll(() => mock.calls.some((call) => call.path === '/api/library/tracks' && /genre=Rock/.test(call.url))).toBe(true)
  })

  test('the genre menu narrows as you type, and Enter picks the first match', async ({ page }) => {
    const mock = await installApiMock(page)
    await page.goto('/#/songs')
    await page.getByRole('button', { name: /^Genre/ }).click()
    const search = page.getByRole('searchbox', { name: 'Search genres' })
    // With a mouse the field has focus straight away.
    await expect(search).toBeFocused()
    await page.keyboard.type('roc')
    const menu = page.getByRole('menu', { name: 'Genre' })
    await expect(menu.getByRole('menuitemradio', { name: 'Rock', exact: true })).toBeVisible()
    await expect(menu.getByRole('menuitemradio', { name: 'Electronic', exact: true })).toHaveCount(0)
    await expect(menu.getByRole('menuitemradio', { name: 'All genres' })).toHaveCount(0)
    await page.keyboard.type('zz')
    await expect(menu).toContainText('No genres match')
    await page.keyboard.press('Backspace')
    await page.keyboard.press('Backspace')
    await page.keyboard.press('Enter')
    await expect(menu).toHaveCount(0)
    await expect.poll(() => mock.calls.some((call) => call.path === '/api/library/tracks' && /genre=Rock/.test(call.url))).toBe(true)
    // Opening it again starts with an empty search.
    await page.getByRole('button', { name: /^Genre/ }).click()
    await expect(page.getByRole('searchbox', { name: 'Search genres' })).toHaveValue('')
  })
})

test('Recently played has a History link that opens the Recently played tab', async ({ page }) => {
  await installApiMock(page)
  await page.goto('/#/home')
  const section = page.getByRole('region', { name: 'Recently played' })
  await section.getByRole('link', { name: 'History' }).click()
  await expect(page).toHaveURL(/#\/recent-played$/)
  await expect(page.getByRole('heading', { name: 'Recently played', level: 1 })).toBeVisible()
})

test('Show missing is a switch on album and artist pages and is shared', async ({ page }) => {
  await installApiMock(page)
  await installWantListMock(page)
  await page.goto('/#/albums/Queen/A%20Night%20at%20the%20Opera')
  const albumSwitch = page.getByRole('switch', { name: 'Show missing' })
  await expect(albumSwitch).not.toBeChecked()
  await albumSwitch.click()
  await expect(albumSwitch).toBeChecked()
  await page.goto('/#/artists/Queen')
  const artistSwitch = page.getByRole('switch', { name: 'Show missing' })
  await expect(artistSwitch).toBeChecked()
  await artistSwitch.click()
  await expect(artistSwitch).not.toBeChecked()
})

/** 260 artists, ten per letter A-Z ("A-0" … "Z-259"), served whole. */
async function seedArtists(page: Page) {
  const rows = Array.from({ length: 260 }, (_, index) => ({
    artist: `${String.fromCharCode(65 + Math.floor(index / 10))}-${index}`,
    track_count: 1,
    album_count: 1,
    representative_media_item_id: 1000 + index,
  }))
  await page.route('**/api/library/artists**', (route) => {
    const url = new URL(route.request().url())
    if (url.searchParams.get('full') === '1') return route.fulfill({ json: rows })
    const offset = Number(url.searchParams.get('offset') ?? 0)
    const limit = Number(url.searchParams.get('limit') ?? rows.length)
    return route.fulfill({ json: rows.slice(offset, offset + limit) })
  })
}

test.describe('A-Z jump', () => {
  test('a row of letters under the toolbar on a desktop-width screen', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 })
    await installApiMock(page)
    await seedArtists(page)
    await page.goto('/#/artists')
    await page.getByRole('button', { name: 'List', exact: true }).click()
    const row = page.locator('.alphabet-jump-row')
    await expect(row).toBeVisible()
    const box = (await row.boundingBox())!
    // Letters laid out sideways (wrapping to a few lines at most), on the
    // same line as the genre picker, to its right.
    expect(box.width).toBeGreaterThan(box.height * 2)
    const genre = (await page.getByRole('button', { name: /^Genre/ }).boundingBox())!
    const genreMid = genre.y + genre.height / 2
    expect(genreMid).toBeGreaterThan(box.y)
    expect(genreMid).toBeLessThan(box.y + box.height)
    expect(box.x).toBeGreaterThan(genre.x + genre.width)
    await expect(page.locator('.alphabet-jump-bar')).toHaveCount(0)
    await expect.poll(() => page.locator('.browse-list-row').count()).toBeGreaterThan(5)
    await row.getByRole('button', { name: 'Jump to M' }).click()
    const first = page.locator('.browse-list-row').filter({ hasText: 'M-120' })
    await expect(first).toBeVisible()
    // It lands just below the (sticky) letter row, not under it.
    await expect
      .poll(async () => {
        const rowBox = (await row.boundingBox())!
        const target = (await first.boundingBox())!
        return target.y >= rowBox.y + rowBox.height - 2 && target.y < rowBox.y + rowBox.height + 80
      })
      .toBe(true)
  })

  test('a vertical rail on the right edge on a phone', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 800 })
    await installApiMock(page)
    await seedArtists(page)
    await page.goto('/#/artists')
    await expect(page.locator('.browse-card').first()).toBeVisible()
    const rail = page.locator('.alphabet-jump-bar-inner')
    await expect(rail).toBeVisible()
    await expect(page.locator('.alphabet-jump-row')).toHaveCount(0)
    const box = (await rail.boundingBox())!
    expect(box.height).toBeGreaterThan(box.width * 5)
    expect(box.x + box.width).toBeGreaterThan(390 - 24)
    await page.waitForFunction(() => (document.querySelector('.browse-grid')?.getBoundingClientRect().height ?? 0) > 3000)
    await rail.getByRole('button', { name: 'Jump to T' }).click()
    await expect(page.locator('.browse-card').filter({ hasText: 'T-190' })).toBeInViewport()
  })

  for (const [width, height] of [
    [390, 800],
    [320, 568],
  ]) {
    for (const hash of ['/#/albums', '/#/artists']) {
      test(`on a ${width}px phone no tile control sits under the rail on ${hash.slice(3)}, and its last letter is above the player bar`, async ({ page }) => {
        await page.setViewportSize({ width, height })
        await installApiMock(page)
        if (hash.endsWith('artists')) await seedArtists(page)
        await page.goto(hash)
        await expect(page.locator('.browse-card').first()).toBeVisible()
        const rail = page.locator('.alphabet-jump-bar-inner')
        await expect(rail).toBeVisible()
        const railBox = (await rail.boundingBox())!
        const mores = page.locator('.more-card-wrap > button:not(.browse-card)')
        await expect.poll(() => mores.count()).toBeGreaterThanOrEqual(3)
        const count = await mores.count()
        for (let i = 0; i < count; i++) {
          const more = (await mores.nth(i).boundingBox())!
          expect(more.x + more.width, `more button ${i} ends before the rail`).toBeLessThanOrEqual(railBox.x)
        }
        const bar = (await page.locator('.player-bar').boundingBox())!
        expect(railBox.y + railBox.height).toBeLessThanOrEqual(bar.y)
        // On a screen too short for 27 letters the strip scrolls; reach the end of it.
        const last = rail.getByRole('button', { name: 'Jump to Z' })
        await last.scrollIntoViewIfNeeded()
        const lastBox = (await last.boundingBox())!
        expect(lastBox.y + lastBox.height).toBeLessThanOrEqual(bar.y)
        expect(lastBox.y).toBeGreaterThanOrEqual(railBox.y)
        expect(lastBox.width).toBeGreaterThanOrEqual(24)
        expect(lastBox.height).toBeGreaterThanOrEqual(width === 390 ? 22 : 24)
        if (width === 390) {
          // All 27 fit without scrolling.
          expect(await rail.evaluate((el) => el.scrollHeight - el.clientHeight)).toBeLessThanOrEqual(1)
        }
      })
    }
  }

  test('Songs jumps by artist and reads later pages to reach a letter', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 })
    const mock = await installApiMock(page)
    const rows = songs(mock, 300, (index) => `${String.fromCharCode(65 + Math.floor(index / 12))} artist`)
    await page.route(/\/api\/library\/tracks(\?|$)/, (route) => {
      const params = new URL(route.request().url()).searchParams
      const offset = Number(params.get('offset') ?? 0)
      const limit = Number(params.get('limit') ?? 100)
      return route.fulfill({ json: rows.slice(offset, offset + limit) })
    })
    await page.goto('/#/songs')
    await expect(page.getByRole('button', { name: 'Play Song 0' })).toBeVisible()
    // Only A-I are loaded (100 rows, 12 per letter); Y is further on but enabled.
    const letterY = page.getByRole('button', { name: 'Jump to Y' })
    await expect(letterY).toBeEnabled()
    await letterY.click()
    // Y artist starts at index 288 (24 * 12).
    await expect(page.getByRole('button', { name: 'Play Song 288' })).toBeInViewport()
  })
})

function songs(mock: AppMockState, total: number, artistFor: (index: number) => string = () => 'Artist A'): MediaItem[] {
  return Array.from({ length: total }, (_, index) => ({
    ...mock.tracks[0],
    id: 1000 + index,
    title: `Song ${index}`,
    artist: artistFor(index),
    artists: undefined,
    album: `Album ${index}`,
    album_artist: null,
    is_favorite: false,
    is_disliked: false,
    excluded_by: null,
  }))
}

async function seedSongs(page: Page, mock: AppMockState, total: number) {
  const rows = songs(mock, total)
  const offsets: number[] = []
  await page.route(/\/api\/library\/tracks(\?|$)/, (route) => {
    const params = new URL(route.request().url()).searchParams
    const offset = Number(params.get('offset') ?? 0)
    offsets.push(offset)
    const limit = Number(params.get('limit') ?? 100)
    return route.fulfill({ json: rows.slice(offset, offset + limit) })
  })
  return offsets
}

test.describe('Songs view and density', () => {
  test('Table / Cards is remembered on this device', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 })
    await installApiMock(page)
    await page.goto('/#/songs')
    const table = page.getByRole('button', { name: 'Table', exact: true })
    const cards = page.getByRole('button', { name: 'Cards', exact: true })
    await expect(table).toHaveAttribute('aria-pressed', 'true')
    await expect(page.locator('.track-table-head')).toBeVisible()
    await cards.click()
    await expect(cards).toHaveAttribute('aria-pressed', 'true')
    await expect(page.locator('.track-list-cards .track-card')).toHaveCount(3)
    await expect(page.locator('.track-table-head')).toHaveCount(0)
    // Two cards side by side on a desktop-width screen.
    const [a, b] = await page.locator('.track-card').evaluateAll((els) => els.slice(0, 2).map((el) => el.getBoundingClientRect().top))
    expect(Math.abs(a - b)).toBeLessThan(1)
    await page.reload()
    await expect(page.getByRole('button', { name: 'Cards', exact: true })).toHaveAttribute('aria-pressed', 'true')
    await expect(page.locator('.track-list-cards')).toBeVisible()
    await page.getByRole('button', { name: 'Table', exact: true }).click()
    await page.reload()
    await expect(page.locator('.track-table-head')).toBeVisible()
  })

  test('cards are one column on a phone', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 800 })
    await installApiMock(page)
    await page.goto('/#/songs')
    // Phones always get cards: there is no Table/Cards choice to make.
    await expect(page.locator('.track-card').nth(1)).toBeVisible()
    await expect(page.getByRole('button', { name: 'Cards', exact: true })).toHaveCount(0)
    const tops = await page.locator('.track-card').evaluateAll((els) => els.map((el) => el.getBoundingClientRect().top))
    expect(tops[1]).toBeGreaterThan(tops[0] + 20)
  })

  test('Compact makes rows shorter, is remembered, and the long list still lines up far down', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 })
    const mock = await installApiMock(page)
    const requested = await seedSongs(page, mock, 300)
    await page.goto('/#/songs')
    const firstRow = page.locator('.track-row').first()
    await expect(firstRow).toBeVisible()
    const comfortable = (await firstRow.boundingBox())!.height
    const compact = page.getByRole('button', { name: 'Compact', exact: true })
    await compact.click()
    await expect(compact).toHaveAttribute('aria-pressed', 'true')
    await expect(page.locator('html')).toHaveAttribute('data-density', 'compact')
    await expect.poll(async () => (await firstRow.boundingBox())!.height).toBeLessThan(comfortable - 6)
    const rowHeight = (await firstRow.boundingBox())!.height

    // Far down (past the first page), every rendered row sits exactly where
    // its index says, so the virtual window and its spacers agree.
    await main(page).evaluate((el) => el.scrollTo(0, el.scrollHeight))
    await expect.poll(() => requested.includes(100)).toBe(true)
    await main(page).evaluate((el) => el.scrollTo(0, 4000))
    await page.waitForTimeout(200)
    const offsets = await page.evaluate(() => {
      const list = document.querySelector('.songs-view .track-list')!
      const top = list.getBoundingClientRect().top
      return [...list.querySelectorAll('.track-row')].map((row) => {
        const index = Number(row.querySelector('.track-title')!.textContent!.replace('Song ', ''))
        return { index, offset: row.getBoundingClientRect().top - top }
      })
    })
    expect(offsets.length).toBeGreaterThan(5)
    expect(offsets[0].index).toBeGreaterThan(20)
    for (const { index, offset } of offsets) expect(Math.abs(offset - index * rowHeight)).toBeLessThan(2)

    await page.reload()
    await expect(page.locator('html')).toHaveAttribute('data-density', 'compact')
    await expect(page.getByRole('button', { name: 'Compact', exact: true })).toHaveAttribute('aria-pressed', 'true')
  })
})

test.describe('Customise Home', () => {
  const titles = (page: Page) => page.locator('.home-rail .home-section-title').allTextContents()

  test('reorders and hides sections, and remembers it', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/home')
    await expect(page.getByRole('region', { name: 'Recently played' })).toBeVisible()
    await page.getByRole('button', { name: 'Customise Home' }).click()
    const dialog = page.getByRole('dialog', { name: 'Customise Home' })
    await expect(dialog.getByRole('button', { name: /^Move / })).toHaveCount(0)
    await dialog.getByRole('button', { name: 'Reorder Your radio stations' }).focus()
    await page.keyboard.press('ArrowUp')
    await page.keyboard.press('ArrowUp')
    await dialog.getByRole('switch', { name: 'Show Discover new artists' }).click()
    await dialog.getByRole('button', { name: 'Done' }).click()
    await expect(dialog).toHaveCount(0)
    const expected = ['Recently played', 'Recommended from your library', 'Your radio stations', 'Mixes for you']
    await expect.poll(() => titles(page)).toEqual(expected)
    await page.reload()
    await expect.poll(() => titles(page)).toEqual(expected)

    await page.getByRole('button', { name: 'Customise Home' }).click()
    await page.getByRole('dialog', { name: 'Customise Home' }).getByRole('button', { name: 'Reset to default' }).click()
    await page.getByRole('dialog', { name: 'Customise Home' }).getByRole('button', { name: 'Done' }).click()
    await expect.poll(() => titles(page)).toEqual([
      'Recently played',
      'Recommended from your library',
      'Discover new artists',
      'Mixes for you',
      'Your radio stations',
    ])
  })

  test('a hidden section asks the server for nothing', async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem(
        'atriolyric.settings',
        JSON.stringify({ homeSections: { order: ['recent', 'recommended', 'discover', 'mixes', 'radio'], hidden: ['mixes', 'recent'] } }),
      )
    })
    const mock = await installApiMock(page)
    await page.goto('/#/home')
    await expect(page.getByRole('region', { name: 'Your radio stations' })).toBeVisible()
    await expect(page.getByRole('region', { name: 'Mixes for you' })).toHaveCount(0)
    await expect(page.getByRole('region', { name: 'Recently played' })).toHaveCount(0)
    expect(mock.calls.some((call) => call.path === '/api/library/mixes')).toBe(false)
    expect(mock.calls.some((call) => call.path === '/api/library/recently-played-sources')).toBe(false)
  })
})
