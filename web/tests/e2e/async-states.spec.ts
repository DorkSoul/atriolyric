import { type Page, type Route } from 'playwright/test'
import { answerDialog } from './dialogs'
import { expect, refusedWhileLeaving, test } from './fixtures'
import { installApiMock, mockStates } from './mockApi'
import { player, nowPlayingView } from './app-helpers'

// Every view that loads data has three distinct states besides its content:
// loading, empty, and failed-with-Retry. These tests break one request at a
// time (a route override registered after the mock, which wins) and check
// each state on its own - none of them may look like another.

type How = 'status500' | 'status502' | 'abort' | 'never'

/** Makes the matching requests fail until `heal()`; after that they reach the mock. */
async function breakRoute(
  page: Page,
  url: RegExp,
  how: How = 'status500',
  only: (route: Route) => boolean = () => true,
) {
  const control = { broken: true, hits: 0 }
  await page.route(url, async (route) => {
    if (!control.broken || !only(route)) return route.fallback()
    control.hits += 1
    if (how === 'abort') return route.abort('failed')
    if (how === 'never') return new Promise<void>(() => {})
    if (how === 'status502') {
      // What a proxy in front of a stopped server answers: not JSON.
      return route.fulfill({ status: 502, contentType: 'text/html', body: '<html>Bad gateway</html>' })
    }
    return route.fulfill({ status: 500, json: { detail: 'Something broke on the server.' } })
  })
  return {
    heal: () => {
      control.broken = false
    },
    hits: () => control.hits,
  }
}

/** Holds the matching requests until `release()`, then answers them with
 * `respond` (default: the mock's own answer). */
async function holdRoute(page: Page, url: RegExp, respond: (route: Route) => Promise<void> = (route) => route.fallback()) {
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route(url, async (route) => {
    await gate
    await respond(route)
  })
  return release
}

const TRACKS = /\/api\/library\/tracks(\?|$)/
const ALBUMS = /\/api\/library\/albums(\?|$)/
const ARTISTS = /\/api\/library\/artists(\?|$)/
const alert = (page: Page) => page.locator('.error-state')

test.describe('Songs', () => {
  test('a failed load shows the error with Retry (not "Nothing here yet."), and Retry recovers', async ({ page }) => {
    await installApiMock(page)
    const songs = await breakRoute(page, TRACKS, 'status502')
    await page.goto('/#/songs')
    await expect(alert(page)).toContainText('The server ran into a problem')
    // The raw "GET /api/… failed: 502" never reaches the user.
    await expect(alert(page)).not.toContainText('/api/')
    await expect(page.getByText('Nothing here yet.')).toHaveCount(0)
    songs.heal()
    await alert(page).getByRole('button', { name: 'Retry' }).click()
    await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
    await expect(alert(page)).toHaveCount(0)
  })

  test('loading shows Loading… and an empty library shows the empty state', async ({ page }) => {
    await installApiMock(page)
    const release = await holdRoute(page, TRACKS, (route) => route.fulfill({ json: [] }))
    await page.goto('/#/songs')
    await expect(page.locator('.app-main .loading')).toHaveText('Loading…')
    await expect(page.getByText('Nothing here yet.')).toHaveCount(0)
    release()
    await expect(page.getByText('Nothing here yet.')).toBeVisible()
    await expect(page.locator('.app-main .loading')).toHaveCount(0)
    await expect(alert(page)).toHaveCount(0)
  })

  test('a failed "load more" keeps the rows and offers another try', async ({ page }) => {
    await installApiMock(page)
    const many = Array.from({ length: 100 }, (_, index) => ({
      ...mockStates.get(page)!.tracks[0],
      id: 100 + index,
      title: `Song ${index}`,
    }))
    await page.route(TRACKS, (route) => {
      const offset = Number(new URL(route.request().url()).searchParams.get('offset') ?? 0)
      if (offset > 0) return route.fulfill({ status: 500, json: { detail: 'No more for you.' } })
      return route.fulfill({ json: many })
    })
    await page.goto('/#/songs')
    await expect(page.getByRole('button', { name: 'Play Song 0' })).toBeVisible()
    await page.locator('.app-main').evaluate((el) => el.scrollTo(0, el.scrollHeight))
    await expect(alert(page)).toContainText('Could not load more songs.')
    // The rows already read are still there (the list is virtualised: the last ones show).
    await expect(page.locator('.track-row').last()).toBeVisible()
  })
})

