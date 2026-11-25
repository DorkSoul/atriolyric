import { type Page, type Route } from 'playwright/test'
import { expect, test } from './fixtures'
import { installApiMock, type AppMockState, type MediaItem } from './mockApi'

// Lists and detail views keep what they showed: no stale rows from another
// album, one request per typed word, a refresh that does not throw the list
// away, and Back landing where the user was.

const TRACKS = /\/api\/library\/tracks(\?|$)/
const ALBUMS = /\/api\/library\/albums(\?|$)/

function trackRow(mock: AppMockState, id: number, title: string, extra: Partial<MediaItem> = {}): MediaItem {
  return { ...mock.tracks[0], id, title, ...extra }
}

/** A library of `total` songs, "Song 0"…, each on its own album, served in pages. */
async function seedSongs(page: Page, mock: AppMockState, total: number) {
  const rows = Array.from({ length: total }, (_, index) =>
    trackRow(mock, 1000 + index, `Song ${index}`, { artist: 'Artist A', album: `Album ${index}`, artists: undefined, album_artist: null }),
  )
  const requests: { offset: number; limit: number; q: string | null; album: string | null }[] = []
  await page.route(TRACKS, (route) => {
    const params = new URL(route.request().url()).searchParams
    const offset = Number(params.get('offset') ?? 0)
    const limit = Number(params.get('limit') ?? 100)
    const album = params.get('album')
    requests.push({ offset, limit, q: params.get('q'), album })
    if (album) {
      const row = rows.find((candidate) => candidate.album === album)
      return route.fulfill({ json: row ? [row] : [] })
    }
    return route.fulfill({ json: rows.slice(offset, offset + limit) })
  })
  return { rows, requests }
}

const main = (page: Page) => page.locator('.app-main')
const scrollTopOf = (page: Page) => main(page).evaluate((el) => el.scrollTop)
/** Scrolls the page's scroller to `top` (retrying while a virtual list is
 * still growing, and waiting for rows to render there). */
async function scrollMainTo(page: Page, top: number) {
  await expect
    .poll(async () => main(page).evaluate((el, value) => (el.scrollTo(0, value), el.scrollTop), top))
    .toBeGreaterThan(top - 2)
}
/** The text of the first element matching `selector` that is fully inside the
 * scroller's viewport. Clicking by index would make Playwright scroll a row
 * that is only rendered as overscan into view first, moving the position the
 * test is about. */
async function firstVisibleText(page: Page, selector: string) {
  let text: string | null = null
  await expect
    .poll(async () => {
      text = await main(page).evaluate((scroller, css) => {
        const box = scroller.getBoundingClientRect()
        // Rows under the sticky A-Z row or selection bar are covered, not visible.
        const sticky = [...scroller.querySelectorAll('.page-toolbar:has(.alphabet-jump-row), .bulk-action-bar')]
        const top = Math.max(box.top, ...sticky.map((el) => el.getBoundingClientRect().bottom))
        const hit = [...scroller.querySelectorAll(css)].find((el) => {
          const rect = el.getBoundingClientRect()
          return rect.height > 0 && rect.top >= top && rect.bottom <= box.bottom
        })
        return hit?.textContent?.trim() ?? null
      }, selector)
      return text
    }, { message: `a visible ${selector}` })
    .not.toBeNull()
  return text!
}

/** Records the scroller's position at the moment of the next click, which is
 * what the app remembers (Playwright may nudge a target into view first). */
async function recordScrollAtClick(page: Page) {
  await main(page).evaluate((el) => {
    el.addEventListener('click', () => ((window as unknown as { __clickTop: number }).__clickTop = el.scrollTop), { capture: true, once: true })
  })
  return () => page.evaluate(() => (window as unknown as { __clickTop: number }).__clickTop)
}

async function settled(page: Page) {
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
}

