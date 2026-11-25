import { type Page } from 'playwright/test'
import { answerDialog } from './dialogs'
import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'
import { makeTrack } from './mock/state'
import { player, nowPlayingView } from './app-helpers'

// Back, focus and title on view changes; the pull-to-refresh gesture; the
// service worker's decisions; the installable-app metadata.

const hash = (page: Page) => page.evaluate(() => window.location.hash)

/** A dialog drops its history entry a moment after it closes: wait for that. */
const entryDropped = (page: Page) =>
  expect.poll(() => page.evaluate(() => (window.history.state as { modalEntry?: number } | null)?.modalEntry ?? null)).toBeNull()

async function openLyrics(page: Page) {
  await page.getByRole('button', { name: 'More actions for Bohemian Rhapsody' }).click()
  await page.getByRole('menuitem', { name: 'View lyrics' }).click()
  const dialog = nowPlayingView(page)
  await expect(dialog).toBeVisible()
  return dialog
}

test.describe('the Back button closes dialogs', () => {
  test('Back closes the lyrics dialog and leaves the page where it was; Back again leaves the page', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/home')
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).waitFor()
    const dialog = await openLyrics(page)
    await page.goBack()
    await expect(dialog).toHaveCount(0)
    expect(await hash(page)).toBe('#/songs')
    await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
    await page.goBack()
    await expect.poll(() => hash(page)).toBe('#/home')
  })

  test('closing a dialog with Escape or its button does not leave a stray history entry', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/home')
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).waitFor()
    let dialog = await openLyrics(page)
    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    dialog = await openLyrics(page)
    await dialog.getByRole('button', { name: 'Close' }).click()
    await expect(dialog).toHaveCount(0)
    await entryDropped(page)
    // One Back from the Songs tab is Home, not a leftover dialog entry.
    await page.goBack()
    await expect.poll(() => hash(page)).toBe('#/home')
    await expect(page.locator('.home-view')).toBeVisible()
  })

  test('Back closes only the top dialog: a confirmation over Now Playing', async ({ page }) => {
    // Below the side panel's 1200px, so Now Playing (a page) carries Up next.
    await page.setViewportSize({ width: 1100, height: 800 })
    await installApiMock(page)
    await page.goto('/#/songs')
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    const nowPlaying = nowPlayingView(page)
    await nowPlaying.locator('.queue-row').first().getByRole('button', { name: /^Remove .* from queue$/ }).click()
    const confirm = page.getByRole('dialog', { name: 'Remove from queue' })
    await expect(confirm).toBeVisible()
    await page.goBack()
    await expect(confirm).toHaveCount(0)
    await expect(nowPlaying).toBeVisible()
    // The next Back is Now Playing's own.
    await page.goBack()
    await expect(nowPlaying).toHaveCount(0)
    expect(await hash(page)).toBe('#/songs')
  })

  test('a link inside a dialog that navigates is not undone when the dialog closes', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/home')
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).waitFor()
    const dialog = await openLyrics(page)
    await dialog.getByRole('link', { name: 'Queen' }).click()
    await expect.poll(() => hash(page)).toBe('#/artists/Queen')
    await expect(dialog).toHaveCount(0)
    await expect(page.locator('.browse-detail-title')).toHaveText('Queen')
    expect(await hash(page)).toBe('#/artists/Queen')
    // The dialog left no entry of its own: one Back is the page it was opened
    // on (closed), the next is the page before that.
    await page.goBack()
    await expect.poll(() => hash(page)).toBe('#/songs')
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
    await page.goBack()
    await expect.poll(() => hash(page)).toBe('#/home')
  })

  /** Songs (opened from Home) with a song by two artists. */
  async function openSongsWithCollab(page: Page) {
    const state = await installApiMock(page)
    state.tracks.push(
      makeTrack(60, {
        title: 'Under Pressure',
        artist: 'Queen feat. David Bowie',
        album: 'Hot Space',
        artists: [
          { name: 'Queen', role: 'main' },
          { name: 'David Bowie', role: 'featured' },
        ],
        album_artist: 'Queen',
      }),
    )
    await page.goto('/#/home')
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Under Pressure' }).waitFor()
  }

  test('picking an artist in the picker: one Back is the page it was opened on, with no dead entry', async ({ page }) => {
    await openSongsWithCollab(page)
    const row = page.locator('.track-row').filter({ has: page.getByRole('button', { name: 'Play Under Pressure' }) })
    await row.getByRole('link', { name: 'Queen feat. David Bowie' }).click()
    await page.getByRole('dialog', { name: 'Choose an artist' }).getByRole('button', { name: /David Bowie/ }).click()
    await expect.poll(() => hash(page)).toBe('#/artists/David%20Bowie')
    await expect(page.locator('.browse-detail-title')).toHaveText('David Bowie')
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await page.goBack()
    await expect.poll(() => hash(page)).toBe('#/songs')
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Play Under Pressure' })).toBeVisible()
    await page.goBack()
    await expect.poll(() => hash(page)).toBe('#/home')
    // And forward again lands on the artist, not on an entry of the picker.
    await page.goForward()
    await page.goForward()
    await expect.poll(() => hash(page)).toBe('#/artists/David%20Bowie')
  })

  test('the picker over the lyrics dialog closes both, and one Back is the page under them', async ({ page }) => {
    await openSongsWithCollab(page)
    await page.getByRole('button', { name: 'More actions for Under Pressure' }).click()
    await page.getByRole('menuitem', { name: 'View lyrics' }).click()
    const lyrics = page.getByRole('dialog', { name: 'Under Pressure' })
    await lyrics.getByRole('link', { name: 'Queen feat. David Bowie' }).click()
    await page.getByRole('dialog', { name: 'Choose an artist' }).getByRole('button', { name: /David Bowie/ }).click()
    await expect.poll(() => hash(page)).toBe('#/artists/David%20Bowie')
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await page.goBack()
    await expect.poll(() => hash(page)).toBe('#/songs')
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await page.goBack()
    await expect.poll(() => hash(page)).toBe('#/home')
  })

  test('a genre picked in the Genres/tags popup: one Back is the album it was opened on', async ({ page }) => {
    await installApiMock(page)
    await page.route('**/api/library/labels?*', (route) =>
      route.fulfill({ json: { genres: [{ name: 'Rock', track_count: 2 }], tags: [] } }),
    )
    await page.goto('/#/home')
    await page.getByRole('button', { name: 'Albums', exact: true }).click()
    await page.locator('.browse-card-album').filter({ hasText: 'A Night at the Opera' }).click()
    await page.getByRole('button', { name: 'Genres/tags' }).click()
    await page.getByRole('dialog').getByRole('button', { name: 'Rock' }).click()
    await expect.poll(() => hash(page)).toBe('#/songs')
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await page.goBack()
    await expect.poll(() => hash(page)).toBe('#/albums/Queen/A%20Night%20at%20the%20Opera')
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await page.goBack()
    await expect.poll(() => hash(page)).toBe('#/albums')
  })

  test('an artist opened from the artist info dialog: one Back is the page it was opened on', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/home')
    await page.getByRole('button', { name: 'Artists', exact: true }).click()
    await page.getByRole('button', { name: 'More actions for Queen', exact: true }).click()
    await page.getByRole('menuitem', { name: 'Info', exact: true }).click()
    const info = page.getByRole('dialog', { name: 'Queen' })
    await info.getByRole('button', { name: 'M83' }).click()
    await expect.poll(() => hash(page)).toBe('#/artists/M83')
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await page.goBack()
    await expect.poll(() => hash(page)).toBe('#/artists')
    await expect(page.getByRole('dialog')).toHaveCount(0)
  })

  test('a confirmation closes on Back too', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/playlists')
    await page.getByRole('button', { name: 'Delete Workout' }).click()
    await expect(page.locator('.confirm-dialog')).toBeVisible()
    await page.goBack()
    await expect(page.locator('.confirm-dialog')).toHaveCount(0)
    expect(await hash(page)).toBe('#/playlists')
    await expect(page.getByRole('button', { name: 'Delete Workout' })).toBeVisible()
    // And answering it still works with its own entry in the history.
    await page.getByRole('button', { name: 'Delete Workout' }).click()
    await answerDialog(page, 'dismiss')
    expect(await hash(page)).toBe('#/playlists')
  })
})

