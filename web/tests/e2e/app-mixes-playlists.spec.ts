// Specified mixes, playlists, Home discovery and radio.

import { expect, test } from './fixtures'
import { answerDialog } from './dialogs'
import { installApiMock } from './mockApi'
import { openApp, player, sawCall, chooseOption, selectMenu } from './app-helpers'

test.describe('Atriolyric browser flows', () => {
  test('sorts and searches specify mix options', async ({ page }) => {
    await openApp(page)

    await page.getByRole('button', { name: 'Specify mix' }).click()
    await expect(page.getByRole('dialog', { name: 'Specify mix' })).toBeVisible()

    const options = page.locator('.specify-mix-options > button')
    await expect(options).toHaveCount(3)
    await expect(options.nth(0)).toContainText('Rock')
    await expect(options.nth(0)).toContainText('Genre · 200 tracks')
    // The name sits above the count and must not be squashed away.
    const nameBox = await options.nth(0).locator(':scope > span').boundingBox()
    const metaBox = await options.nth(0).locator('small').boundingBox()
    expect(nameBox?.height ?? 0).toBeGreaterThan(10)
    expect(nameBox!.y + nameBox!.height).toBeLessThanOrEqual(metaBox!.y + 1)
    await expect(options.nth(1)).toContainText('Electronic')

    await page.getByRole('searchbox', { name: 'Search genres and tags' }).fill('work')
    await expect(options).toHaveCount(1)
    await expect(options.first()).toContainText('Workout')
  })

  test('specify mix tiles cycle include/exclude, fit a phone and keep Make mix pinned', async ({ page }) => {
    const mock = await installApiMock(page, true)
    await page.setViewportSize({ width: 375, height: 600 })
    await page.goto('/#/home')
    await page.getByRole('button', { name: 'Specify mix' }).click()
    const dialog = page.getByRole('dialog', { name: 'Specify mix' })
    const options = dialog.locator('.specify-mix-options > button')
    await expect(options).toHaveCount(3)

    // No horizontal overflow and every tile in the first row is equally tall.
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    const grid = dialog.locator('.specify-mix-options')
    expect(await grid.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true)
    const a = (await options.nth(0).boundingBox())!
    const b = (await options.nth(1).boundingBox())!
    expect(Math.abs(a.y - b.y)).toBeLessThan(1)
    expect(Math.abs(a.height - b.height)).toBeLessThan(1)

    const make = dialog.getByRole('button', { name: 'Make mix' })
    await expect(make).toBeDisabled()
    const rock = dialog.getByRole('button', { name: /Genre Rock/ })
    const electronic = dialog.getByRole('button', { name: /Genre Electronic/ })
    await expect(rock).toHaveAttribute('aria-pressed', 'false')
    await rock.click()
    await expect(rock).toHaveAttribute('aria-pressed', 'true')
    await expect(rock).toHaveAccessibleName(/included/)
    await expect(make).toBeEnabled()
    await rock.click()
    await expect(rock).toHaveAttribute('aria-pressed', 'mixed')
    await expect(rock).toHaveAccessibleName(/excluded/)
    expect(await rock.locator(':scope > span').evaluate((el) => getComputedStyle(el).textDecorationLine)).toBe('line-through')
    await expect(make).toBeDisabled()
    await rock.click()
    await expect(rock).toHaveAttribute('aria-pressed', 'false')

    // Make mix stays inside the dialog while the option list scrolls.
    await grid.evaluate((el) => { el.scrollTop = el.scrollHeight })
    await expect(make).toBeInViewport()

    await rock.click()
    await rock.click()
    await electronic.click()
    await make.click()
    const call = mock.calls.find((c) => c.path === '/api/library/mixes/specified' && c.method === 'POST')
    expect(call?.body).toEqual({
      include: [{ kind: 'genre', name: 'Electronic' }],
      exclude: [{ kind: 'genre', name: 'Rock' }],
    })
    await expect(dialog).toHaveCount(0)
  })

  test('says so when too few tracks carry every chosen genre and tag', async ({ page }) => {
    await installApiMock(page, true)
    await page.goto('/#/home')

    // One label: every track of the mix carries it, so there is nothing to say.
    await page.getByRole('button', { name: 'Specify mix' }).click()
    let dialog = page.getByRole('dialog', { name: 'Specify mix' })
    await dialog.getByRole('button', { name: /Genre Rock/ }).click()
    await dialog.getByRole('button', { name: 'Make mix' }).click()
    await expect(dialog).toHaveCount(0)
    await expect(page.locator('.toast')).toHaveCount(0)

    // Two labels, and only one of the mix's two tracks carries both.
    await page.getByRole('button', { name: 'Specify mix' }).click()
    dialog = page.getByRole('dialog', { name: 'Specify mix' })
    await dialog.getByRole('button', { name: /Genre Rock/ }).click()
    await dialog.getByRole('button', { name: /Genre Electronic/ }).click()
    await dialog.getByRole('button', { name: 'Make mix' }).click()
    await expect(dialog).toHaveCount(0)
    await expect(page.locator('.toast-warning')).toHaveText(
      'Only 1 track matches everything you chose, so the rest of the mix matches just some of it.',
    )
  })

  test('explains that the Home mixes come from what the user plays', async ({ page }) => {
    await installApiMock(page, true)
    await page.goto('/#/home')

    await page.getByRole('button', { name: 'About Mixes for you' }).click()
    await expect(page.getByRole('tooltip')).toContainText('artists you play together')
    await expect(page.getByRole('tooltip')).toContainText('Specify mix')
  })

  test('keeps specified mixes visible past the 13-item Home rail cap', async ({ page }) => {
    const mock = await installApiMock(page, true, { defaultMixes: 13 })
    await page.goto('/#/home')

    const mixesRail = page.locator('.home-rail').filter({ hasText: 'Mixes for you' })
    // The 13 featured mixes already fill the rail on their own.
    await expect(mixesRail.locator('.mix-card')).toHaveCount(13)

    await page.getByRole('button', { name: 'Specify mix' }).click()
    await page.locator('.specify-mix-options > button').filter({ hasText: 'Workout' }).click()
    await page.getByRole('button', { name: 'Make mix' }).click()

    expect(sawCall(mock, '/api/library/mixes/specified', 'POST')).toBe(true)
    // The specified mix is added on top of the 13 defaults rather than being
    // cut off by the rail cap, and it sorts after them.
    await expect(mixesRail.locator('.mix-card')).toHaveCount(14)
    await expect(mixesRail.locator('.mix-card').last()).toContainText('Workout')
  })

  test('specifies a mix from the Playlists tab Generated heading', async ({ page }) => {
    const mock = await installApiMock(page, true)
    await page.goto('/#/playlists')

    // The entry point lives in the Generated heading row, to the right of
    // that section's HelpTip.
    const generatedHeading = page.locator('.tab-section-heading').filter({ hasText: 'Generated' })
    await expect(generatedHeading.getByRole('button', { name: 'About generated playlists' })).toBeVisible()
    const headingChildren = await generatedHeading
      .locator(':scope > *')
      .evaluateAll((elements) =>
        elements.map((element) =>
          element.classList.contains('specify-mix-trigger') ? 'specify-mix-trigger' : element.className,
        ),
      )
    const helpIndex = headingChildren.indexOf('section-help-tip')
    const triggerIndex = headingChildren.indexOf('specify-mix-trigger')
    expect(helpIndex).toBeGreaterThanOrEqual(0)
    expect(helpIndex).toBeLessThan(triggerIndex)

    await generatedHeading.getByRole('button', { name: 'Specify mix' }).click()
    const dialog = page.getByRole('dialog', { name: 'Specify mix' })
    await expect(dialog).toBeVisible()
    // The shared dialog's copy is adapted to this entry point rather than
    // reusing Home's rail wording.
    await expect(dialog).toContainText('Generated list below')
    await expect(dialog).not.toContainText('end of this row')

    const options = dialog.locator('.specify-mix-options > button')
    await expect(options).toHaveCount(3)
    await page.getByRole('searchbox', { name: 'Search genres and tags' }).fill('work')
    await expect(options).toHaveCount(1)
    await options.first().click()
    await dialog.getByRole('button', { name: 'Make mix' }).click()

    expect(sawCall(mock, '/api/library/mixes/specified', 'POST')).toBe(true)
    await expect(dialog).toHaveCount(0)
    await expect(page.getByText('Added a temporary mix to Generated.')).toBeVisible()
    // The specified mix is a real auto-generated playlist row, so refreshing
    // the list puts its card in the Generated grid without leaving the tab.
    await expect(page.locator('.browse-card-playlist').filter({ hasText: 'Workout Mix' })).toBeVisible()
  })

  test('keeps the Playlists Specify mix entry point with nothing generated', async ({ page }) => {
    const mock = await installApiMock(page, true)
    // Drop the mock's generated playlists (Discovery, Rock Mix) so the
    // Generated section has nothing to list.
    mock.playlists = mock.playlists.filter((playlist) => !playlist.auto_kind || playlist.auto_kind === 'favorites')
    await page.goto('/#/playlists')

    const generatedHeading = page.locator('.tab-section-heading').filter({ hasText: 'Generated' })
    await expect(generatedHeading).toBeVisible()
    const trigger = generatedHeading.getByRole('button', { name: 'Specify mix' })
    await expect(trigger).toBeVisible()
    await expect(page.getByText(/No generated playlists yet/)).toBeVisible()
    // Only the permanent cards are left (Favorites, Workout): no generated
    // grid, but the heading and its trigger stay.
    await expect(page.locator('.browse-card-playlist')).toHaveCount(2)
    await expect(page.locator('.generated-section-note')).toHaveCount(0)

    await trigger.click()
    const dialog = page.getByRole('dialog', { name: 'Specify mix' })
    await expect(dialog).toBeVisible()
    await dialog.getByRole('button', { name: 'Close' }).click()
    await expect(dialog).toHaveCount(0)
  })

  test('creates, edits, plays, and copies playlists', async ({ page }) => {
    const mock = await openApp(page)

    await page.getByRole('button', { name: 'Playlists', exact: true }).click()
    await page.getByRole('button', { name: 'About playlists' }).click()
    await expect(page.getByRole('tooltip')).toContainText('Create playlists')
    await page.getByPlaceholder('New playlist name').fill('Road trip')
    await page.getByRole('button', { name: 'Create', exact: true }).click()
    await expect(page.locator('.browse-card-playlist').filter({ hasText: 'Road trip' })).toBeVisible()

    await page.locator('.browse-card-main').filter({ hasText: 'Workout' }).click()
    await expect(page.getByRole('heading', { name: 'Workout', exact: true })).toBeVisible()
    const workoutRow = page.locator('.track-row').filter({ hasText: 'Bohemian Rhapsody' })
    await workoutRow.getByRole('button', { name: 'More actions' }).click()
    await page.getByRole('menuitem', { name: 'Remove from playlist' }).click()
    await answerDialog(page, 'accept')
    await expect(page.locator('.track-row').filter({ hasText: 'Bohemian Rhapsody' })).toHaveCount(0)
    await expect(page.getByRole('link', { name: 'Export .m3u' })).toHaveAttribute(
      'href',
      '/api/playlists/2/export.m3u',
    )
    expect(sawCall(mock, '/api/playlists/2/items/201', 'DELETE')).toBe(true)

    await page.getByRole('button', { name: 'Edit', exact: true }).click()
    await page.getByLabel('Name').fill('Leg day')
    await page.getByLabel('Description').fill('Heavy riffs only')
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Leg day', exact: true })).toBeVisible()
    await expect(page.locator('.collection-detail-description')).toHaveText('Heavy riffs only')
    expect(mock.calls.find((call) => call.path === '/api/playlists/2' && call.method === 'PATCH')?.body)
      .toEqual({ name: 'Leg day', description: 'Heavy riffs only' })

    await page.getByRole('banner').getByRole('button', { name: 'Playlists', exact: true }).click()
    await expect(page.locator('.browse-card-playlist').filter({ hasText: 'Leg day' })).toBeVisible()
    await page.locator('.browse-card-main').filter({ hasText: 'Discovery' }).click()
    await expect(page.getByRole('button', { name: 'Save as a playlist' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Edit', exact: true })).toHaveCount(0)
    await page.getByRole('button', { name: 'Add more tracks' }).click()
    await expect(page.getByText('Added more tracks to the playlist.')).toBeVisible()
    expect(sawCall(mock, '/api/playlists/3/add-more', 'POST')).toBe(true)
    await page.getByRole('button', { name: 'Save as a playlist' }).click()
    await expect(page.getByText('Added as a playlist')).toBeVisible()
    expect(sawCall(mock, '/api/playlists/3/save-copy', 'POST')).toBe(true)
    // Clicking the filled icon again removes the copy; then save it again.
    await page.getByRole('button', { name: 'Remove saved playlist' }).click()
    await expect(page.getByText('Removed from playlists')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Save as a playlist' })).toBeVisible()
    await page.getByRole('button', { name: 'Save as a playlist' }).click()
    await expect(page.getByRole('button', { name: 'Remove saved playlist' })).toBeVisible()
    await page.getByRole('banner').getByRole('button', { name: 'Playlists', exact: true }).click()
    await expect(page.locator('.browse-card-playlist').filter({ hasText: 'Discovery' })).toHaveCount(2)
    await page.locator('.browse-card-playlist').filter({ hasText: 'Discovery' }).last().getByRole('button', { name: 'Delete Discovery' }).click()
    await answerDialog(page, 'accept')
    await expect(page.locator('.browse-card-playlist').filter({ hasText: 'Discovery' })).toHaveCount(1)
  })

  test('uses Home discovery and radio playback flows', async ({ page }) => {
    const mock = await openApp(page)

    await expect(page.getByText('Recommended from your library', { exact: true })).toBeVisible()
    await expect(page.getByText('Discover new artists', { exact: true })).toBeVisible()
    await expect(page.getByText('Mixes for you', { exact: true })).toBeVisible()
    await expect(page.locator('.home-view .rail-card').filter({ hasText: 'Radio Paradise' })).toBeVisible()

    const homeRails = page.locator('.home-rail')
    await expect(homeRails).toHaveCount(5)
    await expect(homeRails.locator('.home-rail-help-button')).toHaveCount(5)
    const recentlyPlayedRail = homeRails.filter({ hasText: 'Recently played' })
    await expect(recentlyPlayedRail.getByText('Rock Mix', { exact: true })).toBeVisible()
    await recentlyPlayedRail.getByRole('button', { name: 'About Recently played' }).click()
    const recentlyPlayedTip = page.getByRole('tooltip')
    await expect(recentlyPlayedTip).toContainText('Jump back to albums')
    const tipBox = await recentlyPlayedTip.boundingBox()
    const headerBox = await page.locator('.app-header').boundingBox()
    expect(tipBox && headerBox).toBeTruthy()
    // Whether the bubble overlaps the header depends on how far down the rail
    // sits, so only require it to be fully on screen; z-index below proves it
    // would stack above the header if it did overlap.
    expect(tipBox!.y).toBeGreaterThanOrEqual(0)
    expect(tipBox!.x).toBeGreaterThanOrEqual(0)
    const tooltipZIndex = await recentlyPlayedTip.evaluate((element) => Number(getComputedStyle(element).zIndex))
    const headerZIndex = await page.locator('.app-header').evaluate((element) => Number(getComputedStyle(element).zIndex))
    expect(tooltipZIndex).toBeGreaterThan(headerZIndex)
    const mixesHeader = homeRails.filter({ hasText: 'Mixes for you' }).locator('.home-rail-header')
    // The help tip sits with the title; Regenerate and Specify mix follow it.
    const mixesHeaderChildren = await mixesHeader
      .locator('.home-rail-help, .home-rail-regenerate, .home-rail-specify')
      .evaluateAll((elements) => elements.map((element) => element.className.split(' ').find((name: string) => name.startsWith('home-rail-'))))
    const mixesHelpIndex = mixesHeaderChildren.indexOf('home-rail-help')
    const mixesRegenerateIndex = mixesHeaderChildren.indexOf('home-rail-regenerate')
    if (mixesRegenerateIndex >= 0) expect(mixesHelpIndex).toBeLessThan(mixesRegenerateIndex)
    expect(mixesHelpIndex).toBeGreaterThanOrEqual(0)
    expect(mixesHelpIndex).toBeLessThan(mixesHeaderChildren.indexOf('home-rail-specify'))
    // Mix collages render from the persisted preview ids carried by
    // /api/library/mixes - no per-mix playlist fetches (such a fetch would be
    // GET /api/playlists/4 for the mocked Rock Mix).
    const mixCollageItems = page
      .locator('.home-rail')
      .filter({ hasText: 'Mixes for you' })
      .locator('.mix-card .mix-collage-item')
    await expect(mixCollageItems).toHaveCount(2)
    expect(sawCall(mock, '/api/playlists/4')).toBe(false)
    await page.getByRole('button', { name: 'Regenerate Mixes for you' }).click()
    await expect(recentlyPlayedRail.getByText('Rock Mix', { exact: true })).toHaveCount(0)
    await expect(
      page
        .locator('.home-rail')
        .filter({ hasText: 'Mixes for you' })
        .locator('.mix-card .mix-collage-item')
    ).toHaveCount(2)
    expect(sawCall(mock, '/api/playlists/4')).toBe(false)

    // Recently added is on Library info: Home has the five other sections, in order.
    const railTitles = await page.locator('.home-rail .home-section-title').allTextContents()
    expect(railTitles).toEqual([
      'Recently played',
      'Recommended from your library',
      'Discover new artists',
      'Mixes for you',
      'Your radio stations',
    ])

    await page
      .locator('.home-rail')
      .filter({ hasText: 'Recently played' })
      .getByRole('button', { name: /^Workout/ })
      .click()
    await expect(page.getByRole('banner').getByRole('button', { name: 'Home', exact: true })).toHaveCount(1)
    await page.getByRole('banner').getByRole('button', { name: 'Home', exact: true }).click()
    await expect(page).toHaveURL(/#\/home$/)

    const recommendedCard = page
      .locator('.home-rail')
      .filter({ hasText: 'Recommended from your library' })
      .locator('.rail-card')
      .first()
    await recommendedCard.click({ button: 'right' })
    await expect(page.getByRole('menuitem', { name: 'Add next in queue' })).toBeVisible()
    await expect(page.getByRole('menuitem', { name: 'Add last in queue' })).toBeVisible()
    await expect(page.getByRole('menuitem', { name: 'Replace queue' })).toHaveCount(0)
    await expect(page.getByRole('menuitem', { name: 'Add to playlist' })).toBeVisible()
    await page.getByRole('menuitem', { name: 'Add to playlist' }).click()
    await page.locator('.track-menu-panel').getByRole('menuitem', { name: 'Workout', exact: true }).click()
    await expect(page.getByText(/Added .* to "Workout"\./)).toBeVisible()

    await page.getByRole('button', { name: 'Regenerate Recommended from your library' }).click()
    await expect(page.getByRole('button', { name: 'Regenerate Recommended from your library' })).toBeVisible()
    mock.discoveryState.explore_results = Array.from({ length: 10 }, (_, index) => ({
      artist: `New artist ${index + 1}`,
      title: `Preview ${index + 1}`,
      artwork_url: null,
      preview_url: `https://cdn.example/new-artist-${index + 1}.mp3`,
      view_url: null,
    }))
    await page.getByRole('button', { name: 'Regenerate Discover new artists' }).click()
    expect(sawCall(mock, '/api/library/discovery/refresh', 'POST')).toBe(true)
    expect(sawCall(mock, '/api/library/discovery/explore', 'POST')).toBe(true)
    const exploreRail = page.locator('.home-rail').filter({ hasText: 'Discover new artists' })
    await expect(exploreRail.locator('.explore-card')).toHaveCount(10)
    await expect(exploreRail).toContainText('10 previews · scroll horizontally for more')

    const preview = exploreRail.locator('button[title="Play 30s preview"]').first()
    await preview.click()
    await expect(page.locator('button[title="Pause preview"]')).toBeVisible()
    await page.locator('button[title="Pause preview"]').click()
    await expect(exploreRail.locator('button[title="Play 30s preview"]').first()).toBeVisible()

    await page.getByTitle('Open Rock Mix').click()
    await expect(page).toHaveURL(/#\/playlists\/4$/)
    await expect(page.getByRole('heading', { name: 'Rock Mix', exact: true })).toBeVisible()
    await expect(player(page)).toContainText('Nothing playing')

    await page.getByRole('navigation').getByRole('button', { name: 'Home', exact: true }).click()
    await page.locator('.home-view .rail-card').filter({ hasText: 'Radio Paradise' }).click()
    await expect(player(page)).toContainText('Radio Paradise')
    await expect(player(page)).toContainText('Miles Davis - So What')
    await page.getByRole('button', { name: 'Open now playing' }).click()
    await expect(page.locator(':is(.nowplaying-overlay, .nowplaying-page)')).toContainText('Miles Davis - So What')
    await page.getByRole('button', { name: '+ Want list' }).click()
    await expect.poll(() => sawCall(mock, '/api/radio/want-list', 'POST')).toBe(true)
    // Now Playing's Stop (the side panel behind it has one too, made inert).
    await page.locator(':is(.nowplaying-overlay, .nowplaying-page)').getByRole('button', { name: 'Stop', exact: true }).click()

    await page.getByRole('button', { name: 'Radio', exact: true }).click()
    await page.getByRole('button', { name: 'About radio' }).click()
    await expect(page.getByRole('tooltip')).toContainText('internet radio streams')
    await page.getByPlaceholder('Station name').fill('New Indie')
    await page.getByPlaceholder('Stream URL').fill('https://radio.example/indie')
    await page.getByPlaceholder('Description (optional)').fill('Fresh sounds')
    await page.locator('.radio-view input[type="checkbox"]').check()
    await page.getByRole('button', { name: 'Add station', exact: true }).click()
    const newStation = page.locator('.radio-station-row').filter({ hasText: 'New Indie' })
    await expect(newStation).toBeVisible()
    await newStation.getByRole('button', { name: 'Favourite' }).click()
    await expect(newStation.getByRole('button', { name: 'Unfavourite' })).toBeVisible()
    await chooseOption(page, selectMenu(page.locator('.radio-sort-row'), 'Sort'), 'Name (Z–A)')
    await newStation.getByRole('button', { name: 'Remove New Indie' }).click()
    await answerDialog(page, 'accept')
    await expect(newStation).toHaveCount(0)
    expect(sawCall(mock, '/api/radio/stations', 'POST')).toBe(true)
  })
})