test.describe('Albums and Artists', () => {
  test('album list: a failed load shows Retry, Retry recovers', async ({ page }) => {
    await installApiMock(page)
    const albums = await breakRoute(page, ALBUMS)
    await page.goto('/#/albums')
    await expect(alert(page)).toContainText('Something broke on the server.')
    await expect(page.locator('.app-main .loading')).toHaveCount(0)
    albums.heal()
    await alert(page).getByRole('button', { name: 'Retry' }).click()
    await expect(page.locator('.browse-card-album').first()).toBeVisible()
  })

  test('album detail: loading is not "Nothing here yet.", a failure retries, an empty album says so', async ({ page }) => {
    await installApiMock(page)
    const release = await holdRoute(page, TRACKS)
    await page.goto('/#/albums/Queen/A%20Night%20at%20the%20Opera')
    await expect(page.locator('.app-main .loading')).toHaveText('Loading…')
    await expect(page.getByText('Nothing here yet.')).toHaveCount(0)
    release()
    await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
    await page.unroute(TRACKS)

    const failing = await breakRoute(page, TRACKS, 'abort')
    await page.goto('/#/albums/Queen/News%20of%20the%20World')
    await page.reload()
    await expect(alert(page)).toContainText('Could not reach the server')
    failing.heal()
    await alert(page).getByRole('button', { name: 'Retry' }).click()
    await expect(page.getByRole('button', { name: "Play Don't Stop Me Now" })).toBeVisible()

    await page.route(TRACKS, (route) => route.fulfill({ json: [] }))
    await page.goto('/#/albums/Queen/Live')
    await page.reload()
    await expect(page.getByText('Nothing here yet.')).toBeVisible()
  })

  test('a request the old page starts while a reload is under way is no error', async ({ page, browserName }) => {
    // WebKit refuses a fetch a page starts after a navigation away from it has
    // begun, and logs "Fetch API cannot load … due to access control checks."
    // The Albums grid asks for the full album list 100 ms after the first
    // page, so a reload in that gap can fail it on WebKit.
    // Here the reload's document is held back for a second so the old page's
    // timer always fires inside that window.
    await installApiMock(page)
    const reports: string[] = []
    page.on('request', (request) => {
      if (request.url().includes('/api/client-log')) reports.push(request.postData() ?? '')
    })
    let documents = 0
    await page.route((url) => url.pathname === '/' && url.port === '3000', async (route) => {
      documents += 1
      if (documents > 1) await new Promise((resolve) => setTimeout(resolve, 1000))
      await route.continue()
    })
    const firstPage = page.waitForResponse((response) => ALBUMS.test(response.url()) && !response.url().includes('full=1'))
    await page.goto('/#/albums')
    await firstPage
    await page.reload()
    await expect(page.locator('.browse-card').filter({ hasText: 'News of the World' })).toBeVisible()
    await expect(alert(page)).toHaveCount(0)
    if (browserName === 'webkit') {
      // The window really was hit: the old page's full-list request was refused.
      expect(refusedWhileLeaving.get(page)).toContain('http://127.0.0.1:3000/api/library/albums?sort=alphabetical&direction=asc&full=1')
    }
    // No error report either - sent, or tried by the page being left.
    expect(reports, 'client error reports').toEqual([])
    expect((refusedWhileLeaving.get(page) ?? []).filter((url) => url.includes('/api/client-log'))).toEqual([])
  })

  test('artist list: a failed load shows Retry, Retry recovers', async ({ page }) => {
    await installApiMock(page)
    const artists = await breakRoute(page, ARTISTS)
    await page.goto('/#/artists')
    await expect(alert(page)).toBeVisible()
    artists.heal()
    await alert(page).getByRole('button', { name: 'Retry' }).click()
    await expect(page.locator('.browse-card').first()).toBeVisible()
  })

  test('an artist\'s albums: a failed load shows Retry', async ({ page }) => {
    await installApiMock(page)
    const albums = await breakRoute(page, ALBUMS, 'status500', (route) => new URL(route.request().url()).searchParams.get('appears_on') !== 'true')
    await page.goto('/#/artists/Queen')
    await expect(alert(page)).toContainText('Something broke on the server.')
    albums.heal()
    await alert(page).getByRole('button', { name: 'Retry' }).click()
    await expect(page.locator('.browse-card-album').first()).toBeVisible()
  })

  test('an empty album list says so', async ({ page }) => {
    await installApiMock(page)
    await page.route(ALBUMS, (route) => route.fulfill({ json: [] }))
    await page.goto('/#/albums')
    await expect(page.getByText('No albums yet.')).toBeVisible()
  })
})