test.describe('album and artist views never show another one\'s data', () => {
  test('a slow answer for album A does not land under album B, whatever the order', async ({ page }) => {
    const mock = await installApiMock(page)
    let releaseA!: () => void
    const gateA = new Promise<void>((resolve) => {
      releaseA = resolve
    })
    let answeredA!: () => void
    const aAnswered = new Promise<void>((resolve) => {
      answeredA = resolve
    })
    await page.route(TRACKS, async (route) => {
      const album = new URL(route.request().url()).searchParams.get('album')
      if (album === 'A Night at the Opera') {
        await gateA
        await route.fulfill({ json: [trackRow(mock, 901, 'Row of album A', { album })] })
        answeredA()
      } else if (album === 'News of the World') {
        await route.fulfill({ json: [trackRow(mock, 902, 'Row of album B', { album })] })
      } else {
        await route.fallback()
      }
    })
    const askedA = page.waitForRequest((request) => TRACKS.test(request.url()) && new URL(request.url()).searchParams.get('album') === 'A Night at the Opera')
    await page.goto('/#/albums/Queen/A%20Night%20at%20the%20Opera')
    await askedA
    await expect(page.locator('.app-main .loading')).toHaveText('Loading…')
    await page.evaluate(() => {
      window.location.hash = '#/albums/Queen/News%20of%20the%20World'
    })
    await expect(page.getByRole('button', { name: 'Play Row of album B' })).toBeVisible()
    releaseA()
    await aAnswered
    await settled(page)
    await expect(page.getByRole('button', { name: 'Play Row of album A' })).toHaveCount(0)
    await expect(page.locator('.collection-detail h2')).toHaveText('News of the World')
    await expect(page.getByRole('button', { name: 'Play Row of album B' })).toBeVisible()
  })

  test('album A\'s rows are gone the moment album B opens, not left under its header', async ({ page }) => {
    const mock = await installApiMock(page)
    let releaseB!: () => void
    const gateB = new Promise<void>((resolve) => {
      releaseB = resolve
    })
    await page.route(TRACKS, async (route) => {
      const album = new URL(route.request().url()).searchParams.get('album')
      if (album === 'A Night at the Opera') {
        await route.fulfill({ json: [trackRow(mock, 901, 'Row of album A', { album })] })
      } else if (album === 'News of the World') {
        await gateB
        await route.fulfill({ json: [trackRow(mock, 902, 'Row of album B', { album })] })
      } else {
        await route.fallback()
      }
    })
    await page.goto('/#/albums/Queen/A%20Night%20at%20the%20Opera')
    await expect(page.getByRole('button', { name: 'Play Row of album A' })).toBeVisible()
    await page.evaluate(() => {
      window.location.hash = '#/albums/Queen/News%20of%20the%20World'
    })
    await expect(page.locator('.app-main .loading')).toHaveText('Loading…')
    await expect(page.getByRole('button', { name: 'Play Row of album A' })).toHaveCount(0)
    releaseB()
    await expect(page.getByRole('button', { name: 'Play Row of album B' })).toBeVisible()
  })

  test('an artist opened from a link shows a loading subtitle, not "0 tracks · 0 albums", until it is looked up', async ({ page }) => {
    await installApiMock(page)
    // The artist list holds only Queen, so M83 is not in what the tab loaded.
    await page.route(/\/api\/library\/artists(\?|$)/, (route) =>
      route.fulfill({ json: [{ artist: 'Queen', track_count: 2, album_count: 2, representative_media_item_id: 1 }] }),
    )
    let release!: () => void
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    await page.route(/\/api\/library\/artist-summaries/, async (route) => {
      await gate
      await route.fallback()
    })
    await page.goto('/#/artists/M83')
    const subtitle = page.locator('.collection-detail-copy p').first()
    await expect(subtitle).toHaveText('Loading…')
    release()
    await expect(subtitle).toHaveText('1 track · 1 album')
  })

  test('track and album counts say "1 track" and "1 album"', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/albums/M83/Hurry%20Up%2C%20We%20Are%20Dreaming')
    const facts = page.locator('.collection-detail-facts')
    await expect(facts).toContainText('1 track')
    await expect(facts).not.toContainText('1 tracks')
  })
})

