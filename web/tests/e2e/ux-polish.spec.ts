import { type Page } from 'playwright/test'
import { answerDialog } from './dialogs'
import { expect, test } from './fixtures'
import { installApiMock, type AppMockState } from './mockApi'
import { openSettings, openTab, player } from './app-helpers'

// Small interactions: scrolling text, toasts, target sizes, buttons that
// say they are working, the Admin gate, playlist reordering, "Sign out
// everywhere", the ListenBrainz privacy switch, and who may change artwork.

async function openApp(page: Page): Promise<AppMockState> {
  const mock = await installApiMock(page)
  await page.goto('/#/home')
  await expect(page.locator('.home-view')).toBeVisible()
  return mock
}

/** Holds the matching requests until `release()`, then answers them as the mock would. */
async function hold(page: Page, url: RegExp) {
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route(url, async (route) => {
    await gate
    await route.fallback()
  })
  return release
}

async function openSettingsSection(page: Page, label: string) {
  return openSettings(page, label)
}

test.describe('toasts', () => {
  test('a toast stays while it is hovered and counts down again after', async ({ page }) => {
    await page.clock.install()
    await installApiMock(page)
    await page.goto('/#/songs')
    await page.getByRole('button', { name: 'More actions for Bohemian Rhapsody' }).click()
    await page.getByRole('menuitem', { name: 'Add to playlist' }).click()
    await page.getByRole('menuitem', { name: 'Workout' }).click()
    const toast = page.locator('.toast-success')
    await expect(toast).toContainText('Added "Bohemian Rhapsody" to "Workout".')
    await toast.hover()
    // Well past the usual 6 s: still there, because the pointer is on it.
    await page.clock.runFor(20_000)
    await expect(toast).toBeVisible()
    await page.mouse.move(2, 2)
    await page.clock.runFor(7_000)
    await expect(toast).toHaveCount(0)
  })

  test('a toast stays while focus is inside it', async ({ page }) => {
    await page.clock.install()
    await installApiMock(page)
    await page.goto('/#/songs')
    await page.getByRole('button', { name: 'More actions for Bohemian Rhapsody' }).click()
    await page.getByRole('menuitem', { name: 'Add to playlist' }).click()
    await page.getByRole('menuitem', { name: 'Workout' }).click()
    const toast = page.locator('.toast-success')
    await expect(toast).toBeVisible()
    await toast.getByRole('button', { name: 'Dismiss' }).focus()
    await page.clock.runFor(20_000)
    await expect(toast).toBeVisible()
    await toast.getByRole('button', { name: 'Dismiss' }).blur()
    await page.clock.runFor(7_000)
    await expect(toast).toHaveCount(0)
  })
})

