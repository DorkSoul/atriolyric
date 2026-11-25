// Library flows: setup and sign-in, browsing, likes, the seek bar, browse loading and art, Home search.

import { expect, test } from './fixtures'
import type { Page, Request, Route } from 'playwright/test'
import { answerDialog } from './dialogs'
import { type AppMockState, installApiMock } from './mockApi'
import { openApp, player, sawCall, settle } from './app-helpers'

test.describe('Atriolyric browser flows', () => {
  test('creates the first admin from the setup screen', async ({ page }) => {
    const mock = await installApiMock(page, false, { needsSetup: true })
    await page.goto('/#/home')

    await expect(page.getByRole('button', { name: 'Create admin account' })).toBeVisible()
    await expect(page.getByPlaceholder('Username')).toHaveValue('admin')
    await page.getByPlaceholder('Setup code').fill('wrong-code')
    await page.getByPlaceholder('Password (min. 8 characters)').fill('first-password')
    await page.getByPlaceholder('Confirm password').fill('different-password')
    await page.getByRole('button', { name: 'Create admin account' }).click()
    await expect(page.locator('.login-error')).toHaveText("Passwords don't match.")

    await page.getByPlaceholder('Confirm password').fill('first-password')
    await page.getByRole('button', { name: 'Create admin account' }).click()
    await expect(page.locator('.login-error')).toHaveText("That setup code isn't right - check the server log")

    await page.getByPlaceholder('Setup code').fill('setup-code-123')
    await page.getByPlaceholder('Username').fill('owner')
    await page.getByRole('button', { name: 'Create admin account' }).click()
    await expect(page.locator('.home-view')).toBeVisible()
    expect(mock.calls.find((call) => call.path === '/api/setup' && call.body.code === 'setup-code-123')?.body)
      .toEqual({ code: 'setup-code-123', username: 'owner', password: 'first-password' })

    await page.getByRole('button', { name: 'Settings' }).click()
    await page.getByRole('menuitem', { name: 'Log out' }).click()
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
  })

  test('signs in, shows authentication errors, and logs out', async ({ page }) => {
    const mock = await installApiMock(page, false)
    await page.goto('/#/home')

    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
    await page.getByPlaceholder('Username').fill('dork_soul')
    await page.getByPlaceholder('Password').fill('wrong password')
    await page.getByRole('button', { name: 'Show password' }).click()
    await expect(page.getByPlaceholder('Password')).toHaveAttribute('type', 'text')
    await page.getByRole('button', { name: 'Hide password' }).click()
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.locator('.login-error')).toHaveText('Incorrect username or password.')

    await page.getByPlaceholder('Password').fill(mock.password)
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.locator('.home-view')).toBeVisible()
    await expect(page.getByText('dork_soul', { exact: true })).toBeVisible()

    await page.getByRole('button', { name: 'Settings' }).click()
    await page.getByRole('menuitem', { name: 'Log out' }).click()
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
  })

  test('a username typed with a space after it signs in; the password goes as typed', async ({ page }) => {
    const mock = await installApiMock(page, false)
    await page.goto('/#/home')
    const username = page.getByPlaceholder('Username')
    // What a phone's keyboard does to a word it does not know is a name.
    await expect(username).toHaveAttribute('autocapitalize', 'none')
    await expect(username).toHaveAttribute('autocorrect', 'off')
    await expect(username).toHaveAttribute('spellcheck', 'false')
    await expect(username).toHaveAttribute('autocomplete', 'username')
    await username.fill('  dork_soul ')
    await page.getByPlaceholder('Password').fill(mock.password)
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.locator('.home-view')).toBeVisible()
    expect(mock.calls.find((call) => call.path === '/api/auth/login')?.body).toEqual({ username: 'dork_soul', password: mock.password })

    // A password is never trimmed: one with a space at the end is sent whole.
    await page.getByRole('button', { name: 'Settings' }).click()
    await page.getByRole('menuitem', { name: 'Log out' }).click()
    await page.getByPlaceholder('Username').fill('dork_soul')
    await page.getByPlaceholder('Password').fill(`${mock.password} `)
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.locator('.login-error')).toHaveText('Incorrect username or password.')
    expect(mock.calls.filter((call) => call.path === '/api/auth/login').at(-1)?.body.password).toBe(`${mock.password} `)
  })

  test('the setup screen\'s username is not capitalised or corrected either', async ({ page }) => {
    await installApiMock(page, false, { needsSetup: true })
    await page.goto('/#/home')
    const username = page.getByPlaceholder('Username')
    await expect(username).toHaveAttribute('autocapitalize', 'none')
    await expect(username).toHaveAttribute('autocorrect', 'off')
    await expect(username).toHaveAttribute('spellcheck', 'false')
    await expect(username).toHaveAttribute('autocomplete', 'username')
  })

  test('browses songs and albums and exercises track actions', async ({ page }) => {
    const mock = await openApp(page)

    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    // No search box on Songs: the genre filter narrows the list.
    await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
    const firstSong = page.locator('.track-row').filter({ hasText: 'Bohemian Rhapsody' })
    await firstSong.getByRole('link', { name: 'Queen' }).click()
    await expect(page).toHaveURL(/#\/artists\/Queen$/)
    await page.goBack()
    await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
    await page.getByRole('button', { name: /^Genre/ }).click()
    await page.getByRole('menuitemradio', { name: 'Electronic', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Play Midnight City' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toHaveCount(0)

    await page.getByRole('button', { name: /^Genre/ }).click()
    await page.getByRole('menuitemradio', { name: 'All genres', exact: true }).click()
    // Act on the refetched list, not on the one it is about to replace
    // (which would unmount an open menu or dialog).
    await expect(page.locator('.track-row')).toHaveCount(3)
    const row = page.locator('.track-row').filter({ hasText: 'Bohemian Rhapsody' })
    await expect(row.getByRole('button', { name: /^Rate/ })).toHaveCount(0)
    // The heart cycles none -> liked -> disliked -> none. Each label says
    // what the next tap does.
    await row.getByRole('button', { name: /^Not liked: .*Press to like$/ }).click()
    await expect(row.getByRole('button', { name: /^Liked: .*Press to dislike$/ })).toBeVisible()
    await expect(row).not.toHaveClass(/track-row-disliked/)
    await row.getByRole('button', { name: /^Liked: .*Press to dislike$/ }).click()
    await expect(row.getByRole('button', { name: /^Disliked: .*Press to clear the dislike$/ })).toBeVisible()
    await expect(page.locator('.toast').filter({ hasText: 'Disliked – won’t appear in mixes' })).toBeVisible()
    await expect(row).toHaveClass(/track-row-disliked/)
    await expect(row).toHaveAttribute('title', 'Disliked – won’t appear in mixes')
    await page.reload()
    await expect(page.locator('.track-row')).toHaveCount(3)
    await expect(row.getByRole('button', { name: /^Disliked: .*Press to clear the dislike$/ })).toBeVisible()
    await row.getByRole('button', { name: /^Disliked: .*Press to clear the dislike$/ }).click()
    await expect(row.getByRole('button', { name: /^Not liked: .*Press to like$/ })).toBeVisible()
    await expect(row).not.toHaveClass(/track-row-disliked/)
    expect(
      mock.calls
        .filter((call) => call.path === '/api/media/1/preference')
        .map((call) => call.body.state),
    ).toEqual(['liked', 'disliked', 'none'])

    await row.getByRole('button', { name: 'More actions' }).click()
    await page.getByRole('menuitem', { name: 'View lyrics' }).click()
    await expect(page.locator('.lyrics-text')).toContainText('real life?')
    await page.getByRole('button', { name: 'Close' }).click()

    await row.getByRole('button', { name: 'More actions' }).click()
    await page.getByRole('menuitem', { name: 'Edit tags' }).click()
    await page.getByLabel('Title').fill('Bohemian Rhapsody')
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    expect(sawCall(mock, '/api/admin/media/1/tags', 'PATCH')).toBe(true)

    await row.getByRole('button', { name: 'More actions' }).click()
    await page.getByRole('menuitem', { name: 'Add to playlist' }).click()
    await page.locator('.track-menu-panel').getByRole('menuitem', { name: 'Workout', exact: true }).click()
    await expect(page.getByText('Added "Bohemian Rhapsody" to "Workout".', { exact: true })).toBeVisible()

    await row.getByRole('button', { name: 'More actions' }).click()
    await page.getByRole('menuitem', { name: 'Generate mix from this song' }).click()
    await expect(page.getByText(/Created "Song Mix: Bohemian Rhapsody"/)).toBeVisible()

    await expect(page.locator('.track-row')).toHaveCount(3)
    await row.getByRole('button', { name: 'More actions' }).click()
    await page.getByRole('menuitem', { name: 'Select' }).click()
    await expect(page.locator('.bulk-action-bar')).toContainText('1 selected')
    const secondSelectedRow = page.locator('.track-row').filter({ hasText: 'Midnight City' })
    await secondSelectedRow.getByRole('button', { name: 'More actions' }).click()
    await page.getByRole('menuitem', { name: 'Select' }).click()
    await expect(page.locator('.bulk-action-bar')).toContainText('2 selected')
    await page.locator('.bulk-action-bar').getByRole('button', { name: 'Edit tags' }).click()
    await page.getByLabel('Genre', { exact: true }).fill('Electronic')
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    // The request goes out after the click handler's await: wait for it.
    await expect.poll(() => sawCall(mock, '/api/admin/media/bulk-tags', 'POST')).toBe(true)

    await page.getByRole('button', { name: 'Albums', exact: true }).click()
    await expect(page.locator('.browse-card-album')).toHaveCount(3)
    await page.getByRole('button', { name: 'Sort' }).click()
    await page.getByRole('menuitemradio', { name: 'Date added', exact: true }).click()
    await page.getByRole('button', { name: 'List', exact: true }).click()
    const album = page.locator('.browse-list-row').filter({ hasText: 'A Night at the Opera' })
    await album.click()
    await expect(page.locator('.browse-detail-title')).toHaveText('A Night at the Opera · Queen')
    await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
    await page.getByRole('banner').getByRole('button', { name: 'Albums', exact: true }).click()

    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await expect(page.locator('.track-row')).toHaveCount(3)
    const removeRow = page.locator('.track-row').filter({ hasText: "Don't Stop Me Now" })
    await removeRow.getByRole('button', { name: 'More actions' }).click()
    await page.getByRole('menuitem', { name: 'Select' }).click()
    await page.locator('.bulk-action-bar').getByRole('button', { name: 'Remove from library' }).click()
    const removeTracksConfirmation = await answerDialog(page, 'accept')
    await expect(removeRow).toHaveCount(0)
    await expect(page.locator('.track-row')).toHaveCount(2)
    expect(removeTracksConfirmation).toContain('original music files will not be changed or deleted')
    expect(sawCall(mock, '/api/admin/media/bulk-remove', 'POST')).toBe(true)
  })

  test('likes and dislikes albums and artists from their page headers', async ({ page }) => {
    const mock = await openApp(page)

    await page.getByRole('button', { name: 'Albums', exact: true }).click()
    const card = page.locator('.browse-card-album').filter({ hasText: 'A Night at the Opera' })
    await expect(card).toBeVisible()
    // Hearts live only in the page headers, never on grid cards.
    await expect(page.locator('.browse-card').getByRole('button', { name: /^Not liked: .*Press to like$/ })).toHaveCount(0)
    await card.click()
    const header = page.locator('.collection-detail')
    const heart = header.getByRole('button', { name: /^Not liked: .*Press to like$/ })
    await expect(heart).toHaveAttribute('title', 'Album: A Night at the Opera by Queen')
    await heart.click()
    await header.getByRole('button', { name: /^Liked: .*Press to dislike$/ }).click()
    await expect(header.getByRole('button', { name: /^Disliked: .*Press to clear the dislike$/ })).toBeVisible()
    await expect(page.locator('.toast').filter({ hasText: 'Disliked – won’t appear in mixes' })).toBeVisible()
    // The song keeps its own (outline) heart but its row dims, naming why.
    const row = page.locator('.track-row').filter({ hasText: 'Bohemian Rhapsody' })
    await expect(row).toHaveClass(/track-row-disliked/)
    await expect(row).toHaveAttribute('title', 'Album disliked – won’t appear in mixes')
    await expect(row.getByRole('button', { name: /^Not liked: .*Press to like$/ })).toBeVisible()
    expect(mock.collectionPreferences).toEqual([
      { kind: 'album', artist: 'Queen', album: 'A Night at the Opera', state: 'disliked' },
    ])

    await page.getByRole('button', { name: 'Artists', exact: true }).click()
    await page.locator('.browse-card').filter({ hasText: 'M83' }).click()
    const artistHeader = page.locator('.collection-detail')
    await expect(artistHeader.getByRole('button', { name: /^Not liked: .*Press to like$/ })).toHaveAttribute('title', 'Artist: M83')
    await artistHeader.getByRole('button', { name: /^Not liked: .*Press to like$/ }).click()
    await expect(artistHeader.getByRole('button', { name: /^Liked: .*Press to dislike$/ })).toBeVisible()
    expect(mock.collectionPreferences).toContainEqual({ kind: 'artist', artist: 'M83', album: null, state: 'liked' })
    await expect(player(page).getByRole('button', { name: /^Not liked: .*Press to like$/ })).toHaveCount(0)
  })

  test('plays a track when tapping its row', async ({ page }) => {
    await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()

    const row = page.locator('.track-row').filter({ hasText: 'Bohemian Rhapsody' })
    await expect(row.locator('.track-duration')).toHaveCount(0)
    await row.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    await expect(player(page).locator('.player-info-text .track-subtitle')).toContainText('Queen')
    // The album: on the subtitle line in the bar, a line of its own in the side panel.
    await expect(player(page).locator('.player-info-text')).toContainText('A Night at the Opera')

    await row.dispatchEvent('click')
    await expect(player(page)).toContainText('Bohemian Rhapsody')
  })

  test('shows a like/dislike heart in the bottom bar on desktop and mobile', async ({ page }) => {
    await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page
      .locator('.track-row')
      .filter({ hasText: 'Bohemian Rhapsody' })
      .getByRole('button', { name: 'Play Bohemian Rhapsody' })
      .click()
    const bar = player(page)
    await expect(bar.getByRole('button', { name: /^Not liked: .*Press to like$/ })).toBeVisible()
    await bar.getByRole('button', { name: /^Not liked: .*Press to like$/ }).click()
    await expect(bar.getByRole('button', { name: /^Liked: .*Press to dislike$/ })).toBeVisible()

    await page.setViewportSize({ width: 375, height: 700 })
    const heart = bar.getByRole('button', { name: /^Liked: .*Press to dislike$/ })
    await expect(heart).toBeVisible()
    const heartBox = (await heart.boundingBox())!
    const barBox = (await bar.boundingBox())!
    expect(heartBox.x).toBeGreaterThanOrEqual(0)
    expect(heartBox.x + heartBox.width).toBeLessThanOrEqual(375)
    expect(heartBox.y + heartBox.height).toBeLessThanOrEqual(barBox.y + barBox.height + 1)
  })

  test('swaps the Now Playing art for lyrics and remembers the choice', async ({ page }) => {
    await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page
      .locator('.track-row')
      .filter({ hasText: 'Bohemian Rhapsody' })
      .getByRole('button', { name: 'Play Bohemian Rhapsody' })
      .click()
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    const nowPlaying = page.locator(':is(.nowplaying-overlay, .nowplaying-page)')
    await expect(nowPlaying.locator('.art-nowplaying')).toBeVisible()
    // On a desktop the lyrics take the lower half of the left column; the
    // artwork stays (beside the title and controls).
    const lyricsToggle = nowPlaying.getByRole('button', { name: 'Lyrics', exact: true })
    await lyricsToggle.click()
    await expect(lyricsToggle).toHaveAttribute('aria-pressed', 'true')
    await expect(nowPlaying.getByTestId('lyrics-inline')).toContainText('Is this the real life?')
    await expect(nowPlaying.locator('.art-nowplaying')).toBeVisible()

    // The choice is remembered the next time Now Playing opens.
    await nowPlaying.getByRole('button', { name: 'Back', exact: true }).click()
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    await expect(nowPlaying.getByTestId('lyrics-inline')).toBeVisible()
    await lyricsToggle.click()
    await expect(lyricsToggle).toHaveAttribute('aria-pressed', 'false')
    await expect(nowPlaying.getByTestId('lyrics-inline')).toHaveCount(0)
    await expect(nowPlaying.locator('.art-nowplaying')).toBeVisible()
  })

  test('puts Play and Shuffle icons in album and artist detail headers', async ({ page }) => {
    await openApp(page)
    await page.getByRole('button', { name: 'Artists', exact: true }).click()
    await page.locator('.browse-card').filter({ hasText: 'Queen' }).click()
    const actions = page.locator('.collection-detail-actions')
    await expect(actions.getByRole('button', { name: 'Play', exact: true })).toBeVisible()
    await expect(actions.getByRole('button', { name: 'Shuffle play' })).toBeVisible()
    await expect(page.getByRole('button', { name: '▶ Play all' })).toHaveCount(0)
    await actions.getByRole('button', { name: 'Shuffle play' }).click()
    await expect(player(page)).not.toContainText('Nothing playing')
    // No big empty gap between the left edge of the page area and the art.
    const artBox = (await page.locator('.collection-detail-art').boundingBox())!
    const mainBox = (await page.locator('.app-main').boundingBox())!
    expect(artBox.x - mainBox.x).toBeLessThan(40)
  })

  test('seeks from the player border with hover and touch-hold time previews', async ({ page }) => {
    // The player bar's border: below 1200px wide (the side panel shows above).
    await page.setViewportSize({ width: 1100, height: 720 })
    const mock = await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()

    const seekTrack = page.locator('.player-progress-track')
    await expect(seekTrack).toBeVisible()
    const bounds = await seekTrack.boundingBox()
    expect(bounds).not.toBeNull()
    if (!bounds) return

    const desktopX = bounds.x + bounds.width * 0.5
    await page.mouse.move(desktopX, bounds.y + bounds.height / 2)
    await expect(seekTrack.getByRole('tooltip')).toBeVisible()
    await expect(seekTrack.getByRole('tooltip')).toHaveText(/^\d+:\d{2}$/)
    const desktopTooltipStyle = await seekTrack.getByRole('tooltip').evaluate((element) => {
      const style = getComputedStyle(element)
      return {
        fontSize: Number.parseFloat(style.fontSize),
        top: Number.parseFloat(style.top),
        arrowWidth: Number.parseFloat(
          getComputedStyle(element.querySelector('.player-seek-tooltip-arrow-inner')!).borderTopWidth,
        ),
      }
    })
    expect(desktopTooltipStyle.fontSize).toBeGreaterThanOrEqual(20)
    expect(desktopTooltipStyle.top).toBeLessThan(-45)
    expect(desktopTooltipStyle.arrowWidth).toBeGreaterThan(0)
    const seekArrow = seekTrack.locator('.player-seek-tooltip-arrow-inner')
    const edgeX = bounds.x + 1
    await page.mouse.move(edgeX, bounds.y + bounds.height / 2)
    await expect.poll(async () => {
      const arrowBounds = await seekArrow.boundingBox()
      return arrowBounds ? Math.abs(arrowBounds.x + arrowBounds.width / 2 - edgeX) : Infinity
    }).toBeLessThan(2)
    await page.mouse.move(desktopX, bounds.y + bounds.height / 2)
    await page.mouse.click(desktopX, bounds.y + bounds.height / 2)
    await expect.poll(
      () => mock.wsMessages.filter((message) => message.type === 'seek').at(-1)?.position_seconds ?? 0,
    ).toBeGreaterThan(150)

    const desktopPlayerBounds = await player(page).boundingBox()
    const desktopControlsBounds = await page.locator('.player-controls').boundingBox()
    expect(desktopPlayerBounds).not.toBeNull()
    expect(desktopControlsBounds).not.toBeNull()
    if (!desktopPlayerBounds || !desktopControlsBounds) return
    expect(
      Math.abs(
        desktopPlayerBounds.x + desktopPlayerBounds.width / 2 -
          desktopControlsBounds.x - desktopControlsBounds.width / 2,
      ),
    ).toBeLessThan(2)

    await page.setViewportSize({ width: 390, height: 844 })
    const mobileBounds = await seekTrack.boundingBox()
    expect(mobileBounds).not.toBeNull()
    if (!mobileBounds) return
    await expect(page.locator('.player-extras')).toHaveCount(0)
    const mobilePlayerBounds = await player(page).boundingBox()
    const mobileControlsBounds = await page.locator('.player-controls').boundingBox()
    expect(mobilePlayerBounds).not.toBeNull()
    expect(mobileControlsBounds).not.toBeNull()
    if (!mobilePlayerBounds || !mobileControlsBounds) return
    // A phone's mini player is one row: the controls sit at its right end.
    expect(
      mobilePlayerBounds.x + mobilePlayerBounds.width - (mobileControlsBounds.x + mobileControlsBounds.width),
    ).toBeLessThan(16)
    expect(mobileControlsBounds.y).toBeGreaterThanOrEqual(mobilePlayerBounds.y)
    expect(mobileControlsBounds.y + mobileControlsBounds.height).toBeLessThanOrEqual(
      mobilePlayerBounds.y + mobilePlayerBounds.height + 0.5,
    )

    await page.mouse.move(1, 1)
    await expect(seekTrack.getByRole('tooltip')).toHaveCount(0)
    const touchX = mobileBounds.x + mobileBounds.width * 0.75
    const touchY = mobileBounds.y + mobileBounds.height / 2
    await seekTrack.dispatchEvent('pointerdown', {
      pointerId: 77,
      pointerType: 'touch',
      isPrimary: true,
      button: 0,
      clientX: touchX,
      clientY: touchY,
    })
    await expect(seekTrack.getByRole('tooltip')).toBeVisible()
    await seekTrack.dispatchEvent('pointerup', {
      pointerId: 77,
      pointerType: 'touch',
      isPrimary: true,
      button: 0,
      clientX: touchX,
      clientY: touchY,
    })
    await expect.poll(
      () => mock.wsMessages.filter((message) => message.type === 'seek').at(-1)?.position_seconds ?? 0,
    ).toBeGreaterThan(250)

    const seekCountAfterTouch = mock.wsMessages.filter((message) => message.type === 'seek').length
    await seekTrack.dispatchEvent('pointerdown', {
      pointerId: 78,
      pointerType: 'touch',
      isPrimary: true,
      button: 0,
      clientX: touchX,
      clientY: touchY,
    })
    await expect(seekTrack.getByRole('tooltip')).toBeVisible()
    await seekTrack.dispatchEvent('pointerup', {
      pointerId: 78,
      pointerType: 'touch',
      isPrimary: true,
      button: 0,
      clientX: touchX,
      clientY: mobileBounds.y + mobileBounds.height + 20,
    })
    expect(mock.wsMessages.filter((message) => message.type === 'seek')).toHaveLength(seekCountAfterTouch)
  })

  test('browses artists, videos, files, and recent views', async ({ page }) => {
    const mock = await openApp(page)

    await page.getByRole('button', { name: 'Artists', exact: true }).click()
    await expect(page.locator('.browse-card').filter({ hasText: 'Queen' })).toBeVisible()
    await page.locator('.browse-card').filter({ hasText: 'Queen' }).click({ button: 'right' })
    await expect(page.locator('.collection-menu-panel').getByRole('menuitem', { name: 'Play', exact: true })).toHaveCount(0)
    await expect(page.locator('.collection-menu-panel').getByRole('menuitem', { name: 'Shuffle', exact: true })).toHaveCount(0)
    await expect(page.getByRole('menuitem', { name: 'Add next in queue' })).toBeVisible()
    await expect(page.getByRole('menuitem', { name: 'Add last in queue' })).toBeVisible()
    await expect(page.getByRole('menuitem', { name: 'Replace queue' })).toHaveCount(0)
    await expect(page.getByRole('menuitem', { name: 'Create playlist from Queen' })).toBeVisible()
    await page.getByRole('menuitem', { name: 'Add all to playlist' }).click()
    await page.getByRole('menuitem', { name: 'Workout', exact: true }).click()
    await expect(page.getByText(/Added .* tracks to "Workout"\./)).toBeVisible()
    await page.locator('.browse-card').filter({ hasText: 'Queen' }).click()
    await expect(page.locator('.browse-detail-title')).toHaveText('Queen')
    await expect(page.getByRole('button', { name: '▶ Play all' })).toHaveCount(0)
    await page.locator('.collection-detail-actions').getByRole('button', { name: 'Play', exact: true }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    await page.locator('.browse-card-album').filter({ hasText: 'A Night at the Opera' }).click()
    await expect(page.locator('.browse-detail-title')).toHaveText('A Night at the Opera · Queen')
    // The artist page asks the server for just that artist's albums (full=1
    // plus the artist filter) instead of pulling the whole library and
    // filtering on the client.
    expect(
      mock.calls.some(
        (call) =>
          call.url.startsWith('/api/library/albums?') &&
          call.url.includes('artist=Queen') &&
          call.url.includes('full=1'),
      ),
    ).toBe(true)
    await page.getByRole('banner').getByRole('button', { name: 'Queen', exact: true }).click()
    await page.getByRole('banner').getByRole('button', { name: 'Artists', exact: true }).click()

    await page.getByRole('button', { name: 'Videos', exact: true }).click()
    await page.locator('.browse-card').filter({ hasText: 'Shows' }).click()
    await expect(page.locator('.home-section-title')).toHaveText('Shows')
    const episode = page.locator('.video-card').filter({ hasText: 'Episode One' })
    await expect(episode).toBeVisible()
    await episode.click()
    await expect(player(page)).toContainText('Episode One')

    // Video opens Now Playing in the full-bleed layout with a tap-to-toggle picture.
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    const videoOverlay = page.locator('.nowplaying-overlay.video-mode')
    await expect(videoOverlay).toBeVisible()
    await expect(videoOverlay.getByRole('button', { name: 'Full screen' })).toBeVisible()
    await expect(videoOverlay.getByRole('button', { name: 'Back 30 seconds' })).toBeVisible()
    await expect(videoOverlay.getByRole('button', { name: 'Forward 30 seconds' })).toBeVisible()
    await videoOverlay.getByRole('button', { name: 'Back', exact: true }).click()
    await expect(page.locator(':is(.nowplaying-overlay, .nowplaying-page)')).toHaveCount(0)
    // Off the top-left corner, where a page's help tip opens on hover.
    await page.mouse.move(640, 500)

    await page.getByRole('button', { name: 'Files', exact: true }).click()
    await page.getByRole('button', { name: 'About files' }).click()
    await expect(page.getByRole('tooltip')).toContainText('Browse folders and play tracks')
    // Put the tip away: beside the side panel the page column is narrower,
    // and the open bubble reaches over the first rows.
    await page.keyboard.press('Escape')
    await page.mouse.move(640, 500)
    await page.locator('main.app-main').focus()
    await expect(page.getByRole('tooltip')).toHaveCount(0)
    // Several library paths: Files opens on a list of them.
    await expect(page.locator('.files-search-control')).toHaveCount(0)
    await expect(page.locator('.files-list .files-row-btn').filter({ hasText: 'Video vault' })).toBeVisible()
    await page.locator('.files-list .files-row-btn').filter({ hasText: 'Music' }).click()
    const discOne = page.locator('.files-list .files-row-btn').filter({ hasText: 'Disc 1' })
    await expect(discOne).toBeVisible()
    await discOne.click()
    await expect(page.locator('.files-list .files-row-btn').filter({ hasText: '..' })).toBeVisible()
    await expect(page.getByText('Bohemian Rhapsody', { exact: true })).toBeVisible()
    await page.locator('.files-list .files-row-btn').filter({ hasText: '..' }).click()
    await page.getByRole('button', { name: 'Ignore Disc 1 from metadata and discovery' }).click()
    await expect(page.locator('.files-ignored-label')).toHaveText('Ignored')
    await expect(page.getByRole('status').filter({ hasText: 'Its files remain playable' })).toBeVisible()
    await page.getByRole('button', { name: 'Restore Disc 1 for metadata and discovery' }).click()
    await expect(page.locator('.files-ignored-label')).toHaveCount(0)
    const fileSearch = page.getByRole('searchbox', { name: 'Search files and folders' })
    await fileSearch.fill('Bohemian')
    await expect(page.locator('.files-search-path')).toHaveText('Disc 1/01 - Bohemian Rhapsody.flac')
    await page.locator('.files-list .files-row-btn').filter({ hasText: 'Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    expect(sawCall(mock, '/api/library/files/search')).toBe(true)
    await page.getByRole('button', { name: 'Clear file search' }).click()
    await fileSearch.fill('Disc')
    await expect(page.locator('.files-list .files-row-btn').filter({ hasText: 'Disc 1' })).toBeVisible()
    await page.locator('.files-list .files-row-btn').filter({ hasText: 'Disc 1' }).click()
    await expect(fileSearch).toHaveValue('')
    expect(sawCall(mock, '/api/library/roots/1/ignored-folders', 'POST')).toBe(true)
    expect(sawCall(mock, '/api/library/roots/1/ignored-folders', 'DELETE')).toBe(true)
    await page.locator('.files-breadcrumb').getByRole('button', { name: 'Libraries', exact: true }).click()
    await page.locator('.files-list .files-row-btn').filter({ hasText: 'Video vault' }).click()
    await expect(page.locator('.files-list .files-row-btn').filter({ hasText: 'Shows' })).toBeVisible()

    await page.goto('/#/recent-played')
    await expect(page.getByRole('button', { name: "Play Don't Stop Me Now" })).toBeVisible()
    expect(mock.wsMessages.some((message) => message.type === 'set_queue_context')).toBe(true)
  })

  test('reuses an album list while switching browse tabs', async ({ page }) => {
    const mock = await openApp(page)

    await page.getByRole('button', { name: 'Albums', exact: true }).click()
    await expect(page.locator('.browse-card-album')).toHaveCount(3)
    await page.getByRole('button', { name: 'Artists', exact: true }).click()
    await expect(page.locator('.browse-card').filter({ hasText: 'Queen' })).toBeVisible()
    await page.getByRole('button', { name: 'Albums', exact: true }).click()
    await expect(page.locator('.browse-card-album')).toHaveCount(3)

    // The albums *page* fetch (limit=100) happens once; the later
    // background full-list (full=1) fetch is a different request by design
    // and is not counted here.
    const pageCalls = mock.calls.filter(
      (call) => call.url === '/api/library/albums' || (call.url.startsWith('/api/library/albums?') && !call.url.includes('full=1')),
    )
    expect(pageCalls).toHaveLength(1)
  })

  test('alphabet jump bar covers the full library after the background full-list swap', async ({ page }) => {
    await openApp(page)

    // Seed a library whose first page (limit=100) only spans letters A-J;
    // the full list (260 items, 10 per letter) spans A-Z, so "Jump to Z"
    // is only reachable once the background full-list swap has landed.
    // Names must START with the letter - the alphabet index is built from
    // each name's first character.
    const TOTAL = 260
    const artistFor = (index: number) => `${String.fromCharCode(65 + Math.floor(index / 10))}-${index}`
    // The full list is held back until the test releases it, so the pre-swap
    // state (first page only, Z disabled) can be observed however slow the
    // machine is - mirroring a real network where the full list takes longer
    // than the page.
    let releaseFullList!: () => void
    const fullListReleased = new Promise<void>((resolve) => { releaseFullList = resolve })
    const paginate = (build: () => unknown[], url: URL) => {
      if (url.searchParams.get('full') === '1') return fullListReleased.then(build)
      const offset = Number(url.searchParams.get('offset') ?? 0)
      const limit = Number(url.searchParams.get('limit') ?? TOTAL)
      return build().slice(offset, offset + limit)
    }
    await page.route('**/api/library/tracks', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    })
    await page.route('**/api/library/artists**', async (route) => {
      const url = new URL(route.request().url())
      const build = () =>
        Array.from({ length: TOTAL }, (_, index) => ({
          artist: artistFor(index),
          track_count: 1,
          album_count: 1,
          representative_media_item_id: 1000 + index,
        }))
      const body = JSON.stringify(await paginate(build, url))
      await route.fulfill({ status: 200, contentType: 'application/json', body })
    })
    await page.route('**/api/library/albums**', async (route) => {
      const url = new URL(route.request().url())
      const build = () =>
        // Sorted by title like the real endpoint, with artists whose first
        // letters run the opposite way - the jump bar has to follow the
        // title order the list is actually in, not the artists.
        Array.from({ length: TOTAL }, (_, index) => ({
          artist: `${String.fromCharCode(90 - Math.floor(index / 10))} artist ${index}`,
          album: artistFor(index),
          track_count: 1,
          representative_media_item_id: 1000 + index,
        }))
      const body = JSON.stringify(await paginate(build, url))
      await route.fulfill({ status: 200, contentType: 'application/json', body })
    })

    // Desktop viewport so the jump bar is rendered.
    await page.setViewportSize({ width: 1280, height: 800 })
    await page.goto('/#/artists')
    await expect(page.locator('.browse-card').first()).toBeVisible()

    // First page only: no Z artists are loaded, so Z is unreachable.
    // (hasText, not a ^-anchored regex: each card's text starts with its
    // art fallback glyph, not the artist name.)
    const letterZ = page.getByRole('button', { name: 'Jump to Z' })
    await expect(page.locator('.browse-card').filter({ hasText: 'Z-' })).toHaveCount(0)
    await expect(letterZ).toBeDisabled()
    releaseFullList()

    // The background full-list swap lands; Z becomes reachable and
    // clicking it actually scrolls to a Z artist. Wait until the grid's
    // own height reflects the swapped-in list (260 items vs 100) before
    // clicking - on slower engines (webkit) the click's scrollTo can
    // otherwise race the layout update and clamp to the old scrollHeight.
    await expect(letterZ).toBeEnabled({ timeout: 5_000 })
    await page.waitForFunction(
      () => {
        const grid = document.querySelector('.browse-grid')
        return grid != null && grid.getBoundingClientRect().height > 4000
      },
      { timeout: 5_000 },
    )
    await letterZ.click()
    await expect(page.locator('.browse-card').filter({ hasText: 'Z-' }).first()).toBeVisible({
      timeout: 5_000,
    })

    // The Albums tab gets the same treatment. No pre-swap assertions here:
    // the Artists phase's cross-tab warm-up already cached the identical
    // full-list URL, so the swap can land within the same tick as the
    // first page render.
    await page.getByRole('button', { name: 'Albums', exact: true }).click()
    await expect(page.locator('.browse-card-album').first()).toBeVisible()
    const albumLetterZ = page.getByRole('button', { name: 'Jump to Z' })
    await expect(albumLetterZ).toBeEnabled({ timeout: 5_000 })
    await page.waitForFunction(
      () => {
        const grid = document.querySelector('.browse-grid')
        return grid != null && grid.getBoundingClientRect().height > 4000
      },
      { timeout: 5_000 },
    )
    await albumLetterZ.click()
    await expect(page.locator('.browse-card-album').filter({ hasText: 'Z-' }).first()).toBeVisible({
      timeout: 5_000,
    })
  })

  test('background full-list fetch does not block the initial render', async ({ page }) => {
    await openApp(page)

    // These route overrides are registered after installApiMock, so they
    // handle albums/artists requests themselves and record the query
    // strings they serve (the generic mock records only the pathname).
    const callSearches: Record<string, string[]> = { albums: [], artists: [] }
    // The full list is answered only when the test lets it, so "the page
    // rendered without waiting for it" is a fact about ordering, not about how
    // fast the machine is.
    let releaseFullList!: () => void
    const fullListReleased = new Promise<void>((resolve) => { releaseFullList = resolve })
    let fullListAnswered = false
    await page.route('**/api/library/albums**', async (route) => {
      const url = new URL(route.request().url())
      callSearches.albums.push(url.search)
      const body = JSON.stringify([
        {
          artist: 'Queen',
          album: 'A Night at the Opera',
          track_count: 2,
          representative_media_item_id: 1,
        },
      ])
      if (url.searchParams.get('full') === '1') {
        await fullListReleased
        fullListAnswered = true
        await route.fulfill({ status: 200, contentType: 'application/json', body })
      } else {
        await route.fulfill({ status: 200, contentType: 'application/json', body })
      }
    })
    await page.route('**/api/library/artists**', async (route) => {
      const url = new URL(route.request().url())
      callSearches.artists.push(url.search)
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    })

    await page.getByRole('button', { name: 'Albums', exact: true }).click()
    await expect(page.locator('.browse-card-album')).toHaveCount(1)
    expect(fullListAnswered).toBe(false)

    // The slow full-list response is allowed to finish; the swap doesn't
    // disturb the already-rendered UI and the album stays visible.
    releaseFullList()
    await expect.poll(() => fullListAnswered).toBe(true)
    await settle(page)
    await expect(page.locator('.browse-card-album')).toHaveCount(1)

    // The full-list request went out with full=1, and the cross-tab warm-up
    // for Artists requested the full list too.
    expect(callSearches.albums.some((path) => path.includes('full=1'))).toBe(true)
    // (It follows the Albums list, so it is not necessarily out yet.)
    await expect.poll(() => callSearches.artists.some((path) => path.includes('full=1'))).toBe(true)
  })

  // Seeds large Albums and Artists lists whose representative ids (albums
  // 5000+i, artists 1000+i) tell their art requests apart.
  async function seedBrowseLibrary(page: Parameters<typeof installApiMock>[0], total: number) {
    await page.route('**/api/library/albums**', async (route) => {
      const body = JSON.stringify(
        Array.from({ length: total }, (_, index) => ({
          artist: `Album Artist ${index}`,
          album: `Album ${index}`,
          track_count: 1,
          representative_media_item_id: 5000 + index,
        })),
      )
      await route.fulfill({ status: 200, contentType: 'application/json', body })
    })
    await page.route('**/api/library/artists**', async (route) => {
      const body = JSON.stringify(
        Array.from({ length: total }, (_, index) => ({
          artist: `Artist ${index}`,
          track_count: 1,
          album_count: 1,
          representative_media_item_id: 1000 + index,
        })),
      )
      await route.fulfill({ status: 200, contentType: 'application/json', body })
    })
  }

  // Holds every seeded (id >= 1000) art response until release(), and
  // tracks which art requests were aborted by the page and which are still
  // waiting. Other art (Home, the player bar) is answered at once.
  async function holdArt(page: Parameters<typeof installApiMock>[0]) {
    const svg =
      '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" fill="#5b8def"/></svg>'
    const artId = (url: string) => Number(new URL(url).pathname.match(/^\/api\/stream\/(\d+)\/art$/)?.[1] ?? NaN)
    const isArt = (url: string) => !Number.isNaN(artId(url))
    const requests: Request[] = []
    const failed = new Set<Request>()
    const finished = new Set<Request>()
    const held: Route[] = []
    let released = false
    const fulfill = (route: Route) =>
      route.fulfill({ status: 200, contentType: 'image/svg+xml', body: svg }).catch(() => {})

    page.on('request', (request) => {
      if (isArt(request.url())) requests.push(request)
    })
    page.on('requestfailed', (request) => failed.add(request))
    page.on('requestfinished', (request) => finished.add(request))
    await page.route('**/api/stream/*/art', async (route) => {
      if (released || artId(route.request().url()) < 1000) await fulfill(route)
      else held.push(route)
    })

    const pending = () => requests.filter((request) => !failed.has(request) && !finished.has(request))
    return {
      artId,
      requests,
      failed,
      pending,
      pendingIds: () => pending().map((request) => artId(request.url())),
      release() {
        released = true
        for (const route of held.splice(0)) void fulfill(route)
      },
    }
  }

  test('interrupts old-tab art when switching tabs and fetches it later', async ({ page }) => {
    await openApp(page)
    await seedBrowseLibrary(page, 300)
    const art = await holdArt(page)

    await page.getByRole('button', { name: 'Albums', exact: true }).click()
    await expect.poll(() => art.pendingIds().filter((id) => id >= 5000).length).toBe(6)
    const albumRequests = art.pending()

    // Leaving the tab aborts its in-flight art, and the new tab's art is
    // requested without waiting for it.
    await page.getByRole('button', { name: 'Artists', exact: true }).click()
    await expect.poll(() => albumRequests.every((request) => art.failed.has(request))).toBe(true)
    await expect.poll(() => art.pendingIds().some((id) => id >= 1000 && id < 5000)).toBe(true)

    art.release()
    await expect(page.locator('.browse-card img.art-img').first()).toHaveAttribute('src', /^blob:/)

    // Once everything settles, the interrupted album art is downloaded
    // after all, so it is cached for a return to the Albums tab.
    const abortedUrls = albumRequests.map((request) => request.url())
    await expect
      .poll(
        () => abortedUrls.some((url) => art.requests.filter((request) => request.url() === url).length > 1),
        { timeout: 10_000 },
      )
      .toBe(true)
  })

  test('does not ask again for art the server says does not exist (204)', async ({ page }) => {
    // The art loader's idle timers run on the page clock, which the test moves.
    await page.clock.install()
    await openApp(page)
    await seedBrowseLibrary(page, 300)
    let requests = 0
    await page.route('**/api/stream/*/art', async (route) => {
      if (Number(new URL(route.request().url()).pathname.split('/')[3]) >= 1000) requests += 1
      await route.fulfill({ status: 204, headers: { 'cache-control': 'private, max-age=3600' } }).catch(() => {})
    })
    // Settled: with the loader's idle timers run out and the network quiet,
    // two rounds in a row asked for nothing more.
    const settleArt = async () => {
      let last = -1
      await expect
        .poll(async () => {
          await page.clock.fastForward(1_000)
          await page.waitForLoadState('networkidle')
          const stable = requests === last
          last = requests
          return stable && requests > 0
        })
        .toBe(true)
    }

    // First visit: tiles ask once and fall back to the placeholder.
    for (const tab of ['Albums', 'Artists']) {
      await page.getByRole('button', { name: tab, exact: true }).click()
      await expect(page.locator('.browse-card .art-fallback:not(.hidden)').first()).toBeVisible()
    }
    await settleArt()
    const afterFirstPass = requests

    // Tiles unload and reload as tabs change; a remembered "no art" answer
    // means no further requests (a burst of empty answers from one IP is
    // what CrowdSec bans as probing).
    for (const tab of ['Albums', 'Artists', 'Albums', 'Artists']) {
      await page.getByRole('button', { name: tab, exact: true }).click()
      await expect(page.locator('.browse-card .art-fallback:not(.hidden)').first()).toBeVisible()
    }
    await settleArt()
    expect(requests).toBe(afterFirstPass)
  })

  // Every art request still waiting belongs to a tile on screen - none left
  // queued or in flight for tiles scrolled past. The loader's contract: tiles
  // near the viewport may load ahead, but only while no on-screen tile is
  // waiting for a slot; then each near load gives its slot up. Polled as the
  // two facts that matter, so a failure prints which waiting requests were
  // for tiles off screen (and which tiles were on screen).
  async function expectOnlyOnScreenArtWaiting(page: Page, art: Awaited<ReturnType<typeof holdArt>>) {
    const onScreenAlbumIds = () =>
      page.evaluate(() => {
        const main = document.querySelector('main.app-main')!.getBoundingClientRect()
        const top = Math.max(0, main.top)
        const bottom = Math.min(window.innerHeight, main.bottom)
        return [...document.querySelectorAll('.browse-card-album')]
          .filter((card) => {
            const rect = card.querySelector('.art-wrap')!.getBoundingClientRect()
            return rect.bottom > top && rect.top < bottom
          })
          .map((card) => 5000 + Number(card.querySelector('.browse-title')!.textContent!.replace('Album ', '')))
      })
    await expect
      .poll(async () => {
        const onScreen = await onScreenAlbumIds()
        const pending = art.pendingIds()
        return {
          waiting: pending.length > 0,
          waitingOffScreen: pending.filter((id) => !onScreen.includes(id)),
          ...(pending.some((id) => !onScreen.includes(id)) ? { onScreen } : {}),
        }
      })
      .toEqual({ waiting: true, waitingOffScreen: [] })
  }

  test('loads on-screen art before art scrolled past', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 })
    await openApp(page)
    await seedBrowseLibrary(page, 300)
    const art = await holdArt(page)

    await page.getByRole('button', { name: 'Albums', exact: true }).click()
    await expect.poll(() => art.pendingIds().filter((id) => id >= 5000).length).toBe(6)

    await page.locator('main.app-main').evaluate((main) => main.scrollBy(0, main.clientHeight * 3))
    await expectOnlyOnScreenArtWaiting(page, art)
  })

  test('a slow scroll leaves no art loading for the rows it passed', async ({ page }) => {
    // A slow device renders the rows in between, and their tiles start
    // loading as they pass through the viewport. Stepping the scroll with
    // frames in between does the same on any machine: whole rows of loads
    // that started while on screen must all give way once they are scrolled
    // past, not one per round.
    await page.setViewportSize({ width: 1280, height: 800 })
    await openApp(page)
    await seedBrowseLibrary(page, 300)
    const art = await holdArt(page)

    await page.getByRole('button', { name: 'Albums', exact: true }).click()
    await expect.poll(() => art.pendingIds().filter((id) => id >= 5000).length).toBe(6)

    await page.locator('main.app-main').evaluate(async (main) => {
      const step = (main.clientHeight * 3) / 12
      for (let i = 0; i < 12; i++) {
        main.scrollBy(0, step)
        await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
      }
    })
    await expectOnlyOnScreenArtWaiting(page, art)

    // A row partly on screen loads as on screen; a nudge that takes it out
    // of view hands all of its slots to the rows now showing.
    await page.locator('main.app-main').evaluate((main) => main.scrollBy(0, 160))
    await expectOnlyOnScreenArtWaiting(page, art)
  })
})

