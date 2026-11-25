import { type Page } from 'playwright/test'
import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'
import { player, nowPlayingView } from './app-helpers'

// Now Playing as a real dialog, the queue without a mouse, and its sliders.

async function playFirstSong(page: Page) {
  await page.goto('/#/songs')
  await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
  await expect(player(page)).toContainText('Bohemian Rhapsody')
  return player(page).getByRole('button', { name: 'Open now playing' })
}

const insideDialog = (page: Page) =>
  page.evaluate(() => document.querySelector('.nowplaying-content')?.contains(document.activeElement) ?? false)

// A phone keeps Now Playing as a full-screen dialog (a desktop or tablet
// shows it as a page: see the describe below).
test.describe('Now Playing is a dialog on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test('opens labelled by the track, takes and traps focus, makes the page inert and Escape restores everything', async ({ page }) => {
    await installApiMock(page)
    const opener = await playFirstSong(page)
    await opener.focus()
    await page.keyboard.press('Enter')

    const dialog = nowPlayingView(page)
    await expect(dialog).toBeVisible()
    await expect(dialog).toHaveAttribute('aria-modal', 'true')
    await expect(dialog).toBeFocused()
    // The overlay keeps the class names the stylesheet and other tests use.
    await expect(page.locator(':is(.nowplaying-overlay, .nowplaying-page) > .nowplaying-content')).toHaveCount(1)
    expect(await page.evaluate(() => (document.querySelector('.app-shell') as HTMLElement).inert)).toBe(true)

    // Tab and Shift+Tab never leave the dialog.
    for (let i = 0; i < 40; i++) {
      await page.keyboard.press('Tab')
      expect(await insideDialog(page)).toBe(true)
    }
    for (let i = 0; i < 5; i++) {
      await page.keyboard.press('Shift+Tab')
      expect(await insideDialog(page)).toBe(true)
    }

    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    await expect(opener).toBeFocused()
    expect(await page.evaluate(() => (document.querySelector('.app-shell') as HTMLElement).inert)).toBe(false)
    // Closing popped the history entry that opening pushed.
    expect(await page.evaluate(() => Boolean(window.history.state?.nowPlaying))).toBe(false)
    await expect(page).toHaveURL(/#\/songs$/)
  })

  test('the Back button closes it and focus returns to the opener', async ({ page }) => {
    await installApiMock(page)
    const opener = await playFirstSong(page)
    await opener.click()
    const dialog = nowPlayingView(page)
    await expect(dialog).toBeVisible()
    await page.goBack()
    await expect(dialog).toHaveCount(0)
    await expect(page).toHaveURL(/#\/songs$/)
    await expect(opener).toBeFocused()
  })

  test('Escape closes a menu opened inside it first, then the dialog', async ({ page }) => {
    await installApiMock(page)
    const opener = await playFirstSong(page)
    await opener.click()
    const dialog = nowPlayingView(page)
    const more = dialog.getByRole('button', { name: 'More actions for Bohemian Rhapsody' })
    await more.click()
    const menu = page.getByRole('menu', { name: 'Actions for Bohemian Rhapsody' })
    await expect(menu.getByRole('menuitem').first()).toBeFocused()
    await page.keyboard.press('Escape')
    await expect(menu).toHaveCount(0)
    await expect(dialog).toBeVisible()
    await expect(more).toBeFocused()
    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
  })

  test('a confirmation raised inside it sits above it and hands focus back', async ({ page }) => {
    await installApiMock(page)
    const opener = await playFirstSong(page)
    await opener.click()
    const dialog = nowPlayingView(page)
    const remove = dialog.locator('.queue-row').first().getByRole('button', { name: /^Remove .* from queue$/ })
    await remove.click()
    const confirm = page.getByRole('dialog', { name: 'Remove from queue' })
    await expect(confirm).toBeVisible()
    // The top dialog is the one that answers Escape.
    await page.keyboard.press('Escape')
    await expect(confirm).toHaveCount(0)
    await expect(dialog).toBeVisible()
    await expect(remove).toBeFocused()
  })
})

