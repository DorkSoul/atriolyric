import type { Page } from 'playwright/test'
import { expect, test } from './fixtures'
import { installApiMock, type AppMockState, type MediaItem } from './mockApi'

// The Songs tab's sort (A-Z by artist, Title, Album, Year, Date added, Most
// played, Last played), its Year and Plays columns, the A-Z jump per sort,
// and the queue following the order on screen.

const main = (page: Page) => page.locator('.app-main')

// In the server's default (artist) order. `date_added` is the id in the mock.
const LIBRARY: Array<Partial<MediaItem> & { id: number; title: string }> = [
  { id: 13, title: 'charlie', artist: 'Band A', album: 'First', year: 1975, play_count: 3, last_played: 100 },
  { id: 14, title: 'Bravo', artist: 'Band A', album: 'First', year: 1975, play_count: 0 },
  { id: 12, title: 'Alpha', artist: 'Band B', album: 'Second', year: 2005, play_count: 0 },
  { id: 11, title: 'delta', artist: 'Band B', album: 'Second', year: 2005, play_count: 1, last_played: 300 },
  { id: 15, title: 'echo', artist: 'Zed', album: null, year: null, play_count: 2, last_played: 200 },
]

// What each option asks for, and the order that comes back.
const SORTS: Array<{ option: string; button: string; params: string | null; titles: string[] }> = [
  { option: 'Title', button: 'Title', params: 'sort=title&direction=asc', titles: ['Alpha', 'Bravo', 'charlie', 'delta', 'echo'] },
  { option: 'Album', button: 'Album', params: 'sort=album&direction=asc', titles: ['charlie', 'Bravo', 'Alpha', 'delta', 'echo'] },
  { option: 'Year', button: 'Year', params: 'sort=year&direction=desc', titles: ['Alpha', 'delta', 'charlie', 'Bravo', 'echo'] },
  { option: 'Date added', button: 'Date added', params: 'sort=date_added&direction=desc', titles: ['echo', 'Bravo', 'charlie', 'Alpha', 'delta'] },
  { option: 'Most played', button: 'Most played', params: 'sort=most_played&direction=desc', titles: ['charlie', 'echo', 'delta', 'Bravo', 'Alpha'] },
  { option: 'Last played', button: 'Last played', params: 'sort=last_played&direction=desc', titles: ['delta', 'echo', 'charlie', 'Bravo', 'Alpha'] },
  { option: 'A–Z by artist', button: 'A–Z', params: null, titles: ['charlie', 'Bravo', 'Alpha', 'delta', 'echo'] },
]

async function openSongs(page: Page, width = 1440, height = 900): Promise<AppMockState> {
  await page.setViewportSize({ width, height })
  const mock = await installApiMock(page)
  const template = mock.tracks.find((track) => track.kind === 'track')!
  mock.tracks = LIBRARY.map((values) => ({
    ...template,
    artists: undefined,
    album_artist: null,
    genre: 'Rock',
    ...values,
  }))
  await page.goto('/#/songs')
  await expect(page.getByRole('button', { name: 'Play charlie' })).toBeVisible()
  return mock
}

function titles(page: Page) {
  return page
    .locator('.songs-view .track-play-btn')
    .evaluateAll((buttons) => buttons.map((b) => (b.getAttribute('aria-label') ?? '').replace(/^Play /, '')))
}

function lastTracksQuery(mock: AppMockState): URLSearchParams {
  const call = mock.calls.filter((c) => c.path === '/api/library/tracks').at(-1)!
  return new URL(call.url, 'http://mock.test').searchParams
}

async function pickSort(page: Page, option: string) {
  await page.getByRole('button', { name: /^Sort: / }).click()
  await page.getByRole('menuitemradio', { name: option, exact: true }).click()
}

test('the default order and request are unchanged', async ({ page }) => {
  const mock = await openSongs(page)
  expect(await titles(page)).toEqual(['charlie', 'Bravo', 'Alpha', 'delta', 'echo'])
  const params = lastTracksQuery(mock)
  expect(params.has('sort')).toBe(false)
  expect(params.has('direction')).toBe(false)
  await expect(page.getByRole('button', { name: 'Sort: A–Z' })).toHaveAttribute('title', 'Sorted by A–Z by artist, ascending')
})