test.describe('Home search', () => {
  function beyonceTrack(id: number, title: string) {
    return {
      id,
      library_root_id: 1,
      kind: 'track' as const,
      title,
      artist: 'Beyoncé',
      album: 'Beyoncé',
      genre: 'Pop',
      track_number: id,
      performer: null,
      description: null,
      duration_seconds: 200,
      is_favorite: false,
      is_disliked: false,
      excluded_by: null,
      format_label: null,
    }
  }

  async function openHome(page: Parameters<typeof installApiMock>[0], hash = '#/home'): Promise<AppMockState> {
    const mock = await installApiMock(page)
    mock.tracks.push(beyonceTrack(60, 'Beyoncé Medley'), beyonceTrack(61, 'Beyoncé Interlude'))
    await page.goto(`/${hash}`)
    await expect(page.locator('.home-view')).toBeVisible()
    return mock
  }

  function searchCalls(mock: AppMockState): string[] {
    return mock.calls.filter((call) => call.path === '/api/library/search').map((call) => call.url)
  }

  test('type, see grouped results, open an album, go back, refresh, then clear', async ({ page }) => {
    const mock = await openHome(page)
    await expect(page.getByText('Recently played', { exact: true })).toBeVisible()
    const historyLength = await page.evaluate(() => window.history.length)
    const box = page.getByRole('searchbox', { name: 'Search your library' })

    await box.pressSequentially('beyonce')
    await expect(page.getByRole('region', { name: 'Artists' })).toBeVisible()
    await expect(page.getByRole('region', { name: 'Albums' })).toBeVisible()
    await expect(page.getByRole('region', { name: 'Songs' })).toBeVisible()
    await expect(page.getByText('Recently played', { exact: true })).toBeHidden()
    await expect(page).toHaveURL(/#\/home\/beyonce$/)
    // Typing is debounced, but how many keystrokes land in one burst depends on
    // the machine: every request is a prefix of the word and the last is all of it.
    const calls = searchCalls(mock)
    expect(calls.at(-1)).toBe('/api/library/search?q=beyonce')
    for (const call of calls) {
      expect(call.startsWith('/api/library/search?q=')).toBe(true)
      expect('beyonce'.startsWith(call.slice('/api/library/search?q='.length))).toBe(true)
    }
    expect(await page.evaluate(() => window.history.length)).toBe(historyLength)

    await page.getByRole('region', { name: 'Albums' }).locator('.rail-card').first().click()
    await expect(page).toHaveURL(/#\/albums\/Beyonc%C3%A9\/Beyonc%C3%A9$/)

    await page.goBack()
    await expect(box).toHaveValue('beyonce')
    await expect(page.getByRole('region', { name: 'Songs' })).toBeVisible()

    await page.reload()
    await expect(box).toHaveValue('beyonce')
    await expect(page.getByRole('region', { name: 'Songs' })).toBeVisible()

    const callsBeforeClear = mock.calls.length
    await box.fill('')
    await expect(page.getByText('Recently played', { exact: true })).toBeVisible()
    await expect(page).toHaveURL(/#\/home$/)
    expect(mock.calls.slice(callsBeforeClear).filter((call) => call.path === '/api/library/search')).toEqual([])
  })

  test('View full search opens the matching tab with the search applied as a removable chip', async ({ page }) => {
    const mock = await openHome(page, '#/home/beyonce')
    await expect(page.getByRole('region', { name: 'Artists' })).toBeVisible()

    await page.getByRole('region', { name: 'Artists' }).getByRole('button', { name: /View full/ }).click()
    await expect(page).toHaveURL(/#\/artists$/)
    const artistChip = page.getByRole('button', { name: 'Clear search beyonce' })
    await expect(artistChip).toHaveText(/Search: beyonce/)
    await expect.poll(() => mock.calls.some((call) => call.path === '/api/library/artists' && /[?&]q=beyonce/.test(call.url))).toBe(true)
    // No search box of its own.
    await expect(page.getByRole('textbox')).toHaveCount(0)
    const before = mock.calls.length
    await artistChip.click()
    await expect(artistChip).toHaveCount(0)
    await expect.poll(() => mock.calls.slice(before).some((call) => call.path === '/api/library/artists' && !/[?&]q=/.test(call.url))).toBe(true)

    await page.goBack()
    await page.getByRole('region', { name: 'Songs' }).getByRole('button', { name: /View full/ }).click()
    await expect(page).toHaveURL(/#\/songs$/)
    const songChip = page.getByRole('button', { name: 'Clear search beyonce' })
    await expect(songChip).toBeVisible()
    await expect.poll(() => mock.calls.some((call) => call.path === '/api/library/tracks' && /[?&]q=beyonce/.test(call.url))).toBe(true)
    await songChip.click()
    await expect(songChip).toHaveCount(0)
  })

  test('playing a song result queues the other song results', async ({ page }) => {
    const mock = await openHome(page, '#/home/beyonce')
    const songs = page.getByRole('region', { name: 'Songs' })
    await songs.getByRole('button', { name: 'Play Beyoncé Medley' }).click()

    await expect
      .poll(() => mock.wsMessages.find((message) => message.type === 'set_queue_context') ?? null)
      .toMatchObject({
        context: { kind: 'ids', ids: [61, 60] },
        start_media_item_id: 60,
      })
  })

  test('a slow reply to an older query never replaces newer results', async ({ page }) => {
    await openHome(page)
    const isStale = (url: URL) => url.pathname === '/api/library/search' && url.searchParams.get('q') === 'bey'
    // The old query's reply is held until the newer results are on screen,
    // then delivered; `staleDelivered` says when it has been.
    let releaseStale!: () => void
    const staleReleased = new Promise<void>((resolve) => { releaseStale = resolve })
    let markStaleDelivered!: () => void
    const staleDelivered = new Promise<void>((resolve) => { markStaleDelivered = resolve })
    await page.route(isStale, async (route) => {
      await staleReleased
      await route
        .fulfill({
          json: {
            artists: [{ artist: 'Stale Artist', track_count: 1, album_count: 1, representative_media_item_id: 1 }],
            albums: [],
            songs: [],
            playlists: [],
          },
        })
        .catch(() => {})
      markStaleDelivered()
    })
    const box = page.getByRole('searchbox', { name: 'Search your library' })

    const staleRequest = page.waitForRequest((request) => isStale(new URL(request.url())))
    await box.fill('bey')
    await staleRequest
    await box.fill('beyonce')

    const songs = page.getByRole('region', { name: 'Songs' })
    await expect(songs.getByRole('button', { name: 'Play Beyoncé Medley' })).toBeVisible()
    releaseStale()
    await staleDelivered
    await settle(page)
    await expect(page.getByText('Stale Artist')).toHaveCount(0)
    await expect(songs.getByRole('button', { name: 'Play Beyoncé Medley' })).toBeVisible()
  })

  test('the search box fits a phone screen with a 16px gutter', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await openHome(page)
    const box = page.getByRole('searchbox', { name: 'Search your library' })
    await expect(box).toBeVisible()

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    expect(overflow).toBeLessThanOrEqual(0)
    const rect = await box.boundingBox()
    expect(rect).not.toBeNull()
    // In the top bar: the 16px page gutter on the left, the settings button
    // (inside the same gutter) on the right.
    expect(Math.round(rect!.x)).toBe(16)
    const gear = (await page.getByRole('button', { name: 'Settings', exact: true }).boundingBox())!
    expect(rect!.x + rect!.width).toBeLessThanOrEqual(gear.x)
    expect(Math.round(390 - gear.x - gear.width)).toBe(16)
  })

  test('shows one message when nothing matches', async ({ page }) => {
    await openHome(page)
    await page.getByRole('searchbox', { name: 'Search your library' }).fill('zzzz')

    await expect(page.getByText('No results for "zzzz"')).toBeVisible()
    await expect(page.locator('.home-view section.home-rail')).toHaveCount(0)
  })
})
