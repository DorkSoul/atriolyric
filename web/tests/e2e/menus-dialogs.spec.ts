import { type Page } from 'playwright/test'
import { formatDuration } from '../../src/formatDuration'
import { answerDialog } from './dialogs'
import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'
import { installWantListMock } from './wantListMock'
import { devicePickerToggle, player, openSettings } from './app-helpers'

// The shared Menu (role="menu", roving focus) and ConfirmDialog (the
// replacement for window.confirm/alert/prompt).

async function songRow(page: Page, title = 'Bohemian Rhapsody') {
  await page.goto('/#/songs')
  const row = page.locator('.track-row').filter({ hasText: title })
  await expect(row).toBeVisible()
  return row
}

test.describe('Menu', () => {
  test('a row menu opens from the keyboard, moves with the arrows and Escape returns focus', async ({ page }) => {
    await installApiMock(page)
    const row = await songRow(page)
    const trigger = row.getByRole('button', { name: 'More actions for Bohemian Rhapsody' })
    await expect(trigger).toHaveAttribute('aria-haspopup', 'menu')
    await expect(trigger).toHaveAttribute('aria-expanded', 'false')
    await expect(trigger).not.toHaveAttribute('aria-controls', /./)

    await trigger.focus()
    await page.keyboard.press('Enter')
    const menu = page.getByRole('menu', { name: 'Actions for Bohemian Rhapsody' })
    await expect(menu).toBeVisible()
    await expect(trigger).toHaveAttribute('aria-expanded', 'true')
    await expect(trigger).toHaveAttribute('aria-controls', (await menu.getAttribute('id'))!)

    const items = menu.getByRole('menuitem')
    await expect(items.first()).toBeFocused()
    const count = await items.count()
    expect(count).toBeGreaterThan(4)
    await page.keyboard.press('ArrowDown')
    await expect(items.nth(1)).toBeFocused()
    await page.keyboard.press('End')
    await expect(items.nth(count - 1)).toBeFocused()
    await page.keyboard.press('ArrowDown')
    await expect(items.first()).toBeFocused()
    await page.keyboard.press('ArrowUp')
    await expect(items.nth(count - 1)).toBeFocused()
    await page.keyboard.press('Home')
    await expect(items.first()).toBeFocused()
    // Exactly one item is in the tab order (roving tabindex).
    expect(await menu.locator('[role="menuitem"][tabindex="0"]').count()).toBe(1)

    // Typing a letter jumps to the next item starting with it.
    await page.keyboard.press('g')
    await expect(menu.getByRole('menuitem', { name: 'Generate mix from this song' })).toBeFocused()

    await page.keyboard.press('Escape')
    await expect(menu).toHaveCount(0)
    await expect(trigger).toBeFocused()
    await expect(trigger).toHaveAttribute('aria-expanded', 'false')
  })

  test('Tab closes the menu and a press outside closes it', async ({ page }) => {
    await installApiMock(page)
    const row = await songRow(page)
    const trigger = row.getByRole('button', { name: 'More actions for Bohemian Rhapsody' })
    await trigger.click()
    const menu = page.getByRole('menu')
    await expect(menu).toBeVisible()
    await page.keyboard.press('Tab')
    await expect(menu).toHaveCount(0)
    expect(await page.evaluate(() => document.activeElement?.getAttribute('role'))).not.toBe('menuitem')

    await trigger.click()
    await expect(menu).toBeVisible()
    await page.locator('.app-header h1').click()
    await expect(menu).toHaveCount(0)
  })

  test('right-click opens the menu with focus inside it', async ({ page }) => {
    await installApiMock(page)
    const row = await songRow(page)
    await row.click({ button: 'right' })
    const menu = page.getByRole('menu', { name: 'Actions for Bohemian Rhapsody' })
    await expect(menu).toBeVisible()
    await expect(menu.getByRole('menuitem').first()).toBeFocused()
    await page.keyboard.press('Escape')
    await expect(menu).toHaveCount(0)
  })

  test('the playlist picker opens with Right, goes back with Left or Escape, and Enter adds', async ({ page }) => {
    const mock = await installApiMock(page)
    const row = await songRow(page)
    await row.getByRole('button', { name: 'More actions for Bohemian Rhapsody' }).click()
    const menu = page.getByRole('menu')
    const addToPlaylist = menu.getByRole('menuitem', { name: 'Add to playlist' })
    await expect(addToPlaylist).toHaveAttribute('aria-haspopup', 'menu')
    await addToPlaylist.focus()
    await page.keyboard.press('ArrowRight')
    await expect(menu.getByRole('menuitem', { name: 'Back' })).toBeFocused()
    await page.keyboard.press('ArrowLeft')
    await expect(addToPlaylist).toBeFocused()

    await page.keyboard.press('Enter')
    await expect(menu.getByRole('menuitem', { name: 'Workout' })).toBeVisible()
    // Escape steps back a level instead of closing everything.
    await page.keyboard.press('Escape')
    await expect(addToPlaylist).toBeFocused()
    await expect(menu).toBeVisible()

    await page.keyboard.press('ArrowRight')
    await menu.getByRole('menuitem', { name: 'Workout' }).focus()
    await page.keyboard.press('Enter')
    await expect(menu).toHaveCount(0)
    await expect(page.getByText('Added "Bohemian Rhapsody" to "Workout".', { exact: true })).toBeVisible()
    expect(mock.calls.some((c) => c.method === 'POST' && /\/api\/playlists\/\d+\/items/.test(c.path))).toBe(true)
  })

  test('a menu item does its action and does not also play the row', async ({ page }) => {
    const mock = await installApiMock(page)
    const row = await songRow(page)
    await row.getByRole('button', { name: 'More actions for Bohemian Rhapsody' }).focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('menuitem', { name: 'Add next in queue' })).toBeFocused()
    await page.keyboard.press('Enter')
    await expect.poll(() => mock.wsMessages.some((m) => m.type === 'add_to_queue' && m.mode === 'next')).toBe(true)
    expect(mock.wsMessages.some((m) => m.type === 'set_queue_context')).toBe(false)
    await expect(page.getByRole('menu')).toHaveCount(0)
    await expect(row.getByRole('button', { name: 'More actions for Bohemian Rhapsody' })).toBeFocused()
  })

  test('an album card menu item does not open the album', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/albums')
    await page.getByRole('button', { name: 'More actions for A Night at the Opera' }).click()
    const menu = page.getByRole('menu', { name: /Actions for A Night at the Opera/ })
    await menu.getByRole('menuitem', { name: 'Add last in queue' }).click()
    await expect(menu).toHaveCount(0)
    await expect(page).toHaveURL(/#\/albums$/)
  })

  test('the settings menu is a menu: Enter opens it, Escape closes it and focus returns', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/home')
    await expect(page.locator('.home-view')).toBeVisible()
    const trigger = page.getByRole('button', { name: 'Settings', exact: true })
    await trigger.focus()
    await page.keyboard.press('Enter')
    const menu = page.getByRole('menu', { name: 'Settings' })
    await expect(menu.getByRole('menuitem').first()).toBeFocused()
    await expect(menu.getByRole('menuitem', { name: 'Log out' })).toBeVisible()
    await expect(menu.getByRole('separator')).toHaveCount(1)
    await page.keyboard.press('Escape')
    await expect(menu).toHaveCount(0)
    await expect(trigger).toBeFocused()

    // Opening a section closes the menu and goes to that section of the
    // Settings page; Back returns to where the user was.
    await trigger.click()
    await menu.getByRole('menuitem', { name: 'Appearance' }).click()
    await expect(menu).toHaveCount(0)
    await expect(page).toHaveURL(/#\/settings\/appearance$/)
    await expect(page.locator('#settings-appearance')).toBeFocused()
    await page.goBack()
    await expect(page).toHaveURL(/#\/home$/)
    await expect(page.locator('.home-view')).toBeVisible()
  })

  test('the sort menu exposes the chosen order as a radio item', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/albums')
    const toggle = page.getByRole('button', { name: 'Sort' })
    await toggle.click()
    const menu = page.getByRole('menu', { name: 'Sort by' })
    const checked = menu.getByRole('menuitemradio', { checked: true })
    await expect(checked).toHaveCount(1)
    // Focus starts on the checked choice.
    await expect(checked).toBeFocused()
    await page.keyboard.press('ArrowDown')
    await page.keyboard.press('Enter')
    await expect(menu).toHaveCount(0)
    await expect(toggle).toBeFocused()
  })

  test('the device picker lists devices as radio items', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/home')
    const toggle = devicePickerToggle(page)
    await toggle.click()
    const menu = page.getByRole('menu', { name: 'Playback devices' })
    await expect(menu.getByRole('menuitemradio').first()).toBeVisible()
    await expect(menu.getByRole('menuitemradio', { checked: true })).toHaveCount(1)
    await page.keyboard.press('Escape')
    await expect(toggle).toBeFocused()
  })
})