test.describe('in-app back links go up without growing the history', () => {
  const backLink = (page: Page, label: string) => page.locator('.browse-back').filter({ hasText: label })

  test('Albums, an album, "← Albums", then Back: where the user was before Albums, not the album', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/home')
    await page.getByRole('button', { name: 'Albums', exact: true }).click()
    await page.locator('.browse-card-album').filter({ hasText: 'A Night at the Opera' }).click()
    await expect.poll(() => hash(page)).toBe('#/albums/Queen/A%20Night%20at%20the%20Opera')
    await backLink(page, 'Albums').click()
    await expect.poll(() => hash(page)).toBe('#/albums')
    await expect(page.locator('.browse-card-album').first()).toBeVisible()
    await page.goBack()
    await expect.poll(() => hash(page)).toBe('#/home')
    await expect(page.locator('.home-view')).toBeVisible()
  })

  test('an artist, one of their albums, and the two back links: Back then leaves Artists', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/home')
    await page.getByRole('button', { name: 'Artists', exact: true }).click()
    await page.locator('.browse-card').filter({ hasText: 'Queen' }).first().click()
    await expect.poll(() => hash(page)).toBe('#/artists/Queen')
    await page.locator('.browse-card-album').filter({ hasText: 'A Night at the Opera' }).click()
    await expect.poll(() => hash(page)).toBe('#/artists/Queen/A%20Night%20at%20the%20Opera')
    await backLink(page, 'Queen').click()
    await expect.poll(() => hash(page)).toBe('#/artists/Queen')
    await backLink(page, 'Artists').click()
    await expect.poll(() => hash(page)).toBe('#/artists')
    await page.goBack()
    await expect.poll(() => hash(page)).toBe('#/home')
  })

  test('an album opened by its address: "← Albums" shows the list in its place and stays in the app', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/albums/Queen/A%20Night%20at%20the%20Opera')
    const before = await page.evaluate(() => window.history.length)
    await backLink(page, 'Albums').click()
    await expect.poll(() => hash(page)).toBe('#/albums')
    await expect(page.locator('.browse-card-album').first()).toBeVisible()
    // The list took the album's entry: nothing was added.
    expect(await page.evaluate(() => window.history.length)).toBe(before)
  })

  test('a playlist opened by its address: its back link shows the Playlists list and stays in the app', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/playlists/2')
    await expect(page.getByRole('heading', { name: 'Workout', level: 2 })).toBeVisible()
    const before = await page.evaluate(() => window.history.length)
    await backLink(page, 'Playlists').click()
    await expect.poll(() => hash(page)).toBe('#/playlists')
    await expect(page.getByRole('button', { name: 'Delete Workout' })).toBeVisible()
    expect(await page.evaluate(() => window.history.length)).toBe(before)
    // Opened from the list, the link is the browser's Back; after a reload too.
    await page.locator('.browse-card').filter({ hasText: 'Workout' }).first().click()
    await expect.poll(() => hash(page)).toBe('#/playlists/2')
    await page.reload()
    await expect(page.getByRole('heading', { name: 'Workout', level: 2 })).toBeVisible()
    await backLink(page, 'Playlists').click()
    await expect.poll(() => hash(page)).toBe('#/playlists')
    await page.goForward()
    await expect.poll(() => hash(page)).toBe('#/playlists/2')
  })

  test('a mix opened from Home: "← Home" returns there, and Back does not re-enter the mix', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/songs')
    await page.getByRole('button', { name: 'Home', exact: true }).click()
    await page.locator('.home-rail').filter({ hasText: 'Mixes for you' }).locator('.mix-card').first().click()
    await expect.poll(() => hash(page)).toBe('#/playlists/4')
    await backLink(page, 'Home').click()
    await expect.poll(() => hash(page)).toBe('#/home')
    await page.goBack()
    await expect.poll(() => hash(page)).toBe('#/songs')
  })
})