test.describe('Lyrics, playlists, recents', () => {
  test('lyrics: a failure shows Retry and Retry loads them', async ({ page }) => {
    await installApiMock(page)
    const lyrics = await breakRoute(page, /\/api\/library\/media\/1\/lyrics/)
    await page.goto('/#/songs')
    await page.getByRole('button', { name: 'More actions for Bohemian Rhapsody' }).click()
    await page.getByRole('menuitem', { name: 'View lyrics' }).click()
    const dialog = nowPlayingView(page)
    await expect(dialog.locator('.error-state')).toContainText('Something broke on the server.')
    lyrics.heal()
    await page.route(/\/api\/library\/media\/1\/lyrics/, (route) =>
      route.fulfill({ json: { plain: 'Is this the real life?', synced: null, source: 'embedded' } }),
    )
    await dialog.getByRole('button', { name: 'Retry' }).click()
    await expect(dialog).toContainText('Is this the real life?')
  })

  test('playlist detail: a failure offers Retry and a way back instead of "Loading playlist…" for ever', async ({ page }) => {
    await installApiMock(page)
    const detail = await breakRoute(page, /\/api\/playlists\/2$/)
    await page.goto('/#/playlists')
    await page.getByRole('button', { name: /Workout/ }).first().click()
    await expect(alert(page)).toContainText('Something broke on the server.')
    await expect(page.getByText('Loading playlist…')).toHaveCount(0)
    detail.heal()
    await alert(page).getByRole('button', { name: 'Retry' }).click()
    await expect(page.locator('.collection-detail h2')).toHaveText('Workout')
    await page.goBack()
    await expect(page.getByRole('heading', { name: 'Your playlists' })).toBeVisible()

    // And Back works from the error state itself.
    const again = await breakRoute(page, /\/api\/playlists\/2$/)
    await page.getByRole('button', { name: /Workout/ }).first().click()
    await expect(alert(page)).toBeVisible()
    await page.locator('.top-bar').getByRole('button', { name: /^(Back|Albums|Artists|Playlists|Home|Queen)$/ }).click()
    await expect(page.getByRole('heading', { name: 'Your playlists' })).toBeVisible()
    again.heal()
  })

  for (const [tab, label] of [['recent-played', 'Recently played']] as const) {
    test(`${label}: failure shows Retry, loading and empty are distinct`, async ({ page }) => {
      await installApiMock(page)
      const url = /\/api\/library\/recently-played(\?|$)/
      const recents = await breakRoute(page, url)
      await page.goto(`/#/${tab}`)
      await expect(alert(page)).toContainText('Something broke on the server.')
      recents.heal()
      await page.unroute(url)
      await page.route(url, (route) => route.fulfill({ json: [] }))
      await alert(page).getByRole('button', { name: 'Retry' }).click()
      await expect(page.getByText('Nothing here yet.')).toBeVisible()
    })
  }

  test('Library info: a slow earlier response never lands on the screen that replaced it', async ({ page }) => {
    await installApiMock(page)
    const track = (id: number, title: string) => ({ ...mockStates.get(page)!.tracks[0], id, title })
    let release!: () => void
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    await page.route(/\/api\/library\/stats$/, async (route) => {
      await gate
      await route.fulfill({ status: 500, json: { detail: 'late' } })
    })
    await page.route(/\/api\/library\/recently-played(\?|$)/, (route) => route.fulfill({ json: [track(902, 'Fast Played')] }))
    await page.goto('/#/library-info')
    // Recently played is hidden from the tab bar by default: navigate by hash.
    await page.evaluate(() => {
      window.location.hash = '#/recent-played'
    })
    await expect(page.getByRole('button', { name: 'Play Fast Played' })).toBeVisible()
    release()
    await expect(page.getByRole('button', { name: 'Play Fast Played' })).toBeVisible()
    await expect(alert(page)).toHaveCount(0)
  })
})

