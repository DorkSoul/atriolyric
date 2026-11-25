import { expect, test } from './fixtures'
import { type Page } from 'playwright/test'
import { installApiMock, mockCustomArt, type AppMockState } from './mockApi'
import { player } from './app-helpers'

test.use({ viewport: { width: 375, height: 800 } })

const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
  'base64',
)

async function open(
  page: Page,
  hash: string,
  hideDirectoryPicker = true,
  seed?: () => void,
): Promise<AppMockState> {
  if (hideDirectoryPicker) {
    await page.addInitScript(() => {
      // Phones and Firefox/Safari have no File System Access API.
      Object.defineProperty(window, 'showDirectoryPicker', { value: undefined, configurable: true })
    })
  }
  const mock = await installApiMock(page)
  seed?.()
  await page.goto(hash)
  return mock
}

function moreButton(page: Page, name: string) {
  return page.getByRole('button', { name: `More actions for ${name}`, exact: true })
}

function artCalls(mock: AppMockState, method: string) {
  return mock.calls.filter((c) => c.method === method && c.path.startsWith('/api/art/'))
}

test.describe('More actions menus, custom art and downloads', () => {
  test('shows a visible More actions button on every entity type without navigating', async ({ page }) => {
    await open(page, '/#/albums')
    await expect(page.locator('.browse-card-album').first()).toBeVisible()
    const albumMore = moreButton(page, 'A Night at the Opera')
    await expect(albumMore).toBeVisible()
    await albumMore.click()
    // Still on the album list, menu open with the new entries.
    await expect(page.locator('.browse-card-album').first()).toBeVisible()
    const menu = page.locator('.track-menu-panel')
    await expect(menu.getByRole('menuitem', { name: 'Upload artwork…' })).toBeVisible()
    await expect(menu.getByRole('menuitem', { name: 'Download' })).toBeVisible()
    await expect(menu.getByRole('menuitem', { name: 'Reset art' })).toHaveCount(0)
    // Fits at 375px.
    const box = await menu.boundingBox()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(375)

    await page.goto('/#/artists')
    await expect(moreButton(page, 'Queen')).toBeVisible()
    await moreButton(page, 'Queen').click()
    await expect(page.locator('.track-menu-panel').getByRole('menuitem', { name: 'Download' })).toBeVisible()

    await page.goto('/#/playlists')
    const workout = moreButton(page, 'Workout')
    await expect(workout).toBeVisible()
    await workout.click()
    const playlistMenu = page.locator('.track-menu-panel')
    for (const name of ['Add next in queue', 'Add last in queue', 'Upload artwork…', 'Download']) {
      await expect(playlistMenu.getByRole('menuitem', { name, exact: true })).toBeVisible()
    }
    await expect(playlistMenu.getByRole('menuitem', { name: 'Play', exact: true })).toHaveCount(0)
    await expect(playlistMenu.getByRole('menuitem', { name: 'Shuffle', exact: true })).toHaveCount(0)
    await expect(page.locator('.browse-card-playlist').first()).toBeVisible()
  })

  test('shows More actions on video cards and the full-screen player', async ({ page }) => {
    await open(page, '/#/videos/Shows')
    const more = moreButton(page, 'Episode One')
    await expect(more).toBeVisible()
    await more.click()
    const menu = page.locator('.track-menu-panel')
    await expect(menu.getByRole('menuitem', { name: 'Add next in queue' })).toBeVisible()
    await expect(menu.getByRole('menuitem', { name: 'Upload artwork…' })).toBeVisible()
    await expect(menu.getByRole('menuitem', { name: 'Download' })).toBeVisible()
    // Opening the menu did not start playback.
    await expect(player(page)).not.toContainText('Episode One')
    await page.keyboard.press('Escape')

    await page.locator('.video-card').filter({ hasText: 'Episode One' }).click()
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    const overlay = page.locator(':is(.nowplaying-overlay, .nowplaying-page)')
    const nowMore = overlay.getByRole('button', { name: 'More actions for Episode One' })
    await expect(nowMore).toBeVisible()
    const box = await nowMore.boundingBox()
    expect(box!.x + box!.width).toBeLessThanOrEqual(375)
    await nowMore.click()
    await expect(page.locator('.track-menu-panel').getByRole('menuitem', { name: 'Download' })).toBeVisible()
  })

  test('video menu actions do not also start the video', async ({ page }) => {
    const mock = await open(page, '/#/videos/Shows')
    await moreButton(page, 'Episode One').click()
    const menu = page.locator('.track-menu-panel')
    await menu.getByRole('menuitem', { name: 'Add next in queue' }).click()
    await expect.poll(() => mock.wsMessages.some((m) => m.type === 'add_to_queue' && m.mode === 'next')).toBe(true)
    expect(mock.wsMessages.some((m) => m.type === 'set_queue_context')).toBe(false)
    // (No player-bar check here: queueing onto an empty queue legitimately starts it.)
  })

  test('video menu opens from the keyboard without starting playback', async ({ page }) => {
    const mock = await open(page, '/#/videos/Shows')
    const more = moreButton(page, 'Episode One')
    await more.focus()
    await page.keyboard.press('Enter')
    const menu = page.locator('.track-menu-panel')
    await expect(menu.getByRole('menuitem', { name: 'Add next in queue' })).toBeVisible()
    // The panel sits at the button, not displaced by its DOM position.
    const mb = (await more.boundingBox())!
    const pb = (await menu.boundingBox())!
    expect(Math.abs(pb.x - mb.x)).toBeLessThan(400)
    expect(pb.y).toBeGreaterThanOrEqual(0)
    await menu.getByRole('menuitem', { name: 'Add last in queue' }).focus()
    await page.keyboard.press('Enter')
    await expect.poll(() => mock.wsMessages.some((m) => m.type === 'add_to_queue' && m.mode === 'end')).toBe(true)
    expect(mock.wsMessages.some((m) => m.type === 'set_queue_context')).toBe(false)
    // (No player-bar check here: queueing onto an empty queue legitimately starts it.)
  })

  test('shows More actions in Now Playing for a song', async ({ page }) => {
    await open(page, '/#/songs')
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    const overlay = page.locator(':is(.nowplaying-overlay, .nowplaying-page)')
    await overlay.getByRole('button', { name: 'More actions for Bohemian Rhapsody' }).click()
    const menu = page.locator('.track-menu-panel')
    await expect(menu.getByRole('menuitem', { name: 'Upload artwork…' })).toBeVisible()
    await expect(menu.getByRole('menuitem', { name: 'Download' })).toBeVisible()
  })

  test('uploads custom art with PUT, versions art URLs, and resets', async ({ page }) => {
    const mock = await open(page, '/#/albums')
    await moreButton(page, 'A Night at the Opera').click()
    await page.locator('.track-menu-panel input[type=file]').setInputFiles({
      name: 'cover.png',
      mimeType: 'image/png',
      buffer: PNG,
    })
    await expect(page.locator('.toast-success')).toContainText('Artwork updated')
    const puts = artCalls(mock, 'PUT')
    expect(puts).toHaveLength(1)
    expect(puts[0].path).toMatch(/^\/api\/art\/album\/album-\d+$/)
    // The tile now loads the custom thumbnail with a cache-busting version.
    await expect
      .poll(() => mock.calls.some((c) => /^\/api\/art\/album\/album-\d+\/thumb$/.test(c.path) && /[?&]v=\d+/.test(c.url)))
      .toBe(true)

    // Reset is offered now, and not for an album that was never customised.
    await moreButton(page, 'A Night at the Opera').click()
    await expect(page.locator('.track-menu-panel').getByRole('menuitem', { name: 'Reset art' })).toBeVisible()
    await page.locator('.track-menu-panel').getByRole('menuitem', { name: 'Reset art' }).click()
    await expect(page.locator('.toast-success').last()).toContainText('Artwork reset')
    expect(artCalls(mock, 'DELETE')).toHaveLength(1)
    await moreButton(page, 'A Night at the Opera').click()
    await expect(page.locator('.track-menu-panel').getByRole('menuitem', { name: 'Reset art' })).toHaveCount(0)
  })

  test('offers Reset art when the server reports custom art, and versions the URL', async ({ page }) => {
    const mock = await open(page, '/#/artists', true, () => {
      for (let i = 1; i <= 10; i++) mockCustomArt.set(`artist:artist-${i}`, 4242)
    })
    await expect(moreButton(page, 'Queen')).toBeVisible()
    await moreButton(page, 'Queen').click()
    await expect(page.locator('.track-menu-panel').getByRole('menuitem', { name: 'Reset art' })).toBeVisible()
    expect(mock.calls.some((c) => /^\/api\/art\/artist\/.+\/thumb$/.test(c.path) && c.url.includes('v=4242'))).toBe(true)
  })

  test('reports unsupported images from the server', async ({ page }) => {
    await open(page, '/#/playlists')
    await moreButton(page, 'Workout').click()
    await page.locator('.track-menu-panel input[type=file]').setInputFiles({
      name: 'notes.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from('hello'),
    })
    await expect(page.locator('.toast-error')).toContainText('not a supported image')
  })

  test('downloads a single song through a same-origin link', async ({ page }) => {
    await open(page, '/#/songs')
    await page.getByRole('button', { name: /^More actions for / }).first().click()
    const downloadPromise = page.waitForEvent('download')
    await page.locator('.track-menu-panel').getByRole('menuitem', { name: 'Download' }).click()
    const download = await downloadPromise
    expect(new URL(download.url()).pathname).toMatch(/^\/api\/download\/media\/\d+$/)
    await expect(page.locator('.toast-success')).toContainText('Download started')
  })

  test('falls back to the ZIP for albums without the File System Access API', async ({ page }) => {
    const mock = await open(page, '/#/albums')
    await moreButton(page, 'A Night at the Opera').click()
    const downloadPromise = page.waitForEvent('download')
    await page.locator('.track-menu-panel').getByRole('menuitem', { name: 'Download' }).click()
    const download = await downloadPromise
    expect(new URL(download.url()).pathname).toMatch(/^\/api\/download\/album\/album-\d+$/)
    await expect(page.locator('.toast-success')).toContainText('Download started')
    expect(mock.calls.some((c) => c.path.endsWith('/manifest'))).toBe(false)
  })

  test('writes album files into a picked folder with progress', async ({ page }) => {
    await page.addInitScript(() => {
      const written: Record<string, number> = {}
      ;(window as unknown as { __written: Record<string, number> }).__written = written
      function dir(prefix: string): unknown {
        return {
          getDirectoryHandle: async (name: string) => dir(`${prefix}${name}/`),
          getFileHandle: async (name: string, opts?: { create?: boolean }) => {
            const path = `${prefix}${name}`
            if (!opts?.create && !(path in written)) throw new DOMException('nope', 'NotFoundError')
            return {
              getFile: async () => ({ size: written[path] }),
              createWritable: async () => {
                const sink = new WritableStream<Uint8Array>({
                  write(chunk) {
                    written[path] = (written[path] ?? 0) + chunk.byteLength
                  },
                })
                return sink
              },
            }
          },
        }
      }
      ;(window as unknown as { showDirectoryPicker: () => Promise<unknown> }).showDirectoryPicker = async () => dir('')
    })
    await open(page, '/#/albums', false)
    await moreButton(page, 'A Night at the Opera').click()
    await page.locator('.track-menu-panel').getByRole('menuitem', { name: 'Download' }).click()
    await expect(page.locator('.toast-success').last()).toContainText('Downloaded 2 files')
    const written = await page.evaluate(
      () => (window as unknown as { __written: Record<string, number> }).__written,
    )
    expect(Object.keys(written).sort()).toEqual([
      'Atriolyric/Queen/A Night at the Opera/01 Bohemian Rhapsody.flac',
      'Atriolyric/Queen/A Night at the Opera/02 Love of My Life.flac',
    ])
  })
})