test.describe('album addresses keep the album\'s owner', () => {
  async function openWithAwkwardAlbums(page: Page) {
    const state = await installApiMock(page)
    state.tracks.push(
      // A guest spot: David Bowie performs on Queen's album.
      makeTrack(60, {
        title: 'Under Pressure',
        artist: 'Queen feat. David Bowie',
        album: 'Hot Space',
        artists: [
          { name: 'Queen', role: 'main' },
          { name: 'David Bowie', role: 'featured' },
        ],
        album_artist: 'Queen',
      }),
      makeTrack(61, { title: 'Space Oddity', artist: 'David Bowie', album: 'Space Oddity', artists: [{ name: 'David Bowie', role: 'main' }] }),
      // One album title three times: an artist named "-", no artist, and a
      // name with everything an address has to escape.
      makeTrack(62, { title: 'Dash Song', artist: '-', album: 'Same Name' }),
      makeTrack(63, { title: 'Orphan Song', artist: null, album: 'Same Name' }),
      makeTrack(64, { title: 'Awkward Song', artist: 'AC/DC 100% #1? Ünï', album: 'Same Name' }),
      makeTrack(65, { title: 'Slash Song', artist: 'Queen', album: 'Half/Full 50% – Ünï?' }),
    )
    return state
  }
  const playButton = (page: Page, title: string) => page.getByRole('button', { name: `Play ${title}` })

  test('an "Appears on" album keeps its tracks over a reload, Back and Forward', async ({ page }) => {
    await openWithAwkwardAlbums(page)
    await page.goto('/#/artists/David%20Bowie')
    await page.getByRole('region', { name: 'Appears on' }).locator('.browse-card').filter({ hasText: 'Hot Space' }).click()
    await expect(playButton(page, 'Under Pressure')).toBeVisible()
    // Still under the artist's page, with the album's owner after it.
    expect(await hash(page)).toBe('#/artists/David%20Bowie/Hot%20Space/Queen')
    await page.reload()
    await expect(playButton(page, 'Under Pressure')).toBeVisible()
    await expect(page.locator('.collection-detail-subtitle')).toHaveText('Queen')
    await page.locator('.browse-back').filter({ hasText: 'David Bowie' }).click()
    await expect.poll(() => hash(page)).toBe('#/artists/David%20Bowie')
    await page.goForward()
    await expect.poll(() => hash(page)).toBe('#/artists/David%20Bowie/Hot%20Space/Queen')
    await expect(playButton(page, 'Under Pressure')).toBeVisible()
  })

  test('the artist\'s own album keeps the short address, and an address without an owner still opens', async ({ page }) => {
    await openWithAwkwardAlbums(page)
    await page.goto('/#/artists/David%20Bowie')
    await page.locator('.browse-card-album').filter({ hasText: 'Space Oddity' }).click()
    await expect.poll(() => hash(page)).toBe('#/artists/David%20Bowie/Space%20Oddity')
    await expect(playButton(page, 'Space Oddity')).toBeVisible()
    await page.goto('/#/artists/Queen/A%20Night%20at%20the%20Opera')
    await expect(playButton(page, 'Bohemian Rhapsody')).toBeVisible()
    await expect(page.locator('.collection-detail-subtitle')).toHaveText('Queen')
  })

  test('an artist named "-", no artist, and names with / % # ? and accents each reopen as themselves', async ({ page }) => {
    await openWithAwkwardAlbums(page)
    const cases = [
      { owner: '-', track: 'Dash Song', address: '#/albums/%2D/Same%20Name' },
      { owner: 'Unknown artist', track: 'Orphan Song', address: '#/albums/-/Same%20Name' },
      { owner: 'AC/DC 100% #1? Ünï', track: 'Awkward Song', address: `#/albums/${encodeURIComponent('AC/DC 100% #1? Ünï')}/Same%20Name` },
    ]
    for (const { owner, track, address } of cases) {
      await page.goto('/#/albums')
      await page
        .locator('.more-card-wrap')
        .filter({ hasText: 'Same Name' })
        .filter({ has: page.locator('.browse-subtitle').getByText(owner, { exact: true }) })
        .locator('.browse-card-album')
        .click()
      await expect(playButton(page, track), owner).toBeVisible()
      expect(await hash(page), owner).toBe(address)
      await page.reload()
      await expect(playButton(page, track), owner).toBeVisible()
      await expect(page.locator('.collection-detail-subtitle'), owner).toHaveText(owner)
      // Only its own track: not the other two albums of the same title.
      await expect(page.locator('.track-row')).toHaveCount(1)
    }
    // An album title with the same characters, under an artist's page.
    await page.goto('/#/artists/Queen')
    await page.locator('.browse-card-album').filter({ hasText: 'Half/Full 50% – Ünï?' }).click()
    await expect(playButton(page, 'Slash Song')).toBeVisible()
    expect(await hash(page)).toBe(`#/artists/Queen/${encodeURIComponent('Half/Full 50% – Ünï?')}`)
    await page.reload()
    await expect(playButton(page, 'Slash Song')).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Half/Full 50% – Ünï?', level: 2 })).toBeVisible()
  })
})