test.describe('target sizes', () => {
  test.use({ viewport: { width: 320, height: 700 } })

  test('every tab keeps a 44px target at 320px wide, and the last one can be reached', async ({ page }) => {
    await openApp(page)
    // The bottom bar: Home, three tabs and More, side by side with no scrolling.
    const bar = page.getByRole('navigation', { name: 'Quick tab navigation' })
    const buttons = bar.getByRole('button')
    await expect(buttons).toHaveCount(5)
    for (let i = 0; i < 5; i++) {
      const box = await buttons.nth(i).boundingBox()
      expect(box!.width, `tab ${i} width`).toBeGreaterThanOrEqual(43.5)
      expect(box!.height, `tab ${i} height`).toBeGreaterThanOrEqual(43.5)
      await expect(buttons.nth(i)).toBeInViewport()
    }
    expect(await bar.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true)
    // The rest are under More, each a 44px target; the last one opens.
    await bar.getByRole('button', { name: 'More' }).click()
    const sheet = page.getByRole('dialog', { name: 'More' })
    const items = sheet.locator('.more-sheet-item')
    const count = await items.count()
    expect(count).toBeGreaterThanOrEqual(6)
    for (let i = 0; i < count; i++) {
      const box = await items.nth(i).boundingBox()
      expect(box!.width, `More item ${i} width`).toBeGreaterThanOrEqual(43.5)
      expect(box!.height, `More item ${i} height`).toBeGreaterThanOrEqual(43.5)
    }
    const last = items.last()
    await expect(last).toBeInViewport({ ratio: 0.9 })
    const name = (await last.textContent())!
    await last.click()
    await expect(sheet).toHaveCount(0)
    await expect(page.getByRole('heading', { name, level: 1 })).toBeVisible()
  })

  test('help buttons and alphabet letters are at least 24px', async ({ page }) => {
    await openApp(page)
    await openTab(page, 'Playlists')
    const help = page.getByRole('button', { name: 'About playlists' })
    const helpBox = await help.boundingBox()
    expect(helpBox!.width).toBeGreaterThanOrEqual(23.5)
    expect(helpBox!.height).toBeGreaterThanOrEqual(23.5)
    await page.getByRole('button', { name: 'Albums', exact: true }).click()
    const letters = page.locator('.alphabet-jump-letter')
    await expect(letters.first()).toBeVisible()
    const n = await letters.count()
    for (let i = 0; i < n; i++) {
      const box = await letters.nth(i).boundingBox()
      expect(box!.height, `letter ${i}`).toBeGreaterThanOrEqual(23.5)
      expect(box!.width, `letter ${i}`).toBeGreaterThanOrEqual(23.5)
    }
  })

  test('the admin help buttons are at least 24px wide and tall', async ({ page }) => {
    await openApp(page)
    await openTab(page, 'Admin')
    const helpButtons = page.locator('.admin-panel .admin-help-button')
    await expect(helpButtons.first()).toBeVisible()
    // Measured in one step: the page is still filling in, and a button inside
    // a closed disclosure (WebKit gives it no box) is not on screen to tap.
    const sizes = await helpButtons.evaluateAll((els) =>
      els.filter((el) => el.checkVisibility()).map((el) => {
        const box = el.getBoundingClientRect()
        return { width: box.width, height: box.height }
      }),
    )
    expect(sizes.length).toBeGreaterThan(0)
    sizes.forEach((box, i) => {
      expect(box.width, `help ${i} width`).toBeGreaterThanOrEqual(23.5)
      expect(box.height, `help ${i} height`).toBeGreaterThanOrEqual(23.5)
    })
  })
})