test.describe('Home, Videos, Radio, Files', () => {
  test('Home: a server that is down is not shown as an empty library', async ({ page }) => {
    await installApiMock(page)
    const homeRequests = [
      /\/api\/library\/recently-played-sources/,
      /\/api\/playlists$/,
      /\/api\/radio\/stations$/,
      /\/api\/library\/mixes$/,
    ]
    const broken = await Promise.all(homeRequests.map((url) => breakRoute(page, url, 'abort')))
    await page.goto('/#/home')
    await expect(alert(page)).toContainText('Could not reach the server')
    await expect(page.locator('.home-view').getByText('Recently played', { exact: true })).toHaveCount(0)
    broken.forEach((route) => route.heal())
    await alert(page).getByRole('button', { name: 'Retry' }).click()
    await expect(page.locator('.home-view').getByText('Recently played', { exact: true }).first()).toBeVisible()
  })

  test('Home: one failing rail does not hide the others, and says so', async ({ page }) => {
    await installApiMock(page)
    await breakRoute(page, /\/api\/library\/mixes$/, 'status500')
    await page.goto('/#/home')
    await expect(page.locator('.toast-error')).toContainText('Some of Home could not be loaded.')
    await expect(page.locator('.home-view').getByText('Recently played', { exact: true }).first()).toBeVisible()
    // Not the whole page's error: the one section says it failed, in its place.
    await expect(page.locator('.home-view > .error-state')).toHaveCount(0)
    await expect(alert(page)).toHaveCount(1)
    await expect(page.locator('.home-rail').filter({ hasText: 'Mixes for you' }).locator('.error-state')).toContainText('Something broke on the server.')
  })

  test('Videos: a failure shows Retry, Retry recovers', async ({ page }) => {
    await installApiMock(page)
    const videos = await breakRoute(page, /\/api\/library\/videos\/folders/)
    await page.goto('/#/videos')
    await expect(alert(page)).toContainText('Something broke on the server.')
    await expect(page.getByText('No videos yet.')).toHaveCount(0)
    videos.heal()
    await alert(page).getByRole('button', { name: 'Retry' }).click()
    await expect(page.getByRole('button', { name: /Shows/ })).toBeVisible()
  })

  test('Radio: loading, failure with Retry, and the server\'s message for a bad station', async ({ page }) => {
    await installApiMock(page)
    const get = (route: Route) => route.request().method() === 'GET'
    const stations = await breakRoute(page, /\/api\/radio\/stations$/, 'status500', get)
    await page.goto('/#/radio')
    await expect(alert(page)).toContainText('Something broke on the server.')
    stations.heal()
    await alert(page).getByRole('button', { name: 'Retry' }).click()
    await expect(page.getByRole('button', { name: 'Add station' })).toBeVisible()
    await expect(alert(page)).toHaveCount(0)

    await page.route(/\/api\/radio\/stations$/, (route) =>
      route.request().method() === 'POST'
        ? route.fulfill({ status: 400, json: { detail: 'That stream URL could not be reached.' } })
        : route.fallback(),
    )
    await page.getByLabel('Station name').fill('Bad radio')
    await page.getByLabel('Stream URL').fill('http://nope.invalid/stream')
    await page.getByRole('button', { name: 'Add station' }).click()
    await expect(page.getByRole('alert').filter({ hasText: 'That stream URL could not be reached.' })).toBeVisible()
    // The form keeps what was typed so it can be corrected.
    await expect(page.getByLabel('Stream URL')).toHaveValue('http://nope.invalid/stream')
  })

  test('Radio: loading shows Loading…', async ({ page }) => {
    await installApiMock(page)
    const release = await holdRoute(page, /\/api\/radio\/stations$/)
    await page.goto('/#/radio')
    await expect(page.locator('.radio-view .loading')).toHaveText('Loading…')
    release()
    await expect(page.getByRole('button', { name: 'Add station' })).toBeVisible()
    await expect(page.locator('.radio-view .loading')).toHaveCount(0)
  })

  test('Files: a failure shows Retry, Retry recovers', async ({ page }) => {
    await installApiMock(page)
    const browse = await breakRoute(page, /\/api\/library\/browse/)
    await page.goto('/#/files/1')
    await expect(alert(page)).toContainText('Something broke on the server.')
    await expect(page.getByText('No library folders available to browse.')).toHaveCount(0)
    browse.heal()
    await alert(page).getByRole('button', { name: 'Retry' }).click()
    await expect(page.locator('.files-list')).toBeVisible()
  })
})