test.describe('queueing a whole collection (review L44)', () => {
  const queueMessages = (mock: AppMockState) =>
    mock.wsMessages.filter((m) => m.type === 'add_to_queue' || m.type === 'add_many_to_queue')

  test('Add next in queue on an artist sends one message with every track in order', async ({ page }) => {
    const mock = await open(page, '/#/artists')
    const albumIds = mock.tracks
      .filter((track) => track.artist === 'Queen' && track.kind === 'track')
      .map((track) => track.id)
    expect(albumIds.length).toBeGreaterThan(1)
    await moreButton(page, 'Queen').click()
    await page.getByRole('menuitem', { name: 'Add next in queue' }).click()
    await expect(page.locator('.toast-success')).toContainText('Queued next')
    await expect.poll(() => queueMessages(mock).length).toBe(1)
    const [message] = queueMessages(mock)
    expect(message).toEqual({ type: 'add_many_to_queue', media_item_ids: albumIds, mode: 'next' })
    expect(mock.protocolViolations).toEqual([])
    // The mock server applied it as one change: the tracks are the queue.
    expect(mock.playback.queue_media_item_ids).toEqual(albumIds)
  })

  test('Add last in queue on a playlist sends one message', async ({ page }) => {
    const mock = await open(page, '/#/playlists')
    const playlistIds = mock.playlistDetails[2].items.map((item) => item.id)
    await moreButton(page, 'Workout').click()
    await page.getByRole('menuitem', { name: 'Add last in queue' }).click()
    await expect(page.locator('.toast-success')).toContainText('Added')
    await expect.poll(() => queueMessages(mock).length).toBe(1)
    expect(queueMessages(mock)[0]).toEqual({ type: 'add_many_to_queue', media_item_ids: playlistIds, mode: 'end' })
    expect(mock.protocolViolations).toEqual([])
  })
})