test.describe('focus and title follow the view', () => {
  test('the title names the view, the album or artist, and what is playing', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/home')
    await expect(page).toHaveTitle('Home – Atriolyric')
    await page.getByRole('button', { name: 'Albums', exact: true }).click()
    await expect(page).toHaveTitle('Albums – Atriolyric')
    await page.goto('/#/albums/Queen/A%20Night%20at%20the%20Opera')
    await expect(page).toHaveTitle('A Night at the Opera – Atriolyric')
    await page.goto('/#/artists/Queen')
    await expect(page).toHaveTitle('Queen – Atriolyric')
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(page).toHaveTitle('▶ Bohemian Rhapsody – Queen · Atriolyric')
    await player(page).getByRole('button', { name: /^Pause/ }).click()
    await expect(page).toHaveTitle('Songs – Atriolyric')
  })

  test('on a view change focus moves to the page title, or a detail page\'s heading', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/home')
    await expect(page.locator('.home-view')).toBeVisible()
    await page.getByRole('button', { name: 'Radio', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Radio', level: 1 })).toBeFocused()
    await page.getByRole('button', { name: 'Playlists', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Playlists', level: 1 })).toBeFocused()
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Songs', level: 1 })).toBeFocused()
    // A detail page: its own heading (the album), not the hidden tab title.
    await page.getByRole('button', { name: 'Albums', exact: true }).click()
    await page.locator('.browse-card-album').filter({ hasText: 'A Night at the Opera' }).click()
    await expect(page.getByRole('heading', { name: 'A Night at the Opera', level: 2 })).toBeFocused()
    // Not a ring on a heading nobody tabbed to.
    await page.getByRole('button', { name: 'Radio', exact: true }).click()
    const outline = await page.getByRole('heading', { name: 'Radio' }).evaluate((el) => getComputedStyle(el).outlineStyle)
    expect(outline).toBe('none')
  })

  test('typing in Home search keeps focus in the box', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/home')
    const box = page.getByLabel('Search your library')
    await box.pressSequentially('queen')
    await expect.poll(() => hash(page)).toBe('#/home/queen')
    await expect(box).toBeFocused()
  })
})

test.describe('pull to refresh', () => {
  test.skip(({ browserName }) => browserName !== 'chromium', 'synthesised TouchEvents need Chromium')
  test.use({
    hasTouch: true,
    userAgent: 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36',
  })

  /** A one-finger drag down by `dy` px, dispatched at `selector`. */
  async function drag(page: Page, selector: string, dy: number) {
    await page.locator(selector).first().evaluate((el, distance) => {
      const rect = el.getBoundingClientRect()
      const x = rect.left + rect.width / 2
      const y = rect.top + Math.min(rect.height / 2, 30)
      const fire = (type: string, at: number) => {
        const touch = new Touch({ identifier: 1, target: el, clientX: x, clientY: at })
        const live = type === 'touchend' ? [] : [touch]
        el.dispatchEvent(new TouchEvent(type, { bubbles: true, cancelable: true, touches: live, targetTouches: live, changedTouches: [touch] }))
      }
      fire('touchstart', y)
      fire('touchmove', y + distance / 2)
      fire('touchmove', y + distance)
      fire('touchend', y + distance)
    }, dy)
  }

  test('a drag inside a dialog opened from the page does not reload it; a drag on the page does', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/songs')
    await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
    let loads = 0
    page.on('load', () => (loads += 1))
    await page.getByRole('button', { name: 'More actions for Bohemian Rhapsody' }).click()
    await page.getByRole('menuitem', { name: 'Edit tags' }).click()
    const dialog = page.getByRole('dialog', { name: 'Edit tags' })
    await expect(dialog).toBeVisible()
    await drag(page, '.modal-panel .tag-edit-form', 200)
    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    expect(loads).toBe(0)
    await entryDropped(page)
    // The same drag on the page itself, at the top, is a pull to refresh.
    await Promise.all([page.waitForEvent('load'), drag(page, '.track-list', 200)])
    expect(loads).toBe(1)
  })

  test('a drag inside a scroller that is not at its top scrolls it instead of refreshing', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/songs')
    await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
    await page.locator('.app-main').evaluate((main) => {
      const box = document.createElement('div')
      box.id = 'inner-scroller'
      box.style.cssText = 'height:60px;overflow-y:auto'
      box.innerHTML = '<div style="height:400px">tall</div>'
      main.appendChild(box)
      box.scrollTop = 100
    })
    let loads = 0
    page.on('load', () => (loads += 1))
    await drag(page, '#inner-scroller', 200)
    await Promise.all([page.waitForEvent('load'), drag(page, '.track-list', 200)])
    expect(loads).toBe(1)
  })

  test('a short pull does nothing and the page does not re-render while dragging', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/songs')
    await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
    let loads = 0
    page.on('load', () => (loads += 1))
    await drag(page, '.track-list', 40)
    await expect(page.locator('.pull-refresh-indicator')).toBeHidden()
    await Promise.all([page.waitForEvent('load'), drag(page, '.track-list', 200)])
    expect(loads).toBe(1)
  })
})