// On a desktop or tablet Now Playing is a page in the content area, not a
// dialog: the sidebar and top bar stay usable, nothing is inert, and its
// Back button, the browser's Back and Escape still close it.
test.describe('Now Playing is a page on a desktop', () => {
  test('opens as a page titled Now playing, keeps the shell, and Escape or Back close it', async ({ page }) => {
    await installApiMock(page)
    const opener = await playFirstSong(page)
    await opener.click()
    const view = nowPlayingView(page)
    await expect(view).toBeVisible()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(page.locator('.nowplaying-overlay')).toHaveCount(0)
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Now playing')
    await expect(page.getByRole('navigation').first()).toBeVisible()
    expect(await page.evaluate(() => (document.querySelector('.app-shell') as HTMLElement).inert)).toBe(false)

    await page.keyboard.press('Escape')
    await expect(view).toHaveCount(0)
    expect(await page.evaluate(() => Boolean(window.history.state?.nowPlaying))).toBe(false)
    await expect(page).toHaveURL(/#\/songs$/)

    await opener.click()
    await expect(view).toBeVisible()
    await page.goBack()
    await expect(view).toHaveCount(0)
    await expect(page).toHaveURL(/#\/songs$/)

    await opener.click()
    await view.getByRole('button', { name: 'Back' }).click()
    await expect(view).toHaveCount(0)
  })

  test('a sidebar tab leaves it, and Back from there returns to the page that was under it', async ({ page }) => {
    await installApiMock(page)
    const opener = await playFirstSong(page)
    await opener.click()
    const view = nowPlayingView(page)
    await expect(view).toBeVisible()
    await page.getByRole('navigation', { name: 'Tabs' }).getByRole('button', { name: /^Albums/ }).click()
    await expect(view).toHaveCount(0)
    await expect(page).toHaveURL(/#\/albums/)
    await page.goBack()
    await expect(page).toHaveURL(/#\/songs$/)
    await expect(view).toHaveCount(0)
  })

  test('Escape closes a menu opened inside it first, then the page', async ({ page }) => {
    await installApiMock(page)
    const opener = await playFirstSong(page)
    await opener.click()
    const view = nowPlayingView(page)
    const more = view.getByRole('button', { name: 'More actions for Bohemian Rhapsody' })
    await more.click()
    const menu = page.getByRole('menu', { name: 'Actions for Bohemian Rhapsody' })
    await expect(menu).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(menu).toHaveCount(0)
    await expect(view).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(view).toHaveCount(0)
  })
})

test('a press on the player bar art opens Now Playing even when the art finishes loading mid-press', async ({ page }) => {
  // The press goes down on the loading spinner and comes up on the picture
  // that replaced it. WebKit drops the click when the element the press
  // started on has left the page, so the tap must still count.
  await installApiMock(page)
  let releaseArt = () => {}
  const artHeld = new Promise<void>((resolve) => {
    releaseArt = resolve
  })
  await page.route(/\/api\/stream\/1\/art$/, async (route) => {
    await artHeld
    await route.fallback()
  })
  const opener = await playFirstSong(page)
  const spinner = opener.locator('.art-spinner')
  await expect(spinner).toBeVisible()
  const box = (await spinner.boundingBox())!
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.mouse.down()
  releaseArt()
  await expect(opener.locator('.art-img')).toBeVisible()
  await expect(spinner).toHaveCount(0)
  await page.mouse.up()
  await expect(nowPlayingView(page)).toBeVisible()
})

test.describe('queue rows', () => {
  // Narrower than the side panel's 1200px: the page carries Up next itself.
  test.use({ viewport: { width: 1100, height: 800 } })
  async function openQueue(page: Page) {
    const mock = await installApiMock(page)
    const opener = await playFirstSong(page)
    await opener.click()
    const dialog = nowPlayingView(page)
    const rows = dialog.locator('.queue-row')
    await expect(rows).toHaveCount(3)
    return { mock, dialog, rows }
  }

  test('the drag handle moves a row with the arrow keys, keeps focus and announces the position', async ({ page }) => {
    const { mock, dialog, rows } = await openQueue(page)
    const titles = () => rows.locator('.track-title').allTextContents()
    const before = await titles()
    const second = before[1]

    // No visible arrow buttons anywhere: the handle is the only reorder control.
    await expect(dialog.getByRole('button', { name: /^Move / })).toHaveCount(0)
    await rows.nth(1).getByRole('button', { name: `Reorder ${second}` }).focus()
    await page.keyboard.press('ArrowDown')
    await expect.poll(() => mock.wsMessages.find((m) => m.type === 'move_queue_item')?.direction).toBe(1)
    await expect.poll(titles).toEqual([before[0], before[2], before[1]])
    const handle = rows.filter({ hasText: second }).getByRole('button', { name: `Reorder ${second}` })
    await expect(handle).toBeFocused()
    await expect(dialog.locator('output')).toHaveText(/^Moved to position 3 of 3/)

    await page.keyboard.press('ArrowUp')
    await expect.poll(titles).toEqual(before)
    await expect(rows.nth(1).getByRole('button', { name: `Reorder ${second}` })).toBeFocused()
    await expect(dialog.locator('output')).toHaveText(/^Moved to position 2 of 3/)
    expect(mock.wsMessages.filter((m) => m.type === 'move_queue_item').map((m) => m.direction)).toEqual([1, -1])
  })

  test('nothing moves across the playing song or past the ends', async ({ page }) => {
    const { mock, rows } = await openQueue(page)
    const second = (await rows.nth(1).locator('.track-title').textContent())!
    // Directly under the current row: up would swap with it.
    await rows.nth(1).getByRole('button', { name: `Reorder ${second}` }).focus()
    await page.keyboard.press('ArrowUp')
    const last = (await rows.nth(2).locator('.track-title').textContent())!
    await rows.nth(2).getByRole('button', { name: `Reorder ${last}` }).focus()
    await page.keyboard.press('ArrowDown')
    expect(mock.wsMessages.some((m) => m.type === 'move_queue_item')).toBe(false)
  })
})

test.describe('sliders', () => {
  test('the Now Playing seek slider has a name, a value text, keyboard steps and a focus ring', async ({ page }) => {
    const mock = await installApiMock(page)
    const opener = await playFirstSong(page)
    await opener.click()
    const dialog = nowPlayingView(page)
    const slider = dialog.getByRole('slider', { name: 'Seek' })
    await expect(slider).toHaveAttribute('aria-valuetext', '0:00 of 5:55')

    await slider.focus()
    mock.wsMessages.length = 0
    await page.keyboard.press('ArrowRight')
    await expect.poll(() => mock.wsMessages.find((m) => m.type === 'seek')?.position_seconds).toBe(5)
    await page.keyboard.press('PageUp')
    await expect.poll(() => mock.wsMessages.filter((m) => m.type === 'seek').at(-1)?.position_seconds).toBeGreaterThanOrEqual(30)
    await page.keyboard.press('End')
    await expect.poll(() => mock.wsMessages.filter((m) => m.type === 'seek').at(-1)?.position_seconds).toBe(355)
    await page.keyboard.press('Home')
    await expect.poll(() => mock.wsMessages.filter((m) => m.type === 'seek').at(-1)?.position_seconds).toBe(0)

    // A keyboard-focused slider shows an outline. Reached with Tab, so every
    // engine treats it as keyboard focus.
    await page.keyboard.press('Shift+Tab')
    await page.keyboard.press('Tab')
    await expect(slider).toBeFocused()
    await expect(slider).toHaveCSS('outline-style', 'solid')
    await expect(slider).toHaveCSS('outline-width', '2px')
  })

  test('both volume sliders say their value', async ({ page }) => {
    await installApiMock(page)
    const opener = await playFirstSong(page)
    await expect(player(page).getByRole('slider', { name: 'Volume' })).toHaveAttribute('aria-valuetext', /^\d+%$/)
    await opener.click()
    const dialog = nowPlayingView(page)
    await expect(dialog.getByRole('slider', { name: 'Volume' })).toHaveAttribute('aria-valuetext', /^\d+%$/)
  })
})

test('hidden video controls come back when the keyboard reaches them', async ({ page }) => {
  await page.clock.install()
  await installApiMock(page)
  await page.addStyleTag({ content: '.toast-stack, .toast-stack * { pointer-events: none !important; }' })
  await page.goto('/#/videos')
  await page.locator('.browse-card').filter({ hasText: 'Shows' }).click()
  await page.locator('.video-card').filter({ hasText: 'Episode One' }).click()
  await expect(player(page)).toContainText('Episode One')
  await player(page).getByRole('button', { name: 'Open now playing' }).click()
  const overlay = page.locator('.nowplaying-overlay.video-mode')
  await expect(overlay).toBeVisible()
  // Playing, untouched: after the auto-hide interval the controls fade away.
  await page.clock.runFor(11_000)
  const hidden = page.locator('.nowplaying-overlay.video-mode.controls-hidden')
  await expect(hidden).toHaveCount(1)
  await page.keyboard.press('Tab')
  await expect(hidden).toHaveCount(0)
})