test('each sort asks the server for it and shows its order', async ({ page }) => {
  const mock = await openSongs(page)
  for (const { option, button, params, titles: expected } of SORTS) {
    await pickSort(page, option)
    await expect(page.getByRole('button', { name: `Sort: ${button}` })).toBeVisible()
    await expect.poll(() => titles(page)).toEqual(expected)
    const query = lastTracksQuery(mock)
    if (params) {
      for (const [key, value] of new URLSearchParams(params)) expect(query.get(key)).toBe(value)
    } else {
      expect(query.has('sort')).toBe(false)
    }
  }
})

test('picking the current sort again flips its direction, and the sort is remembered', async ({ page }) => {
  const mock = await openSongs(page)
  await pickSort(page, 'Year')
  const toggle = page.getByRole('button', { name: 'Sort: Year' })
  await expect(toggle).toHaveAttribute('title', 'Sorted by Year, descending')
  await pickSort(page, 'Year')
  await expect(toggle).toHaveAttribute('title', 'Sorted by Year, ascending')
  await expect.poll(() => lastTracksQuery(mock).get('direction')).toBe('asc')
  await expect.poll(() => titles(page)).toEqual(['charlie', 'Bravo', 'Alpha', 'delta', 'echo'])
  // The open menu marks the current sort with its direction.
  await toggle.click()
  await expect(page.getByRole('menuitemradio', { name: 'Year', exact: true })).toHaveAttribute('aria-checked', 'true')
  await page.keyboard.press('Escape')

  await page.reload()
  await expect(page.getByRole('button', { name: 'Sort: Year' })).toHaveAttribute('title', 'Sorted by Year, ascending')
  await expect.poll(() => lastTracksQuery(mock).get('sort')).toBe('year')
  await expect.poll(() => titles(page)).toEqual(['charlie', 'Bravo', 'Alpha', 'delta', 'echo'])
  // The Table / Cards choice shares the stored preference and survives.
  await page.getByRole('button', { name: 'Cards', exact: true }).click()
  await page.reload()
  await expect(page.getByRole('button', { name: 'Cards', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByRole('button', { name: 'Sort: Year' })).toBeVisible()
})

test('Year and Plays columns on a wide screen, with "–" for none', async ({ page }) => {
  await openSongs(page, 1440)
  const head = page.locator('.track-table-head')
  await expect(head).toContainText('Year')
  await expect(head).toContainText('Plays')
  const row = (title: string) => page.locator('.track-row', { has: page.getByRole('button', { name: `Play ${title}` }) })
  await expect(row('charlie').locator('.track-col-year')).toHaveText('Year 1975')
  await expect(row('charlie').locator('.track-col-plays')).toHaveText('Plays 3')
  await expect(row('echo').locator('.track-col-year')).toHaveText('Year –')
  await expect(row('Bravo').locator('.track-col-plays')).toHaveText('Plays –')
  // Right-aligned, in tabular figures.
  const style = await row('charlie').locator('.track-col-year').evaluate((el) => {
    const s = getComputedStyle(el)
    return { align: s.textAlign, numeric: s.fontVariantNumeric }
  })
  expect(style).toEqual({ align: 'right', numeric: 'tabular-nums' })
  // Header and rows share the grid: the Year label sits over the year cells.
  const label = await head.getByText('Year', { exact: true }).boundingBox()
  const cell = await row('charlie').locator('.track-col-year').boundingBox()
  expect(Math.abs(label!.x + label!.width - (cell!.x + cell!.width))).toBeLessThan(2)

  // Cards have no such columns.
  await page.getByRole('button', { name: 'Cards', exact: true }).click()
  await expect(page.locator('.track-col-year')).toHaveCount(0)
})

test('no Year or Plays columns on a phone', async ({ page }) => {
  await openSongs(page, 390, 844)
  await expect(page.locator('.track-col-year')).toHaveCount(0)
  await expect(page.locator('.track-col-plays')).toHaveCount(0)
  // The sort is one button there too, opening the same menu.
  await pickSort(page, 'Most played')
  await expect.poll(() => titles(page)).toEqual(['charlie', 'echo', 'delta', 'Bravo', 'Alpha'])
})

test('Recently played keeps its columns', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await installApiMock(page)
  await page.goto('/#/recent-played')
  await expect(page.locator('.track-table-head')).toBeVisible()
  await expect(page.locator('.track-table-head')).not.toContainText('Year')
  await expect(page.locator('.track-col-year')).toHaveCount(0)
})

test.describe('A-Z jump per sort', () => {
  for (const width of [1440, 390]) {
    test(`shown for the alphabetical sorts and hidden for the others at ${width}px`, async ({ page }) => {
      await openSongs(page, width, 844)
      const rail = page.getByRole('group', { name: 'Jump to letter' })
      await expect(rail).toHaveCount(1)
      for (const [option, shown] of [
        ['Title', true],
        ['Year', false],
        ['Album', true],
        ['Date added', false],
        ['Most played', false],
        ['Last played', false],
        ['A–Z by artist', true],
      ] as const) {
        await pickSort(page, option)
        await expect(rail).toHaveCount(shown ? 1 : 0)
      }
    })
  }

  test('by title, it reads later pages to reach a letter, in either direction', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 })
    const mock = await installApiMock(page)
    const template = mock.tracks.find((track) => track.kind === 'track')!
    // 300 titles, 12 per letter A..Y; the route answers in title order.
    const rows: MediaItem[] = Array.from({ length: 300 }, (_, index) => ({
      ...template,
      id: 1000 + index,
      title: `${String.fromCharCode(65 + Math.floor(index / 12))} song ${index}`,
      artist: 'Someone',
      artists: undefined,
      album_artist: null,
    }))
    await page.route(/\/api\/library\/tracks(\?|$)/, (route) => {
      const params = new URL(route.request().url()).searchParams
      const ordered = params.get('direction') === 'desc' ? [...rows].reverse() : rows
      const offset = Number(params.get('offset') ?? 0)
      const limit = Number(params.get('limit') ?? 100)
      return route.fulfill({ json: ordered.slice(offset, offset + limit) })
    })
    await page.goto('/#/songs')
    await pickSort(page, 'Title')
    await expect(page.getByRole('button', { name: 'Play A song 0' })).toBeVisible()
    await page.getByRole('button', { name: 'Jump to Y' }).click()
    await expect(page.getByRole('button', { name: 'Play Y song 288' })).toBeInViewport()

    // Z-A: the letters before the loaded ones are the pending ones.
    await pickSort(page, 'Title')
    await expect(page.getByRole('button', { name: 'Play Y song 299' })).toBeVisible()
    const letterA = page.getByRole('button', { name: 'Jump to A' })
    await expect(letterA).toBeEnabled()
    await letterA.click()
    await expect(page.getByRole('button', { name: 'Play A song 11' })).toBeInViewport()
  })
})