// The search boxes left: Home's whole-library search and the Files tab's own
// file and folder search (Songs, Albums, Artists and Playlists have none).
test.describe('searching waits for a pause in typing', () => {
  const cases = [
    { name: 'Home', hash: '/#/home', box: 'Search your library', url: /\/api\/library\/search\?/, ready: '.home-view' },
    { name: 'Files', hash: '/#/files/1', box: 'Search files and folders', url: /\/api\/library\/files\/search\?/, ready: '.files-list, .file-browser, [aria-label="Folders"]' },
  ]
  for (const { name, hash, box, url, ready } of cases) {
    test(`${name}: one request for a typed word`, async ({ page }) => {
      await page.clock.install()
      const mock = await installApiMock(page)
      await page.goto(hash)
      await expect(page.locator(ready).first()).toBeVisible()
      const input = page.getByLabel(box, { exact: true })
      await page.clock.pauseAt(new Date(Date.now() + 5_000))
      const searched = () =>
        [...new Set(mock.calls.filter((call) => url.test(call.url) && /[?&]q=/.test(call.url)).map((call) => new URL(call.url, 'http://x').searchParams.get('q')))]
      await input.pressSequentially('queen')
      await page.clock.runFor(200)
      expect(searched()).toEqual([])
      await page.clock.runFor(100)
      await expect.poll(searched).toEqual(['queen'])
    })
  }
})