test.describe('ConfirmDialog', () => {
  test('removing a playback device asks first; Cancel keeps it, Remove sends it', async ({ page }) => {
    const mock = await installApiMock(page)
    await page.goto('/#/home')
    const open = async () => {
      await devicePickerToggle(page).click()
      const remove = page.getByRole('menuitem', { name: /^Remove / }).first()
      await remove.click()
      return remove
    }
    await open()
    const dialog = page.getByRole('dialog', { name: 'Remove playback device' })
    await expect(dialog).toBeVisible()
    // A destructive question starts on its safe button.
    await expect(dialog.getByRole('button', { name: 'Cancel' })).toBeFocused()
    await expect(dialog.locator('.confirm-dialog-body')).toContainText('Remove playback device "')
    await answerDialog(page, 'dismiss')
    expect(mock.wsMessages.some((m) => m.type === 'remove_device')).toBe(false)

    // Answering the dialog was a press outside the menu: the picker closed.
    await open()
    await answerDialog(page, 'accept')
    await expect.poll(() => mock.wsMessages.some((m) => m.type === 'remove_device')).toBe(true)
  })

  test('Escape cancels and focus returns to what opened the dialog', async ({ page }) => {
    const mock = await installApiMock(page)
    await page.goto('/#/radio')
    const remove = page.getByRole('button', { name: /^Remove / }).first()
    await expect(remove).toBeVisible()
    await remove.focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('dialog', { name: 'Remove radio station' })).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(remove).toBeFocused()
    expect(mock.calls.some((c) => c.method === 'DELETE' && c.path.startsWith('/api/radio/stations/'))).toBe(false)

    await remove.click()
    await answerDialog(page, 'accept')
    await expect.poll(() => mock.calls.some((c) => c.method === 'DELETE' && c.path.startsWith('/api/radio/stations/'))).toBe(true)
  })

  test('deleting a playlist: Cancel keeps it', async ({ page }) => {
    const mock = await installApiMock(page)
    await page.goto('/#/playlists')
    const workout = page.locator('.browse-card-playlist').filter({ hasText: 'Workout' })
    await workout.getByRole('button', { name: 'Delete Workout' }).click()
    const message = await answerDialog(page, 'dismiss')
    expect(message).toBe('Delete playlist "Workout"? This cannot be undone.')
    expect(mock.calls.some((c) => c.method === 'DELETE' && /^\/api\/playlists\/\d+$/.test(c.path))).toBe(false)
    await expect(workout).toBeVisible()
  })

  test('removing a song from a playlist: Cancel keeps it', async ({ page }) => {
    const mock = await installApiMock(page)
    await page.goto('/#/playlists')
    await page.locator('.browse-card-main').filter({ hasText: 'Workout' }).click()
    const row = page.locator('.track-row').filter({ hasText: 'Bohemian Rhapsody' })
    await row.getByRole('button', { name: /^More actions for/ }).click()
    await page.getByRole('menuitem', { name: 'Remove from playlist' }).click()
    expect(await answerDialog(page, 'dismiss')).toBe('Remove "Bohemian Rhapsody" from this playlist?')
    expect(mock.calls.some((c) => c.method === 'DELETE' && c.path.includes('/items/'))).toBe(false)
    await expect(row).toBeVisible()
  })

  test('creating a playlist from an album asks for its name in a labelled field', async ({ page }) => {
    const mock = await installApiMock(page)
    await page.goto('/#/albums')
    const trigger = page.getByRole('button', { name: 'More actions for A Night at the Opera' })
    await trigger.click()
    await page.getByRole('menuitem', { name: /^Create playlist from/ }).click()
    const dialog = page.getByRole('dialog', { name: 'Create playlist' })
    const field = dialog.getByLabel('Create a playlist named:')
    await expect(field).toBeFocused()
    await expect(field).toHaveValue(/A Night at the Opera/)
    // Cancel makes nothing.
    await dialog.getByRole('button', { name: 'Cancel' }).click()
    await expect(dialog).toHaveCount(0)
    await expect(trigger).toBeFocused()
    expect(mock.calls.some((c) => c.method === 'POST' && c.path === '/api/playlists')).toBe(false)

    await trigger.click()
    await page.getByRole('menuitem', { name: /^Create playlist from/ }).click()
    await field.fill('Opera night')
    await page.keyboard.press('Enter')
    await expect.poll(() => mock.calls.find((c) => c.method === 'POST' && c.path === '/api/playlists')?.body.name).toBe('Opera night')
  })

  test('Clear owned in the want list asks first', async ({ page }) => {
    await installApiMock(page)
    await installWantListMock(page)
    const clears: string[] = []
    page.on('request', (request) => {
      if (request.method() === 'POST' && request.url().endsWith('/api/want-list/clear-owned')) clears.push(request.url())
    })
    await page.goto('/#/want-list')
    const button = page.getByRole('button', { name: 'Clear owned' })
    await button.click()
    await expect(page.getByRole('dialog', { name: 'Clear owned items' })).toBeVisible()
    await answerDialog(page, 'dismiss')
    expect(clears).toEqual([])
    await expect(button).toBeFocused()

    await button.click()
    await answerDialog(page, 'accept')
    await expect.poll(() => clears.length).toBe(1)
    await expect(page.getByText('Nothing owned to clear')).toBeVisible()
  })

  test('removing a song from the queue asks first', async ({ page }) => {
    // Below the side panel's 1200px, so Now Playing (a page) carries Up next.
    await page.setViewportSize({ width: 1100, height: 800 })
    const mock = await installApiMock(page)
    await page.goto('/#/songs')
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    const sheet = page.locator(':is(.nowplaying-overlay, .nowplaying-page)')
    const rows = sheet.locator('.queue-row')
    await expect(rows).toHaveCount(3)
    await rows.first().getByRole('button', { name: /^Remove .*queue$/ }).click()
    expect(await answerDialog(page, 'dismiss')).toMatch(/^Remove ".*" from the queue\?$/)
    expect(mock.wsMessages.some((m) => m.type === 'remove_from_queue')).toBe(false)
    await rows.first().getByRole('button', { name: /^Remove .*queue$/ }).click()
    await answerDialog(page, 'accept')
    await expect.poll(() => mock.wsMessages.some((m) => m.type === 'remove_from_queue')).toBe(true)
  })

  test('bulk-removing songs from the library: Cancel keeps them', async ({ page }) => {
    const mock = await installApiMock(page)
    const row = await songRow(page, "Don't Stop Me Now")
    await row.getByRole('button', { name: /^More actions for/ }).click()
    await page.getByRole('menuitem', { name: 'Select' }).click()
    await page.locator('.bulk-action-bar').getByRole('button', { name: 'Remove from library' }).click()
    expect(await answerDialog(page, 'dismiss')).toContain('original music files will not be changed or deleted')
    expect(mock.calls.some((c) => c.path === '/api/admin/media/bulk-remove')).toBe(false)
    await expect(row).toBeVisible()
  })

  test('disconnecting ListenBrainz: Cancel keeps the account linked', async ({ page }) => {
    const mock = await installApiMock(page)
    mock.integrations[0].linked = true
    await page.goto('/#/home')
    await openSettings(page, 'Scrobbling')
    await page.locator('[data-provider="listenbrainz"]').getByRole('button', { name: 'Disconnect' }).click()
    expect(await answerDialog(page, 'dismiss')).toBe('Disconnect your ListenBrainz account?')
    expect(mock.calls.some((c) => c.method === 'DELETE' && c.path === '/api/integrations/listenbrainz')).toBe(false)
  })

  test('"Install as PWA" explains itself in a dialog, not a native alert', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/home')
    const trigger = page.getByRole('button', { name: 'Settings', exact: true })
    await trigger.click()
    await page.getByRole('menuitem', { name: 'Install as PWA' }).click()
    const dialog = page.getByRole('dialog', { name: 'Install as PWA' })
    await expect(dialog).toContainText('could not open the install dialog')
    // An alert has one button, and it is the one focused.
    await expect(dialog.getByRole('button')).toHaveCount(2) // OK and the close ✕
    await expect(dialog.getByRole('button', { name: 'OK' })).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(dialog).toHaveCount(0)
    await expect(trigger).toBeFocused()
  })

  // Which explanation is given depends on whether the browser has the install
  // prompt event at all (feature detection), not on its name.
  for (const hasPromptEvent of [false, true]) {
    test(`"Install as PWA" explains a browser ${hasPromptEvent ? 'whose prompt has not come' : 'with no install prompt'}`, async ({ page }) => {
      await page.addInitScript((has) => {
        delete (Window.prototype as unknown as Record<string, unknown>).onbeforeinstallprompt
        delete (window as unknown as Record<string, unknown>).onbeforeinstallprompt
        if (has) Object.defineProperty(window, 'onbeforeinstallprompt', { value: null, writable: true, configurable: true })
      }, hasPromptEvent)
      await installApiMock(page)
      await page.goto('/#/home')
      await page.getByRole('button', { name: 'Settings', exact: true }).click()
      await page.getByRole('menuitem', { name: 'Install as PWA' }).click()
      const dialog = page.getByRole('dialog', { name: 'Install as PWA' })
      await expect(dialog).toContainText(
        hasPromptEvent
          ? 'This browser does not provide the automatic install prompt'
          : 'This browser does not support the automatic install prompt',
      )
    })
  }
})

test('formatDuration shows hours from one hour up and floors seconds', () => {
  expect(formatDuration(0)).toBe('0:00')
  expect(formatDuration(65.9)).toBe('1:05')
  expect(formatDuration(3599)).toBe('59:59')
  expect(formatDuration(3600)).toBe('1:00:00')
  expect(formatDuration(5712)).toBe('1:35:12')
  expect(formatDuration(null)).toBe('')
  expect(formatDuration(undefined)).toBe('')
  expect(formatDuration(Number.NaN)).toBe('0:00')
})