test('a new sort starts at the top, and a slower reply for the old sort never replaces it', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 })
  const mock = await installApiMock(page)
  const template = mock.tracks.find((track) => track.kind === 'track')!
  const rows = (prefix: string): MediaItem[] =>
    Array.from({ length: 100 }, (_, index) => ({
      ...template,
      id: 2000 + index,
      title: `${prefix} ${index}`,
      artists: undefined,
      album_artist: null,
    }))
  let releaseYear!: () => void
  const yearReleased = new Promise<void>((resolve) => { releaseYear = resolve })
  let markYearDelivered!: () => void
  const yearDelivered = new Promise<void>((resolve) => { markYearDelivered = resolve })
  await page.route(/\/api\/library\/tracks(\?|$)/, async (route) => {
    const params = new URL(route.request().url()).searchParams
    const sort = params.get('sort') ?? 'artist'
    if (sort === 'year') {
      await yearReleased
      await route.fulfill({ json: rows('Stale year') }).catch(() => {})
      markYearDelivered()
      return
    }
    await route.fulfill({ json: Number(params.get('offset') ?? 0) > 0 ? [] : rows(sort === 'title' ? 'By title' : 'By artist') })
  })
  await page.goto('/#/songs')
  await expect(page.getByRole('button', { name: 'Play By artist 0' })).toBeVisible()
  await main(page).evaluate((el) => el.scrollTo(0, 2500))
  await expect.poll(() => main(page).evaluate((el) => el.scrollTop)).toBeGreaterThan(1000)

  const yearRequest = page.waitForRequest((request) => new URL(request.url()).searchParams.get('sort') === 'year')
  await pickSort(page, 'Year')
  await yearRequest
  expect(await main(page).evaluate((el) => el.scrollTop)).toBe(0)
  await pickSort(page, 'Title')
  await expect(page.getByRole('button', { name: 'Play By title 0' })).toBeInViewport()
  releaseYear()
  await yearDelivered
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
  await expect(page.getByRole('button', { name: 'Play Stale year 0' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Play By title 0' })).toBeVisible()
})

test('playing the third row after sorting by year queues in the order shown', async ({ page }) => {
  const mock = await openSongs(page)
  await pickSort(page, 'Year')
  await expect.poll(() => titles(page)).toEqual(['Alpha', 'delta', 'charlie', 'Bravo', 'echo'])
  await page.getByRole('button', { name: 'Play charlie' }).click()
  await expect
    .poll(() => mock.wsMessages.findLast((message) => message.type === 'set_queue_context') ?? null)
    .toMatchObject({ context: { kind: 'library', sort: 'year', direction: 'desc' }, start_media_item_id: 13 })
  // The mock queues the context the way the server does: in that order.
  expect(mock.playback.queue_media_item_ids).toEqual([12, 11, 13, 14, 15])
  expect(mock.playback.queue_index).toBe(2)
})

test('the default order queues with no sort, as before', async ({ page }) => {
  const mock = await openSongs(page)
  await page.getByRole('button', { name: 'Play Alpha' }).click()
  await expect.poll(() => mock.wsMessages.findLast((message) => message.type === 'set_queue_context') ?? null).not.toBeNull()
  const context = mock.wsMessages.findLast((message) => message.type === 'set_queue_context')!.context as Record<string, unknown>
  expect(context).toEqual({ kind: 'library', filters: {} })
})

test('a genre filter and a search combine with the sort', async ({ page }) => {
  const mock = await openSongs(page)
  await pickSort(page, 'Most played')
  await page.getByRole('button', { name: /^Genre/ }).click()
  await page.getByRole('menuitemradio', { name: 'Rock', exact: true }).click()
  await expect.poll(() => {
    const query = lastTracksQuery(mock)
    return [query.get('genre'), query.get('sort'), query.get('direction')]
  }).toEqual(['Rock', 'most_played', 'desc'])
  await expect.poll(() => titles(page)).toEqual(['charlie', 'echo', 'delta', 'Bravo', 'Alpha'])
})

test.describe('no sideways scrolling on Songs', () => {
  for (const width of [1440, 1024, 800, 390, 320]) {
    test(`at ${width}px in table, cards and compact`, async ({ page }) => {
      await openSongs(page, width, 800)
      await pickSort(page, 'Last played')
      const overflow = () =>
        page.evaluate(() => {
          const main = document.querySelector('.app-main')!
          return Math.max(
            document.documentElement.scrollWidth - document.documentElement.clientWidth,
            main.scrollWidth - main.clientWidth,
          )
        })
      expect(await overflow()).toBeLessThanOrEqual(0)
      await page.getByRole('button', { name: 'Compact', exact: true }).click()
      await expect(page.locator('html')).toHaveAttribute('data-density', 'compact')
      expect(await overflow()).toBeLessThanOrEqual(0)
      // A phone always shows cards (no Table / Cards chips there).
      if (width >= 640) await page.getByRole('button', { name: 'Cards', exact: true }).click()
      await expect(page.locator('.track-list-cards')).toBeVisible()
      expect(await overflow()).toBeLessThanOrEqual(0)
    })
  }
})