test.describe('service worker decisions', () => {
  // Service workers are blocked in this config, so the pure functions the
  // worker uses (public/sw-logic.js) are loaded into a bare page and called.
  // Covered: caching rules, request classification, offline fallbacks,
  // cache naming and purging. Not covered: the install/activate/fetch event
  // wiring in sw.js itself, which needs a real worker.
  type Logic = {
    cacheNameFor: (version: string | null) => string
    versionFromScriptUrl: (url: string) => string
    staleCaches: (keys: string[], current: string) => string[]
    classify: (request: { method: string; url: string; mode: string }, origin: string) => string
    isCacheable: (response: { ok: boolean; status: number }) => boolean
    offlineFallback: (kind: string) => string | null
    PRECACHE: string[]
  }
  async function load(page: Page) {
    await page.route('**/sw-blank', (route) => route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>blank</title>' }))
    await page.goto('/sw-blank')
    await page.addScriptTag({ url: '/sw-logic.js' })
  }
  const run = <T>(page: Page, fn: (logic: Logic) => T) =>
    page.evaluate(`(${fn.toString()})(self.atriolyricSw)`) as Promise<T>

  test('each release gets its own cache and older ones are purged', async ({ page }) => {
    await load(page)
    expect(
      await run(page, (l) => ({
        a: l.cacheNameFor(l.versionFromScriptUrl('https://x.test/sw.js?v=0.5.2')),
        b: l.cacheNameFor(l.versionFromScriptUrl('https://x.test/sw.js?v=0.6.0')),
        none: l.cacheNameFor(l.versionFromScriptUrl('https://x.test/sw.js')),
        stale: l.staleCaches(['atriolyric-shell-v9', 'atriolyric-shell-0.5.2', 'atriolyric-shell-0.6.0', 'other'], 'atriolyric-shell-0.6.0'),
      })),
    ).toEqual({
      a: 'atriolyric-shell-0.5.2',
      b: 'atriolyric-shell-0.6.0',
      none: 'atriolyric-shell-dev',
      stale: ['atriolyric-shell-v9', 'atriolyric-shell-0.5.2', 'other'],
    })
  })

  test('requests are classified: assets cache-first, navigations network-first, the API untouched', async ({ page }) => {
    await load(page)
    const kinds = await run(page, (l) => {
      const o = 'https://app.test'
      const get = (path: string, mode = 'no-cors') => l.classify({ method: 'GET', url: o + path, mode }, o)
      return {
        asset: get('/assets/index-abc123.js'),
        lazyChunk: get('/assets/hls-def456.js'),
        page: get('/', 'navigate'),
        deepPage: get('/#/albums', 'navigate'),
        manifest: get('/manifest.json'),
        icon: get('/icons/icon-192.png'),
        api: get('/api/library/tracks'),
        socket: get('/ws/playback'),
        post: l.classify({ method: 'POST', url: o + '/assets/x.js', mode: 'cors' }, o),
        otherOrigin: l.classify({ method: 'GET', url: 'https://cdn.test/assets/x.js', mode: 'cors' }, o),
      }
    })
    expect(kinds).toEqual({
      asset: 'asset',
      lazyChunk: 'asset',
      page: 'navigate',
      deepPage: 'navigate',
      manifest: 'other',
      icon: 'other',
      api: 'ignore',
      socket: 'ignore',
      post: 'ignore',
      otherOrigin: 'ignore',
    })
  })

  test('only a complete 200 is cached; the HTML shell is the fallback for navigations only', async ({ page }) => {
    await load(page)
    const result = await run(page, (l) => ({
      ok: l.isCacheable({ ok: true, status: 200 }),
      partial: l.isCacheable({ ok: true, status: 206 }),
      notFound: l.isCacheable({ ok: false, status: 404 }),
      error: l.isCacheable({ ok: false, status: 500 }),
      navigate: l.offlineFallback('navigate'),
      asset: l.offlineFallback('asset'),
      other: l.offlineFallback('other'),
      precached: l.PRECACHE,
    }))
    expect(result).toMatchObject({ ok: true, partial: false, notFound: false, error: false, navigate: '/', asset: null, other: null })
    expect(result.precached).toEqual(expect.arrayContaining(['/', '/boot.js', '/manifest.json', '/icons/icon-192.png', '/icons/icon-512.png', '/icons/apple-touch-icon.png']))
  })
})

test.describe('installable app metadata', () => {
  test('the manifest lists 192 and 512 PNG icons, any and maskable, and every file is served', async ({ page, request }) => {
    const manifest = await (await request.get('/manifest.json')).json()
    const icons = manifest.icons as { src: string; sizes: string; type: string; purpose: string }[]
    for (const size of ['192x192', '512x512']) {
      for (const purpose of ['any', 'maskable']) {
        expect(icons.some((icon) => icon.sizes === size && icon.purpose === purpose && icon.type === 'image/png'), `${purpose} ${size}`).toBe(true)
      }
    }
    // A separate entry for each purpose: nothing is "any maskable".
    expect(icons.some((icon) => icon.purpose.includes(' '))).toBe(false)
    await page.goto('/#/home')
    for (const icon of icons) {
      const response = await request.get(icon.src)
      expect(response.status(), icon.src).toBe(200)
      if (icon.type === 'image/png') {
        const [width, height] = icon.sizes.split('x').map(Number)
        const size = await page.evaluate(async (src) => {
          const image = new Image()
          image.src = src
          await image.decode()
          return [image.naturalWidth, image.naturalHeight]
        }, icon.src)
        expect(size, icon.src).toEqual([width, height])
      }
    }
  })

  test('the page declares the touch icon, viewport-fit=cover and a theme colour per scheme', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/home')
    expect(await page.locator('link[rel="apple-touch-icon"]').getAttribute('href')).toBe('/icons/apple-touch-icon.png')
    expect((await page.request.get('/icons/apple-touch-icon.png')).status()).toBe(200)
    // Declared in the HTML for an installed app; boot.js drops it in a browser tab (loading.spec.ts).
    expect(await (await page.request.get('/')).text()).toContain('viewport-fit=cover')
    const themes = await page.locator('meta[name="theme-color"]').evaluateAll((els) => els.map((el) => el.getAttribute('media')))
    expect(themes).toEqual(['(prefers-color-scheme: dark)', '(prefers-color-scheme: light)'])
  })
})
