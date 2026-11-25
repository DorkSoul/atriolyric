// Long lists render only the rows near the viewport: the Files
// tab's folders and files, and a playlist in reorder mode.
import { type Locator, type Page } from 'playwright/test'
import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'

/** Scrolls the app's scroll container (where these lists live) to `top`. */
async function scrollMain(page: Page, top: number | 'end') {
  await page.locator('.app-main').evaluate((el, to) => {
    el.scrollTo({ top: to === 'end' ? el.scrollHeight : to, behavior: 'instant' })
  }, top)
}

test.describe('the Files tab with thousands of entries in one folder', () => {
  test('renders a bounded number of folder and file rows and reaches the last of each', async ({ page }) => {
    const mock = await installApiMock(page)
    const template = mock.tracks[0]
    const items = Array.from({ length: 5000 }, (_, i) => ({ ...template, id: 100_000 + i, title: `File ${i}` }))
    const directories = Array.from({ length: 2000 }, (_, i) => ({
      name: `Folder ${i}`,
      path: `Folder ${i}`,
      is_ignored: false,
      ignored_directly: false,
      ignored_by: null,
    }))
    await page.route(/\/api\/library\/browse\?/, (route) =>
      route.fulfill({ json: { root_id: 1, path: '', parent_path: null, directories, items } }),
    )
    await page.goto('/#/files/1')
    const folders = page.getByRole('list', { name: 'Folders' }).locator('li.files-row')
    const files = page.getByRole('list', { name: 'Files' }).locator('li.files-row')
    await expect(folders.first()).toContainText('Folder 0')
    const bounded = async () => {
      expect(await folders.count()).toBeLessThan(150)
      expect(await files.count()).toBeLessThan(150)
    }
    await bounded()

    // The middle of the folders.
    await scrollMain(page, 1000 * 45)
    const folderNames = page.locator('.files-folder-row .files-name')
    await expect(folderNames.first()).not.toHaveText('Folder 0')
    const middle = Number((await folderNames.nth(10).textContent())!.replace('Folder ', ''))
    expect(middle).toBeGreaterThan(500)
    expect(middle).toBeLessThan(1500)
    await expect(folderNames.nth(10)).toBeInViewport()
    await bounded()

    // The end: the last file, and no folder rows left in the page.
    await scrollMain(page, 'end')
    await expect(files.filter({ hasText: 'File 4999' })).toBeInViewport()
    expect(await folders.count()).toBe(0)
    await bounded()
    // The spacers stand in for the rest: scrolling back up finds the first file.
    await page.getByRole('list', { name: 'Files' }).evaluate((el) => el.scrollIntoView({ block: 'start', behavior: 'instant' }))
    await expect(files.first().locator('.files-name')).toHaveText('File 0')
    await expect(files.first()).toBeInViewport()
  })
})

test.describe('reordering a playlist of 5,000 tracks', () => {
  async function openLongPlaylist(page: Page) {
    const mock = await installApiMock(page)
    const template = mock.tracks[0]
    mock.playlistDetails[2].items = Array.from({ length: 5000 }, (_, i) => ({
      ...template,
      id: 100_000 + i,
      title: `Song ${i}`,
      playlist_item_id: 200_000 + i,
    }))
    mock.playlists.find((playlist) => playlist.id === 2)!.item_count = 5000
    await page.goto('/#/playlists/2')
    await expect(page.getByRole('heading', { name: 'Workout', exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Reorder', exact: true }).click()
    await expect(page.locator('.playlist-reorder-row').first()).toContainText('Song 0')
    return mock
  }
  const rows = (page: Page) => page.locator('.playlist-reorder-row')
  const reorderCalls = (mock: Awaited<ReturnType<typeof installApiMock>>) =>
    mock.calls.filter((c) => c.method === 'PUT' && c.path === '/api/playlists/2/reorder')
  const handleOf = (row: Locator) => row.locator('[data-reorder-control="handle"]')

  test('renders a bounded number of rows, each saying where it is in the playlist', async ({ page }) => {
    await openLongPlaylist(page)
    expect(await rows(page).count()).toBeLessThan(150)
    await expect(rows(page).first()).toHaveAttribute('aria-posinset', '1')
    await expect(rows(page).first()).toHaveAttribute('aria-setsize', '5000')
    await scrollMain(page, 'end')
    await expect(rows(page).filter({ hasText: 'Song 4999' })).toBeInViewport()
    expect(await rows(page).count()).toBeLessThan(150)
  })

  test('the arrow keys and Move buttons still move a row and keep focus on the control', async ({ page }) => {
    const mock = await openLongPlaylist(page)
    const handle = page.getByRole('button', { name: 'Reorder Song 0' })
    await handle.focus()
    await page.keyboard.press('ArrowDown')
    await expect(rows(page).nth(1)).toContainText('Song 0')
    await expect(handle).toBeFocused()
    await expect(page.getByRole('status').filter({ hasText: 'Moved Song 0 to position 2 of 5000.' })).toBeAttached()
    await expect.poll(() => reorderCalls(mock).length).toBe(1)
    const saved = reorderCalls(mock)[0].body as { ordered_item_ids: number[] }
    expect(saved.ordered_item_ids).toHaveLength(5000)
    expect(saved.ordered_item_ids.slice(0, 3)).toEqual([200_001, 200_000, 200_002])

    await page.keyboard.press('ArrowDown')
    await expect(rows(page).nth(2)).toContainText('Song 0')
    await expect(handle).toBeFocused()
    await expect(page.getByRole('button', { name: /^Move Song 0 (up|down)$/ })).toHaveCount(0)
  })

  test('a row moved out of the rendered window is scrolled back into view with focus kept', async ({ page }) => {
    await openLongPlaylist(page)
    // The last rendered row sits below the viewport (the overscan); focus its
    // handle without scrolling, then move it one further down - out of the
    // window.
    const last = rows(page).last()
    const title = (await last.locator('.track-title').textContent())!
    const position = Number(await last.getAttribute('aria-posinset'))
    await expect(last).not.toBeInViewport()
    await handleOf(last).evaluate((el) => (el as HTMLElement).focus({ preventScroll: true }))
    await page.keyboard.press('ArrowDown')
    const moved = page.getByRole('button', { name: `Reorder ${title}` })
    await expect(moved).toBeFocused()
    await expect(moved).toBeInViewport()
    await expect(page.locator(`.playlist-reorder-row[aria-posinset="${position + 1}"]`)).toContainText(title)
    expect(await rows(page).count()).toBeLessThan(150)
  })
})