test.describe('buttons that say they are working', () => {
  test('Create playlist is disabled and says so while the request is out, and creates once', async ({ page }) => {
    const mock = await installApiMock(page)
    await page.goto('/#/playlists')
    const release = await hold(page, /\/api\/playlists$/)
    await page.getByPlaceholder('New playlist name').fill('Road trip')
    const create = page.getByRole('button', { name: 'Create', exact: true })
    await create.click()
    const creating = page.getByRole('button', { name: 'Creating…' })
    await expect(creating).toBeDisabled()
    await page.getByPlaceholder('New playlist name').press('Enter')
    release()
    await expect(page.locator('.browse-card-playlist').filter({ hasText: 'Road trip' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Create', exact: true })).toBeEnabled()
    expect(mock.calls.filter((c) => c.method === 'POST' && c.path === '/api/playlists')).toHaveLength(1)
  })

  // The forms submit through React form actions, after which React resets
  // the form: what was typed must still be there for another attempt.
  test('a failed sign-in keeps what was typed', async ({ page }) => {
    await installApiMock(page, false)
    await page.goto('/#/home')
    await page.getByPlaceholder('Username').fill('dork_soul')
    await page.getByPlaceholder('Password').fill('wrong password')
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.locator('.login-error')).toHaveText('Incorrect username or password.')
    await expect(page.getByPlaceholder('Username')).toHaveValue('dork_soul')
    await expect(page.getByPlaceholder('Password')).toHaveValue('wrong password')
  })

  test('a failed setup keeps what was typed', async ({ page }) => {
    await installApiMock(page, false, { needsSetup: true })
    await page.goto('/#/home')
    await page.getByPlaceholder('Setup code').fill('wrong-code')
    await page.getByPlaceholder('Password (min. 8 characters)').fill('first-password')
    await page.getByPlaceholder('Confirm password').fill('first-password')
    await page.getByRole('button', { name: 'Create admin account' }).click()
    await expect(page.locator('.login-error')).toHaveText("That setup code isn't right - check the server log")
    await expect(page.getByPlaceholder('Setup code')).toHaveValue('wrong-code')
    await expect(page.getByPlaceholder('Username')).toHaveValue('admin')
    await expect(page.getByPlaceholder('Password (min. 8 characters)')).toHaveValue('first-password')
    await expect(page.getByPlaceholder('Confirm password')).toHaveValue('first-password')
  })

  test('a refused password change keeps what was typed, and Enter submits it', async ({ page }) => {
    await openApp(page)
    const dialog = await openSettingsSection(page, 'Account')
    const account = dialog
    await page.route(/\/api\/auth\/password$/, (route) =>
      route.fulfill({ status: 400, json: { detail: 'Current password is incorrect' } }),
    )
    await account.getByPlaceholder('Current password').nth(1).fill('not-it')
    await account.getByPlaceholder('New password').fill('a-new-password')
    await account.getByPlaceholder('New password').press('Enter')
    await expect(account.getByText('Current password is incorrect')).toBeVisible()
    await expect(account.getByPlaceholder('Current password').nth(1)).toHaveValue('not-it')
    await expect(account.getByPlaceholder('New password')).toHaveValue('a-new-password')
  })

  test('a failed new playlist keeps its name', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/playlists')
    await page.route(/\/api\/playlists$/, (route) =>
      route.request().method() === 'POST' ? route.fulfill({ status: 500, json: { detail: 'boom' } }) : route.fallback(),
    )
    await page.getByPlaceholder('New playlist name').fill('Road trip')
    await page.getByRole('button', { name: 'Create', exact: true }).click()
    await expect(page.locator('.toast-error')).toContainText('Could not create the playlist.')
    await expect(page.getByPlaceholder('New playlist name')).toHaveValue('Road trip')
  })

  test('Change password and Change username are disabled while they save', async ({ page }) => {
    const mock = await openApp(page)
    const dialog = await openSettingsSection(page, 'Account')
    const account = dialog
    const releasePassword = await hold(page, /\/api\/auth\/password$/)
    await account.getByPlaceholder('Current password').nth(1).fill(mock.password)
    await account.getByPlaceholder('New password').fill('a-new-password')
    await account.getByRole('button', { name: 'Change password' }).click()
    await expect(account.getByRole('button', { name: 'Changing…' })).toBeDisabled()
    releasePassword()
    await expect(account.getByText(/Password changed/)).toBeVisible()

    const releaseUsername = await hold(page, /\/api\/auth\/username$/)
    await account.getByPlaceholder('New username').fill('renamed')
    await account.getByPlaceholder('Current password').first().fill(mock.password)
    await account.getByRole('button', { name: 'Change username' }).click()
    await expect(account.getByRole('button', { name: 'Changing…' })).toBeDisabled()
    releaseUsername()
    await expect(account.getByText('Username changed.', { exact: true })).toBeVisible()
  })

  test('a discovery setting the server refuses goes back to what it was', async ({ page }) => {
    await openApp(page)
    const dialog = await openSettingsSection(page, 'Discovery')
    const toggle = dialog.getByRole('switch', { name: 'Enabled' })
    await expect(toggle).toBeVisible()
    const before = await toggle.isChecked()
    await page.route(/\/api\/library\/discovery\/settings$/, (route) =>
      route.request().method() === 'PUT' ? route.fulfill({ status: 500, json: { detail: 'boom' } }) : route.fallback(),
    )
    await toggle.click()
    await expect(dialog.getByText(/could not|boom|went wrong|problem/i).first()).toBeVisible()
    await expect(toggle).toBeChecked({ checked: before })
  })
})

test.describe('Admin', () => {
  test('someone who is not an admin sees why, and no admin request is made', async ({ page }) => {
    const mock = await installApiMock(page)
    mock.user.is_admin = false
    await page.goto('/#/admin')
    await expect(page.getByRole('alert').filter({ hasText: 'Admin access required.' })).toBeVisible()
    await page.waitForLoadState('networkidle')
    expect(mock.calls.filter((call) => call.path.startsWith('/api/admin'))).toEqual([])
  })

  test('the player bar stays on the Admin tab', async ({ page }) => {
    await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    // By address, not by the tab button: the mock's empty stream can raise a
    // toast that sits over the tab strip for a moment.
    await page.evaluate(() => {
      window.location.hash = '#/admin'
    })
    await expect(page.getByRole('heading', { name: 'Users', exact: true })).toBeVisible()
    await expect(player(page)).toBeVisible()
  })

  test('a running task is announced when its stage changes, not on every poll', async ({ page }) => {
    const mock = await installApiMock(page)
    await page.goto('/#/admin')
    const root = mock.roots[0]
    root.active_task = {
      id: 7,
      kind: 'scan',
      status: 'running',
      stage: 'reading',
      processed: 1,
      total: 100,
    } as never
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Admin', exact: true }).click()
    const live = page.locator('.admin-panel [role="status"].visually-hidden').first()
    await expect(live).not.toHaveText('')
    const first = await live.textContent()
    // One percent more is not worth speaking.
    ;(root.active_task as { processed: number }).processed = 3
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Admin', exact: true }).click()
    await expect(live).toHaveText(first!)
  })
})

test.describe('reordering a playlist', () => {
  async function openWorkout(page: Page) {
    const mock = await installApiMock(page)
    // A third entry, so there is a middle row.
    mock.playlistDetails[2].items.push({ ...mock.tracks[2], playlist_item_id: 203 })
    await page.goto('/#/playlists/2')
    await expect(page.getByRole('heading', { name: 'Workout', exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Reorder', exact: true }).click()
    return mock
  }
  const titles = (page: Page) => page.locator('.playlist-reorder-row .track-title').allTextContents()
  const reorderCalls = (mock: AppMockState) => mock.calls.filter((c) => c.method === 'PUT' && c.path === '/api/playlists/2/reorder')

  test('moving down saves the order with exactly the visible entries, and keeps focus on the handle', async ({ page }) => {
    const mock = await openWorkout(page)
    expect(await titles(page)).toEqual(['Bohemian Rhapsody', "Don't Stop Me Now", 'Midnight City'])
    await expect(page.getByRole('button', { name: /^Move .* (up|down)$/ })).toHaveCount(0)
    const down = page.getByRole('button', { name: 'Reorder Bohemian Rhapsody' })
    await down.focus()
    await page.keyboard.press('ArrowDown')
    await expect.poll(() => titles(page)).toEqual(["Don't Stop Me Now", 'Bohemian Rhapsody', 'Midnight City'])
    await expect(down).toBeFocused()
    await expect.poll(() => reorderCalls(mock).length).toBe(1)
    expect(reorderCalls(mock)[0].body).toEqual({ ordered_item_ids: [202, 201, 203] })
    await expect(page.getByRole('status').filter({ hasText: 'Moved Bohemian Rhapsody to position 2 of 3.' })).toBeAttached()
  })

  test('the arrow keys on the handle move the row and focus stays on the handle', async ({ page }) => {
    const mock = await openWorkout(page)
    const handle = page.getByRole('button', { name: 'Reorder Midnight City' })
    await handle.focus()
    await page.keyboard.press('ArrowUp')
    await expect.poll(() => titles(page)).toEqual(['Bohemian Rhapsody', 'Midnight City', "Don't Stop Me Now"])
    await expect(handle).toBeFocused()
    await page.keyboard.press('ArrowUp')
    await expect.poll(() => titles(page)).toEqual(['Midnight City', 'Bohemian Rhapsody', "Don't Stop Me Now"])
    await expect(handle).toBeFocused()
    // At the top: nothing further, and nothing is sent for it.
    await page.keyboard.press('ArrowUp')
    await expect.poll(() => reorderCalls(mock).length).toBe(2)
    expect(reorderCalls(mock).at(-1)!.body).toEqual({ ordered_item_ids: [203, 201, 202] })
  })

  test('dragging a row by its handle opens a gap and drops it there', async ({ page }) => {
    const mock = await openWorkout(page)
    const rows = page.locator('.playlist-reorder-row')
    const handle = rows.nth(2).locator('.queue-drag-handle')
    const from = (await handle.boundingBox())!
    const to = (await rows.nth(0).boundingBox())!
    await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2)
    await page.mouse.down()
    await page.mouse.move(from.x + from.width / 2, to.y + 4, { steps: 8 })
    // Mid-drag: the held row is see-through and the rows it passed slid down.
    await expect(rows.nth(2)).toHaveClass(/reorder-dragging/)
    await expect(rows.nth(0)).toHaveAttribute('style', /translateY\(\d/)
    await page.mouse.up()
    await expect.poll(() => titles(page)).toEqual(['Midnight City', 'Bohemian Rhapsody', "Don't Stop Me Now"])
    await expect.poll(() => reorderCalls(mock).length).toBe(1)
    expect(reorderCalls(mock)[0].body).toEqual({ ordered_item_ids: [203, 201, 202] })
  })

  test('a refused save puts the order back and says so', async ({ page }) => {
    await openWorkout(page)
    await page.route(/\/api\/playlists\/2\/reorder$/, (route) =>
      route.fulfill({ status: 409, json: { detail: 'The playlist changed on another device.' } }),
    )
    await page.getByRole('button', { name: 'Reorder Bohemian Rhapsody' }).focus()
    await page.keyboard.press('ArrowDown')
    await expect(page.locator('.toast-error')).toContainText('The playlist changed on another device.')
    await expect.poll(() => titles(page)).toEqual(['Bohemian Rhapsody', "Don't Stop Me Now", 'Midnight City'])
  })

  test('only a playlist you own offers Reorder: not Favorites, not a generated mix', async ({ page }) => {
    await installApiMock(page)
    for (const id of [1, 3, 4]) {
      await page.goto(`/#/playlists/${id}`)
      await expect(page.locator('.collection-detail-copy, .track-row').first()).toBeVisible()
      await expect(page.getByRole('button', { name: 'Reorder', exact: true })).toHaveCount(0)
    }
  })

  test('Done returns to the track list', async ({ page }) => {
    await openWorkout(page)
    await page.getByRole('button', { name: 'Done', exact: true }).click()
    await expect(page.locator('.playlist-reorder-row')).toHaveCount(0)
    await expect(page.locator('.track-row').first()).toBeVisible()
  })
})

test.describe('hearts while they save', () => {
  const SONG_PREFERENCE = /\/api\/media\/\d+\/preference$/

  async function songRow(page: Page) {
    await installApiMock(page)
    await page.goto('/#/songs')
    const row = page.locator('.track-row').filter({ hasText: 'Bohemian Rhapsody' })
    await expect(row.getByRole('button', { name: 'Not liked: Bohemian Rhapsody. Press to like' })).toBeVisible()
    return row
  }

  /** Every accessible name the row's heart has had from now on. */
  async function watchHeartLabels(page: Page) {
    await page.evaluate(() => {
      const w = window as Window & { __heartLabels?: string[] }
      const row = [...document.querySelectorAll('.track-row')].find((r) => r.textContent?.includes('Bohemian Rhapsody'))!
      const heart = row.querySelector('.favorite-btn')!
      w.__heartLabels = [heart.getAttribute('aria-label') ?? '']
      new MutationObserver(() => {
        const label = heart.getAttribute('aria-label') ?? ''
        if (w.__heartLabels!.at(-1) !== label) w.__heartLabels!.push(label)
      }).observe(heart, { attributes: true, attributeFilter: ['aria-label'] })
    })
    return () => page.evaluate(() => (window as Window & { __heartLabels?: string[] }).__heartLabels ?? [])
  }

  test('a song heart shows the next state at once, cannot be pressed again, and keeps it', async ({ page }) => {
    const row = await songRow(page)
    const labels = await watchHeartLabels(page)
    const requests: string[] = []
    page.on('request', (request) => {
      if (SONG_PREFERENCE.test(new URL(request.url()).pathname) && request.method() === 'PUT') requests.push(request.url())
    })
    const release = await hold(page, SONG_PREFERENCE)
    await row.getByRole('button', { name: 'Not liked: Bohemian Rhapsody. Press to like' }).click()
    const liked = row.getByRole('button', { name: 'Liked: Bohemian Rhapsody. Press to dislike' })
    await expect(liked).toBeDisabled()
    release()
    await expect(liked).toBeEnabled()
    // Straight from "not liked" to "liked": no step back to the old state
    // between the save ending and the real state taking over.
    expect(await labels()).toEqual([
      'Not liked: Bohemian Rhapsody. Press to like',
      'Liked: Bohemian Rhapsody. Press to dislike',
    ])
    expect(requests).toHaveLength(1)
  })

  test('a dislike the server refuses dims the row only while it saves, then puts both back and says so', async ({ page }) => {
    const row = await songRow(page)
    await row.getByRole('button', { name: 'Not liked: Bohemian Rhapsody. Press to like' }).click()
    const liked = row.getByRole('button', { name: 'Liked: Bohemian Rhapsody. Press to dislike' })
    await expect(liked).toBeEnabled()

    let refuse!: () => void
    const gate = new Promise<void>((resolve) => {
      refuse = resolve
    })
    await page.route(SONG_PREFERENCE, async (route) => {
      await gate
      await route.fulfill({ status: 500, json: { detail: 'boom' } })
    })
    await liked.click()
    await expect(row.getByRole('button', { name: 'Disliked: Bohemian Rhapsody. Press to clear the dislike' })).toBeDisabled()
    await expect(row).toHaveClass(/track-row-disliked/)
    refuse()
    await expect(page.locator('.toast-error')).toContainText('Couldn\'t update "Bohemian Rhapsody".')
    await expect(liked).toBeEnabled()
    await expect(row).not.toHaveClass(/track-row-disliked/)
  })

  test('an album heart the server refuses goes back and says so', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/albums/Queen/A%20Night%20at%20the%20Opera')
    const header = page.locator('.collection-detail')
    await page.route(/\/api\/preferences\/album/, (route) =>
      route.request().method() === 'PUT' ? route.fulfill({ status: 500, json: { detail: 'boom' } }) : route.fallback(),
    )
    await header.getByRole('button', { name: /^Not liked: .*Press to like$/ }).click()
    await expect(page.locator('.toast-error')).toContainText("Couldn't update this album.")
    await expect(header.getByRole('button', { name: /^Not liked: .*Press to like$/ })).toBeEnabled()
  })
})

test.describe('Settings: account and privacy', () => {
  test('Sign out everywhere asks first, calls the server, and lands on the login page', async ({ page }) => {
    const mock = await openApp(page)
    const dialog = await openSettingsSection(page, 'Account')
    await dialog.getByRole('button', { name: 'Sign out everywhere' }).click()
    await answerDialog(page, 'dismiss')
    expect(mock.calls.filter((c) => c.path === '/api/auth/logout-everywhere')).toHaveLength(0)
    await dialog.getByRole('button', { name: 'Sign out everywhere' }).click()
    await answerDialog(page, 'accept')
    await expect(page.getByLabel('Username')).toBeVisible()
    expect(mock.calls.filter((c) => c.path === '/api/auth/logout-everywhere')).toHaveLength(1)
  })

  test('the ListenBrainz recommendations switch saves, and goes back when the server refuses', async ({ page }) => {
    const mock = await openApp(page)
    const dialog = await openSettingsSection(page, 'Discovery')
    const sw = dialog.getByRole('switch', { name: 'ListenBrainz recommendations' })
    await expect(sw).toBeChecked()
    await expect(dialog.getByText('asks ListenBrainz about the tracks', { exact: false })).toBeVisible()
    await sw.click()
    await expect(sw).not.toBeChecked()
    await expect
      .poll(() => mock.calls.filter((c) => c.method === 'PUT' && c.path === '/api/auth/privacy-settings').at(-1)?.body)
      .toEqual({ online_recommendations: false })

    await page.route(/\/api\/auth\/privacy-settings$/, (route) =>
      route.request().method() === 'PUT' ? route.fulfill({ status: 500, json: { detail: 'Not saved.' } }) : route.fallback(),
    )
    await sw.click()
    await expect(dialog.getByText('Not saved.')).toBeVisible()
    await expect(sw).not.toBeChecked()
  })
})

test.describe('who can change artwork', () => {
  async function openAlbumMenu(page: Page) {
    await page.goto('/#/songs')
    await page.getByRole('button', { name: 'More actions for Bohemian Rhapsody' }).click()
    return page.getByRole('menu')
  }

  test('an admin is offered Choose from found artwork and Upload artwork', async ({ page }) => {
    await installApiMock(page)
    const menu = await openAlbumMenu(page)
    await expect(menu.getByRole('menuitem', { name: 'Choose from found artwork…' })).toBeVisible()
    await expect(menu.getByRole('menuitem', { name: 'Upload artwork…' })).toBeVisible()
  })

  test('anyone else is offered neither, for a track, an album or an artist; a playlist keeps its own', async ({ page }) => {
    const mock = await installApiMock(page)
    mock.user.is_admin = false
    const menu = await openAlbumMenu(page)
    await expect(menu.getByRole('menuitem', { name: 'Download' })).toBeVisible()
    await expect(menu.getByRole('menuitem', { name: /artwork|Reset art/ })).toHaveCount(0)
    await page.keyboard.press('Escape')

    await page.goto('/#/albums')
    await page.getByRole('button', { name: 'More actions for A Night at the Opera', exact: true }).click()
    await expect(page.getByRole('menuitem', { name: 'Download' })).toBeVisible()
    await expect(page.getByRole('menuitem', { name: /artwork|Reset art/ })).toHaveCount(0)
    await page.keyboard.press('Escape')

    await page.goto('/#/artists')
    await page.getByRole('button', { name: 'More actions for Queen', exact: true }).click()
    await expect(page.getByRole('menuitem', { name: /artwork|Reset art/ })).toHaveCount(0)
    await page.keyboard.press('Escape')

    await page.goto('/#/playlists')
    await page.getByRole('button', { name: 'More actions for Workout', exact: true }).click()
    await expect(page.getByRole('menuitem', { name: 'Upload artwork…' })).toBeVisible()
  })
})

test.describe('counts read as English', () => {
  test('"1 track" and "2 tracks" in the toast after adding a collection to the queue', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/albums')
    await page.getByRole('button', { name: 'More actions for A Night at the Opera', exact: true }).click()
    await page.getByRole('menuitem', { name: 'Add last in queue' }).click()
    await expect(page.locator('.toast-success')).toContainText(/Added \d+ tracks? from/)
    await expect(page.locator('.toast-success')).not.toContainText('1 tracks')
  })
})

test.describe('video cards', () => {
  test('the card is a real play button with the ⋯ menu beside it, not inside it', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/videos')
    const card = page.locator('.video-card').filter({ hasText: 'Episode One' })
    await expect(card.getByRole('button', { name: 'Play Episode One' })).toBeVisible()
    expect(await card.evaluate((el) => el.getAttribute('role'))).toBeNull()
    // No source comment may leak into the page as text.
    await expect(page.locator('.video-grid')).not.toContainText('//')
    // Nothing interactive inside another interactive element.
    expect(await card.locator('button button, a button, button a').count()).toBe(0)
    await card.getByRole('button', { name: 'More actions for Episode One' }).click()
    await expect(page.getByRole('menuitem', { name: 'Download' })).toBeVisible()
  })

  test('Enter on the focused play button plays it', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/videos')
    await page.getByRole('button', { name: 'Play Episode One' }).focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('dialog', { name: 'Now Playing' }).or(player(page))).toBeVisible()
  })
})