test.describe('refreshing a list in place', () => {
  test('after a tag edit the Songs list keeps its rows and scroll position and refetches what was loaded', async ({ page }) => {
    const mock = await installApiMock(page)
    const { requests } = await seedSongs(page, mock, 300)
    await page.route(/\/api\/admin\/media\/\d+\/tags$/, (route) => route.fulfill({ json: { ok: true } }))
    await page.goto('/#/songs')
    await expect(page.getByRole('button', { name: 'Play Song 0' })).toBeVisible()
    // Reach the second page so two pages are loaded, then settle mid-list.
    await main(page).evaluate((el) => el.scrollTo(0, el.scrollHeight))
    await expect.poll(() => requests.some((request) => request.offset === 100)).toBe(true)
    await expect(page.locator('.loading-more')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Play Song 150' }).or(page.locator('.track-row').last())).toBeVisible()
    await scrollMainTo(page, 4_000)
    const before = await scrollTopOf(page)
    expect(before).toBeGreaterThan(3_000)
    const title = await firstVisibleText(page, '.track-row .track-title')
    const firstRow = page.locator('.track-row').filter({ has: page.getByRole('button', { name: `Play ${title}`, exact: true }) })
    const fetchesBefore = requests.length

    await firstRow.getByRole('button', { name: `More actions for ${title}` }).click()
    await page.getByRole('menuitem', { name: 'Edit tags' }).click()
    const dialog = page.getByRole('dialog', { name: 'Edit tags' })
    await dialog.getByLabel('Artist').fill('Someone Else')
    await dialog.getByRole('button', { name: 'Save' }).click()
    await expect(dialog).toHaveCount(0)

    // The two loaded pages came back (offset 0 and 100), nothing blanked, and the scroll held.
    await expect.poll(() => requests.slice(fetchesBefore).map((request) => request.offset)).toEqual([0, 100])
    await expect(page.locator('.app-main .loading')).toHaveCount(0)
    await expect(page.locator('.track-row').first()).toBeVisible()
    expect(Math.abs((await scrollTopOf(page)) - before)).toBeLessThan(3)
    expect(requests.slice(fetchesBefore).every((request) => request.limit <= 100)).toBe(true)
  })
})

test.describe('Back lands where the user was', () => {
  test('Songs, album link, Back: same scroll position and the list is not refetched', async ({ page }) => {
    const mock = await installApiMock(page)
    const { requests } = await seedSongs(page, mock, 300)
    await page.goto('/#/songs')
    await expect(page.getByRole('button', { name: 'Play Song 0' })).toBeVisible()
    await scrollMainTo(page, 3_000)
    const listRequests = requests.filter((request) => !request.album).length
    const albumName = await firstVisibleText(page, '.track-row a[href^="#/albums/"]')
    const clickTop = await recordScrollAtClick(page)
    await page.locator('.track-row a[href^="#/albums/"]').filter({ hasText: new RegExp(`^${albumName}$`) }).click()
    await expect(page.locator('.collection-detail h2')).toHaveText(albumName)
    await page.goBack()
    await expect(page.locator('.track-row').first()).toBeVisible()
    const before = await clickTop()
    expect(before).toBeGreaterThan(2_000)
    await expect.poll(async () => Math.abs((await scrollTopOf(page)) - before)).toBeLessThan(3)
    // The list was kept, not fetched again from page one.
    expect(requests.filter((request) => !request.album).length).toBe(listRequests)
  })

  test('Albums, open one, Back: same scroll position', async ({ page }) => {
    await installApiMock(page)
    const albums = (route: Route) =>
      route.fulfill({
        json: Array.from({ length: 300 }, (_, index) => ({
          artist: `Album Artist ${index}`,
          album: `Album ${index}`,
          track_count: 1,
          representative_media_item_id: 5000 + index,
        })),
      })
    await page.route(ALBUMS, albums)
    await page.goto('/#/albums')
    await expect(page.locator('.browse-card-album').first()).toBeVisible()
    await scrollMainTo(page, 3_000)
    const albumTitle = await firstVisibleText(page, '.browse-card-album .browse-title')
    const clickTop = await recordScrollAtClick(page)
    await page.locator('.browse-card-album').filter({ has: page.getByText(albumTitle, { exact: true }) }).click()
    await expect(page.locator('.collection-detail')).toBeVisible()
    await page.goBack()
    await expect(page.locator('.browse-card-album').first()).toBeVisible()
    const before = await clickTop()
    expect(before).toBeGreaterThan(2_000)
    await expect.poll(async () => Math.abs((await scrollTopOf(page)) - before)).toBeLessThan(3)
  })

  test('Albums, another tab, Back: same scroll position', async ({ page }) => {
    await installApiMock(page)
    await page.route(ALBUMS, (route) =>
      route.fulfill({
        json: Array.from({ length: 300 }, (_, index) => ({
          artist: `Album Artist ${index}`,
          album: `Album ${index}`,
          track_count: 1,
          representative_media_item_id: 5000 + index,
        })),
      }),
    )
    await page.goto('/#/albums')
    await expect(page.locator('.browse-card-album').first()).toBeVisible()
    await scrollMainTo(page, 3_000)
    // The full list swapping in can nudge the position (WebKit's scroll
    // anchoring): take the position the user is actually leaving from, once it
    // has stopped moving, not the one just set.
    let last = -1
    await expect
      .poll(async () => {
        const now = await scrollTopOf(page)
        const held = now === last
        last = now
        return held
      })
      .toBe(true)
    const before = last
    await page.getByRole('button', { name: 'Radio', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Radio' })).toBeVisible()
    await page.goBack()
    await expect(page.locator('.browse-card-album').first()).toBeVisible()
    await expect.poll(async () => Math.abs((await scrollTopOf(page)) - before)).toBeLessThan(3)
  })
})

test.describe('hidden tabs stay quiet', () => {
  test('a hidden Admin tab stops polling, and polls again once shown', async ({ page }) => {
    await page.clock.install()
    const mock = await installApiMock(page)
    await page.goto('/#/admin')
    await expect(page.getByRole('heading', { name: 'Users', exact: true })).toBeVisible()
    const adminCalls = () => mock.calls.filter((call) => call.path.startsWith('/api/admin/')).length
    // Shown: a minute of page time brings more requests (its refresh tick).
    const shown = adminCalls()
    await page.clock.runFor(61_000)
    await expect.poll(adminCalls).toBeGreaterThan(shown)

    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
    const hidden = adminCalls()
    await page.clock.runFor(61_000)
    await settled(page)
    expect(adminCalls()).toBe(hidden)
  })
})