test.describe('Admin', () => {
  const USERS = /\/api\/admin\/users$/
  const ROOTS = /\/api\/admin\/roots$/
  const usersHeading = (page: Page) => page.getByRole('heading', { name: 'Users', exact: true })

  test('a failed first load shows the error with Retry (not an empty Users list), and Retry recovers', async ({ page }) => {
    await installApiMock(page)
    const roots = await breakRoute(page, ROOTS)
    await page.goto('/#/admin')
    await expect(alert(page)).toContainText('Something broke on the server.')
    // Nothing that would read as "no users, no library paths yet".
    await expect(usersHeading(page)).toHaveCount(0)
    await expect(page.getByPlaceholder('Username')).toHaveCount(0)
    await expect(page.locator('.admin-root-row')).toHaveCount(0)
    roots.heal()
    await alert(page).getByRole('button', { name: 'Retry' }).click()
    await expect(usersHeading(page)).toBeVisible()
    await expect(page.locator('.admin-root-row').first()).toBeVisible()
    await expect(alert(page)).toHaveCount(0)
  })

  test('the first load shows Loading…, not empty lists', async ({ page }) => {
    await installApiMock(page)
    const release = await holdRoute(page, USERS)
    await page.goto('/#/admin')
    await expect(page.locator('.admin-panel > .loading')).toBeVisible()
    await expect(usersHeading(page)).toHaveCount(0)
    release()
    await expect(usersHeading(page)).toBeVisible()
    await expect(page.locator('.admin-panel > .loading')).toHaveCount(0)
  })

  test('a failed refresh after that keeps what is on screen, quietly', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/admin')
    await expect(page.locator('.admin-root-row').first()).toBeVisible()
    const users = await breakRoute(page, USERS)
    // The page asks again when it becomes visible.
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
    await expect.poll(() => users.hits()).toBeGreaterThan(0)
    await expect(page.locator('.admin-root-row').first()).toBeVisible()
    await expect(usersHeading(page)).toBeVisible()
    await expect(alert(page)).toHaveCount(0)
    await expect(page.locator('.toast-error')).toHaveCount(0)
  })
})

test.describe('timeouts', () => {
  test('a request that never answers fails after 30 seconds with a clear message, and Retry works', async ({ page }) => {
    await page.clock.install()
    await installApiMock(page)
    const songs = await breakRoute(page, TRACKS, 'never')
    await page.goto('/#/songs')
    await expect(page.locator('.app-main .loading')).toHaveText('Loading…')
    await page.clock.runFor(29_000)
    await expect(page.locator('.app-main .loading')).toHaveText('Loading…')
    await page.clock.runFor(2_000)
    await expect(alert(page)).toContainText('The server took too long to respond')
    songs.heal()
    await alert(page).getByRole('button', { name: 'Retry' }).click()
    await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
  })

  test('a timed-out browse request does not leave later callers waiting on it', async ({ page }) => {
    await page.clock.install()
    await installApiMock(page)
    const albums = await breakRoute(page, ALBUMS, 'never')
    // The clock may only run once the request is out: "Loading…" is also what
    // shows while the view's own code is still arriving, and time run then
    // is over before the request's timeout has even been set.
    const asked = page.waitForRequest((request) => ALBUMS.test(request.url()))
    await page.goto('/#/albums')
    await asked
    await expect(page.locator('.app-main .loading')).toHaveText('Loading…')
    await page.clock.runFor(31_000)
    await expect(alert(page)).toContainText('The server took too long to respond')
    albums.heal()
    // Retry starts a fresh request: it would hang forever if the dead one were still shared.
    await alert(page).getByRole('button', { name: 'Retry' }).click()
    await expect(page.locator('.browse-card-album').first()).toBeVisible()
  })
})