test.describe('the device picker', () => {
  for (const [name, thisDeviceFlagged] of [
    ['this device is connected', true],
    ['this device is not flagged connected yet', false],
  ] as const) {
    test(`offers "(this device)" while the active device is offline (${name})`, async ({ page }) => {
      const mock = await installApiMock(page)
      const [self, other] = mock.playback.devices
      other.is_active = true
      other.is_connected = false
      self.is_active = false
      self.is_connected = thisDeviceFlagged
      mock.playback.active_device_id = other.id
      await page.goto('/#/home')
      await expect(page.locator('.home-view')).toBeVisible()
      await page.getByRole('button', { name: /^Choose playback device/ }).click()
      const menu = page.getByRole('menu', { name: 'Playback devices' })
      await expect(menu.getByRole('menuitemradio', { name: /\(this device\)/ })).toBeVisible()
      // The offline one is not offered as a place to play.
      await expect(menu.getByRole('menuitemradio', { name: /Living room/ })).toHaveCount(0)
    })
  }

  test('Now Playing leaves the device to the header picker', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await installApiMock(page)
    await page.goto('/#/songs')
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    await expect(page.getByRole('region', { name: 'Now playing', exact: true }).getByRole('button', { name: /^Playing on/ })).toHaveCount(0)
    await expect(page.getByRole('button', { name: /^Choose playback device/ })).toBeVisible()
  })
})