test.describe('error boundary', () => {
  test('a view that throws shows the fallback; the player keeps playing and navigating away recovers', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/songs')
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')

    // A playlist whose items are missing makes PlaylistsView throw while rendering.
    await page.route(/\/api\/playlists\/2$/, (route) =>
      route.fulfill({ json: { id: 2, name: 'Broken', description: null, created_at: '2026-01-01T00:00:00Z', items: null } }),
    )
    const reports: string[] = []
    await page.route('**/api/client-log', async (route) => {
      reports.push(route.request().postData() ?? '')
      await route.fulfill({ json: { ok: true } })
    })
    await page.getByRole('button', { name: 'Playlists', exact: true }).click()
    await page.getByRole('button', { name: /Workout/ }).first().click()
    await expect(page.locator('.app-main .error-state')).toContainText('Something went wrong')
    await expect(page.getByRole('button', { name: 'Reload this view' })).toBeVisible()
    // The shell and the player are untouched.
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    await player(page).getByRole('button', { name: /^Pause/ }).click()
    await expect(player(page).getByRole('button', { name: /^Play/ }).first()).toBeVisible()
    // It was reported to the server log.
    await expect.poll(() => reports.some((body) => body.includes('Render error'))).toBe(true)

    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Play Midnight City' })).toBeVisible()
    await expect(page.locator('.error-state:visible')).toHaveCount(0)

    // Coming back to the tab that broke starts it afresh instead of showing the fallback again.
    await page.unroute(/\/api\/playlists\/2$/)
    await page.getByRole('button', { name: 'Playlists', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Your playlists' })).toBeVisible()
    await expect(page.locator('.error-state:visible')).toHaveCount(0)
  })
})

test.describe('tag editor selection', () => {
  test('after a partial bulk remove the failed track stays selected and Edit tags still opens', async ({ page }) => {
    const state = await installApiMock(page)
    // Track 3 cannot be removed; track 1 can. The rows shift up afterwards.
    await page.route('**/api/admin/media/bulk-remove', async (route) => {
      const ids = (route.request().postDataJSON() as { media_item_ids: number[] }).media_item_ids
      const failed = ids.filter((id) => id === 3)
      const removed = ids.filter((id) => id !== 3)
      state.tracks = state.tracks.filter((track) => !removed.includes(track.id))
      await route.fulfill({ json: { removed, failed } })
    })
    await page.goto('/#/songs')
    await page.getByRole('button', { name: 'More actions for Bohemian Rhapsody' }).click()
    await page.getByRole('menuitem', { name: 'Select' }).click()
    await page.getByRole('button', { name: 'Select Midnight City' }).click()
    await expect(page.locator('.bulk-action-bar')).toContainText('2 selected')
    await page.getByRole('button', { name: 'Remove from library' }).click()
    await answerDialog(page, 'accept')
    await expect(page.getByRole('button', { name: 'Select Bohemian Rhapsody' })).toHaveCount(0)
    await expect(page.locator('.bulk-action-bar')).toContainText('1 selected')
    await expect(page.getByRole('button', { name: 'Deselect Midnight City' })).toBeVisible()
    await page.getByRole('button', { name: 'Edit tags' }).click()
    const dialog = page.getByRole('dialog', { name: 'Edit tags' })
    await expect(dialog.getByLabel('Title')).toHaveValue('Midnight City')
  })

  test('a partial bulk tag edit names the files that failed and refreshes the rest on close', async ({ page }) => {
    const state = await installApiMock(page)
    await page.route('**/api/admin/media/bulk-tags', async (route) => {
      // The server's BulkTagEditOut: {updated, succeeded, failed: [{id, reason}]}.
      for (const track of state.tracks) if (track.id === 1) track.genre = 'Glam'
      await route.fulfill({ json: { updated: 1, succeeded: [1], failed: [{ id: 2, reason: 'File is read-only' }] } })
    })
    await page.goto('/#/songs')
    await page.getByRole('button', { name: 'More actions for Bohemian Rhapsody' }).click()
    await page.getByRole('menuitem', { name: 'Select' }).click()
    await page.getByRole('button', { name: "Select Don't Stop Me Now" }).click()
    await page.getByRole('button', { name: 'Edit tags' }).click()
    const dialog = page.getByRole('dialog', { name: 'Edit 2 tracks' })
    await dialog.getByLabel('Genre').fill('Glam')
    await dialog.getByRole('button', { name: 'Save' }).click()
    await expect(dialog.getByRole('alert')).toContainText("1 of 2 couldn't be updated")
    await expect(dialog.getByRole('list', { name: 'Files that could not be updated' })).toContainText("Don't Stop Me Now — File is read-only")
    // Closing refreshes the list (the genre filter now sees the edited track)...
    await dialog.getByRole('button', { name: 'Close' }).click()
    // ...and leaves only the failed file selected.
    await expect(page.locator('.bulk-action-bar')).toContainText('1 selected')
    await expect(page.getByRole('button', { name: "Deselect Don't Stop Me Now" })).toBeVisible()
  })
})
