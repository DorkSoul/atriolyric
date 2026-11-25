import { type Page } from 'playwright/test'
import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'
import { devicePickerToggle, player } from './app-helpers'

// The desktop side panel (1200px and wider, in place of the player bar), and
// the Now Playing layouts it sits beside.

const sidePanel = (page: Page) => page.locator('aside.side-panel')

async function playSong(page: Page) {
  await page.goto('/#/songs')
  await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
  await expect(player(page)).toContainText('Bohemian Rhapsody')
}

test.describe('the side panel', () => {
  test('shows at 1440px wide and gives way to the player bar at 1100px', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await installApiMock(page)
    await playSong(page)
    const panel = sidePanel(page)
    await expect(panel).toBeVisible()
    await expect(page.locator('.player-bar')).toHaveCount(0)
    await expect(panel.locator('.side-panel-player')).toContainText('Bohemian Rhapsody')
    await expect(panel.locator('.side-panel-player')).toContainText('A Night at the Opera')
    // To the right of the page column, full height.
    const main = (await page.locator('main.app-main').boundingBox())!
    const box = (await panel.boundingBox())!
    expect(box.x).toBeGreaterThanOrEqual(main.x + main.width - 1)
    expect(Math.round(box.y + box.height)).toBe(900)

    await page.setViewportSize({ width: 1100, height: 900 })
    await expect(panel).toHaveCount(0)
    await expect(page.locator('.player-bar')).toBeVisible()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
  })

  test('the artwork opens Now Playing, and the controls answer like the bar', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    const mock = await installApiMock(page)
    await playSong(page)
    const panel = player(page)
    await expect(panel.getByRole('button', { name: 'Pause' })).toBeVisible()
    mock.wsMessages.length = 0
    await panel.getByRole('button', { name: 'Next' }).click()
    await expect.poll(() => mock.wsMessages.some((m) => m.type === 'next')).toBe(true)
    await expect(panel.getByRole('slider', { name: 'Seek' })).toBeVisible()
    await expect(panel.getByRole('slider', { name: 'Volume' })).toHaveAttribute('aria-valuetext', /^\d+%$/)

    // The more button opens the song menu Now Playing uses.
    await panel.getByRole('button', { name: /^More actions for/ }).click()
    await expect(page.getByRole('menu', { name: /^Actions for / })).toBeVisible()
    await page.keyboard.press('Escape')

    await panel.getByRole('button', { name: 'Open now playing' }).click()
    await expect(page.locator(':is(.nowplaying-overlay, .nowplaying-page)')).toBeVisible()
  })

  test('play sits in the middle of the panel, volume runs upright right of the artwork and more sits under the heart', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await installApiMock(page)
    await playSong(page)
    const panel = sidePanel(page)
    const box = (await panel.locator('.side-panel-player').boundingBox())!
    const play = (await panel.getByRole('button', { name: 'Pause' }).boundingBox())!
    expect(Math.abs(play.x + play.width / 2 - (box.x + box.width / 2))).toBeLessThan(3)
    const art = (await panel.getByRole('button', { name: 'Open now playing' }).boundingBox())!
    const volume = (await panel.getByRole('slider', { name: 'Volume' }).boundingBox())!
    // Upright, to the right of the (square) artwork, within its height.
    expect(volume.height).toBeGreaterThan(volume.width * 3)
    expect(Math.abs(art.width - art.height)).toBeLessThan(2)
    expect(volume.x).toBeGreaterThanOrEqual(art.x + art.width - 1)
    expect(volume.y).toBeGreaterThanOrEqual(art.y - 1)
    expect(volume.y + volume.height).toBeLessThanOrEqual(art.y + art.height + 1)
    const column = (await panel.locator('.side-panel-volume-col').boundingBox())!
    expect(column.height).toBeGreaterThan(art.height * 0.9)
    expect(play.y).toBeGreaterThan(art.y + art.height - 1)
    const heart = (await panel.locator('.side-panel-actions .player-pref-btn').boundingBox())!
    const more = (await panel.getByRole('button', { name: /^More actions for/ }).boundingBox())!
    expect(more.y).toBeGreaterThan(heart.y + heart.height - 1)
    expect(Math.abs(more.x + more.width / 2 - (heart.x + heart.width / 2))).toBeLessThan(3)
  })

  test('shows the queue, reorders it from the keyboard and toggles shuffle and repeat', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    const mock = await installApiMock(page)
    await playSong(page)
    const panel = sidePanel(page)
    await expect(panel.getByRole('heading', { name: 'Up next' })).toBeVisible()
    const rows = panel.locator('.queue-row')
    await expect(rows).toHaveCount(3)
    await expect(rows.first()).toHaveClass(/queue-row-current/)
    const titles = () => rows.locator('.track-title').allTextContents()
    const before = await titles()
    const second = before[1]
    await rows.nth(1).getByRole('button', { name: `Reorder ${second}` }).focus()
    await page.keyboard.press('ArrowDown')
    await expect.poll(() => mock.wsMessages.find((m) => m.type === 'move_queue_item')?.direction).toBe(1)
    await expect.poll(titles).toEqual([before[0], before[2], before[1]])
    await expect(rows.filter({ hasText: second }).getByRole('button', { name: `Reorder ${second}` })).toBeFocused()

    await panel.getByRole('button', { name: 'Shuffle off' }).click()
    await expect(panel.getByRole('button', { name: 'Shuffle on' })).toHaveAttribute('aria-pressed', 'true')
    await panel.getByRole('button', { name: 'Repeat off' }).click()
    await expect(panel.getByRole('button', { name: 'Repeat all' })).toBeVisible()
  })

  test('a long queue title uses the width it has and is cut only when it really does not fit', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    const mock = await installApiMock(page)
    const titles = ['Short one', 'A fairly long song title here', 'A title that is far too long to fit on two lines of the side panel or of the Now Playing queue, however wide either one is, because it just goes on and on and on']
    mock.playlistDetails[2].items = titles.map((title, i) => ({ ...mock.tracks[0], id: 100_000 + i, title, playlist_item_id: 200_000 + i }))
    await page.goto('/#/playlists/2')
    await page.locator('.hero-play-btn[aria-label="Play"]').click()
    const panel = sidePanel(page)
    await expect(panel.locator('.queue-row')).toHaveCount(3)
    const cut = (scope: Page | ReturnType<typeof sidePanel>) =>
      scope.locator('.queue-row .queue-row-jump .track-title').evaluateAll((els) =>
        els.map((el) => el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1),
      )
    // A title that fits in two lines is whole; only the third is cut.
    expect(await cut(panel)).toEqual([false, false, true])
    // The title reaches the remove button (no spare room left unused beside it).
    const row = panel.locator('.queue-row').nth(1)
    const info = (await row.locator('.queue-row-info').boundingBox())!
    const remove = (await row.locator('.queue-row-actions button').boundingBox())!
    expect(remove.x - (info.x + info.width)).toBeLessThanOrEqual(12)
    // The same rows in Now Playing's Up next column (narrower than the side
    // panel's 1200px, the page carries it), which is wider: same rule.
    await page.setViewportSize({ width: 1100, height: 900 })
    await page.locator('.player-bar').getByRole('button', { name: 'Open now playing' }).click()
    const overlay = page.locator(':is(.nowplaying-overlay, .nowplaying-page)')
    await expect(overlay.locator('.queue-row')).toHaveCount(3)
    expect(await cut(overlay)).toEqual([false, false, true])
  })

  test('keeps its scroll position across tab changes', async ({ page }) => {
    // Short, so the panel (artwork, controls, queue) has to scroll.
    await page.setViewportSize({ width: 1440, height: 560 })
    await installApiMock(page)
    await playSong(page)
    const panel = sidePanel(page)
    await expect(panel.locator('.queue-row')).toHaveCount(3)
    const scrollable = await panel.evaluate((el) => el.scrollHeight - el.clientHeight)
    expect(scrollable).toBeGreaterThan(100)
    await panel.evaluate((el) => {
      el.scrollTop = 140
    })
    await page.getByRole('button', { name: 'Albums', exact: true }).first().click()
    await expect(page).toHaveURL(/#\/albums/)
    await page.getByRole('button', { name: 'Home', exact: true }).first().click()
    await expect(page.locator('.home-view')).toBeVisible()
    expect(await panel.evaluate((el) => el.scrollTop)).toBe(140)
    // The Up next header stays at the top while the queue scrolls under it.
    const header = (await panel.locator('.side-panel-upnext').boundingBox())!
    const panelBox = (await panel.boundingBox())!
    await panel.evaluate((el) => {
      el.scrollTop = el.scrollHeight
    })
    const stuck = (await panel.locator('.side-panel-upnext').boundingBox())!
    expect(stuck.y).toBeGreaterThanOrEqual(panelBox.y - 1)
    expect(stuck.y).toBeLessThanOrEqual(header.y)
  })

  test('has the radio and nothing-playing states of the bar', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await installApiMock(page)
    await page.goto('/#/home')
    await expect(player(page)).toContainText('Nothing playing — pick a track to get started.')
    await page.locator('.home-view .rail-card').filter({ hasText: 'Radio Paradise' }).click()
    const panel = player(page)
    await expect(panel).toContainText('Radio Paradise')
    await expect(panel).toContainText('Live radio')
    await expect(panel.getByRole('button', { name: /^(Play|Pause)$/ })).toBeVisible()
    await panel.getByRole('button', { name: 'Stop', exact: true }).click()
    await expect(panel).toContainText('Nothing playing')
  })
})

test.describe('the playback device picker', () => {
  for (const [width, place] of [
    [1440, '.top-bar-actions'],
    [1100, '.player-bar'],
    [390, '.top-bar-actions'],
  ] as const) {
    test(`is reachable at ${width}px (in ${place})`, async ({ page }) => {
      await page.setViewportSize({ width, height: 844 })
      await installApiMock(page)
      await playSong(page)
      const toggle = devicePickerToggle(page)
      await expect(toggle).toHaveCount(1)
      await expect(page.locator(place).getByRole('button', { name: /^Choose playback device/ })).toBeVisible()
      await toggle.click()
      const menu = page.getByRole('menu', { name: 'Playback devices' })
      await expect(menu.getByRole('menuitemradio').first()).toBeVisible()
      // The menu stays on screen.
      const box = (await menu.boundingBox())!
      expect(box.x).toBeGreaterThanOrEqual(0)
      expect(box.y).toBeGreaterThanOrEqual(0)
      expect(box.x + box.width).toBeLessThanOrEqual(width)
      expect(box.y + box.height).toBeLessThanOrEqual(844)
    })
  }
})

test.describe('one set of player controls', () => {
  for (const width of [1440, 1100, 390]) {
    test(`exactly one play/pause button at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 844 })
      await installApiMock(page)
      await playSong(page)
      await expect(page.getByRole('button', { name: /^(Play|Pause)$/ })).toHaveCount(1)
      await expect(page.getByRole('button', { name: 'Open now playing' })).toHaveCount(1)
      await expect(page.getByRole('region', { name: 'Player' })).toHaveCount(1)
    })
  }
})

test.describe('Now Playing', () => {
  test('Now Playing opened from Home does not carry Home\'s Customise button', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await installApiMock(page)
    await page.goto('/#/songs')
    await page.getByRole('button', { name: /^Play Bohemian Rhapsody/ }).first().click()
    await page.goto('/#/home')
    await expect(page.getByRole('button', { name: 'Customise Home' })).toBeVisible()
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    await expect(page.locator('.nowplaying-page')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Customise Home' })).toHaveCount(0)
    await page.goBack()
    await expect(page.getByRole('button', { name: 'Customise Home' })).toBeVisible()
  })

  test('the Now Playing page fits the window, and lyrics take the bottom half', async ({ page }) => {
    await page.setViewportSize({ width: 1920, height: 940 })
    await installApiMock(page)
    await playSong(page)
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    const view = page.locator('.nowplaying-page')
    const main = view.locator('.nowplaying-main')
    const scrolls = () => main.evaluate((el) => el.scrollHeight > el.clientHeight + 1)
    await expect(view.getByRole('button', { name: 'Genres/tags' })).toBeVisible()
    expect(await scrolls()).toBe(false)
    await view.getByRole('button', { name: 'Lyrics', exact: true }).click()
    await expect(view.getByTestId('lyrics-inline')).toContainText('Is this the real life?')
    expect(await scrolls()).toBe(false)
    const pane = (await view.locator('.nowplaying-lyrics-pane').boundingBox())!
    const box = (await main.boundingBox())!
    // Half of the column below its top padding, give or take a few pixels.
    expect(Math.abs(pane.y + pane.height - (box.y + box.height))).toBeLessThan(2)
    expect(pane.height).toBeGreaterThan(box.height * 0.45)
  })

  test('Now Playing has no device button: the header picker covers it', async ({ page }) => {
    for (const [width, height] of [[390, 844], [1440, 900]] as const) {
      await page.setViewportSize({ width, height })
      await installApiMock(page)
      await page.goto('/#/songs')
      await page.getByRole('button', { name: /^Play Bohemian Rhapsody/ }).first().click()
      await player(page).getByRole('button', { name: /now playing/i }).first().click()
      const view = page.locator(':is(.nowplaying-overlay, .nowplaying-page)')
      await expect(view.getByRole('button', { name: 'Genres/tags' })).toBeVisible()
      await expect(view.getByRole('button', { name: /^Playing on/ })).toHaveCount(0)
    }
  })

  test('at 1440px is a page between the sidebar and a side panel that keeps only Up next', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await installApiMock(page)
    await playSong(page)
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    const view = page.locator('.nowplaying-page')
    await expect(view).toBeVisible()
    await expect(page.locator('.nowplaying-overlay')).toHaveCount(0)
    // The sidebar stays, left of the page.
    const sidebar = page.getByRole('navigation', { name: 'Tabs' })
    await expect(sidebar).toBeVisible()
    const nav = (await sidebar.boundingBox())!
    const box = (await view.boundingBox())!
    expect(box.x).toBeGreaterThanOrEqual(nav.x + nav.width - 1)
    // The side panel has Up next and no player; the page has no second Up next.
    const panel = sidePanel(page)
    await expect(panel.locator('.side-panel-player')).toHaveCount(0)
    await expect(panel.getByRole('region', { name: 'Up next' }).locator('.queue-row')).toHaveCount(3)
    await expect(view.locator('.nowplaying-side')).toHaveCount(0)
    await expect(view.locator('.nowplaying-album')).toHaveText('A Night at the Opera')
    // The album line opens the album.
    await view.locator('.nowplaying-album').getByRole('link').click()
    await expect(view).toHaveCount(0)
    await expect(page).toHaveURL(/#\/albums\//)
    await expect(panel.locator('.side-panel-player')).toContainText('Bohemian Rhapsody')
  })

  test('at 1000px the page carries Up next itself, beside the controls', async ({ page }) => {
    await page.setViewportSize({ width: 1000, height: 800 })
    await installApiMock(page)
    await playSong(page)
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    const view = page.locator('.nowplaying-page')
    const main = (await view.locator('.nowplaying-main').boundingBox())!
    const side = (await view.locator('.nowplaying-side').boundingBox())!
    expect(side.x).toBeGreaterThanOrEqual(main.x + main.width - 1)
    await expect(view.locator('.nowplaying-side .queue-row')).toHaveCount(3)
  })

  for (const [width, height] of [[1440, 900], [1280, 800]] as const) {
    test(`with lyrics the title and controls are centred beside the art at ${width}x${height}`, async ({ page }) => {
      await page.setViewportSize({ width, height })
      await installApiMock(page)
      await playSong(page)
      await player(page).getByRole('button', { name: 'Open now playing' }).click()
      const view = page.locator('.nowplaying-page')
      await view.getByRole('button', { name: 'Lyrics', exact: true }).click()
      await expect(view.getByTestId('lyrics-inline')).toContainText('Is this the real life?')
      const art = (await view.locator('.art-nowplaying').boundingBox())!
      const head = (await view.locator('.nowplaying-head').boundingBox())!
      const ctl = (await view.locator('.nowplaying-ctl').boundingBox())!
      expect(head.x).toBeGreaterThanOrEqual(art.x + art.width - 1)
      const blockCentre = (head.y + ctl.y + ctl.height) / 2
      expect(Math.abs(blockCentre - (art.y + art.height / 2))).toBeLessThanOrEqual(4)
    })
  }

  test('with lyrics below 1200px wide the artwork sits above the title and controls', async ({ page }) => {
    await page.setViewportSize({ width: 1000, height: 800 })
    await installApiMock(page)
    await playSong(page)
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    const view = page.locator('.nowplaying-page')
    await view.getByRole('button', { name: 'Lyrics', exact: true }).click()
    await expect(view.getByTestId('lyrics-inline')).toContainText('Is this the real life?')
    const art = (await view.locator('.art-nowplaying').boundingBox())!
    const head = (await view.locator('.nowplaying-head').boundingBox())!
    expect(head.y).toBeGreaterThanOrEqual(art.y + art.height - 1)
  })

  for (const [width, height] of [[1440, 900], [1024, 768], [800, 700], [1280, 600]] as const) {
    test(`lyrics take the bottom half without covering the controls at ${width}x${height}`, async ({ page }) => {
      await page.setViewportSize({ width, height })
      await installApiMock(page)
      await playSong(page)
      await player(page).getByRole('button', { name: 'Open now playing' }).click()
      const overlay = page.locator(':is(.nowplaying-overlay, .nowplaying-page)')
      await overlay.getByRole('button', { name: 'Lyrics', exact: true }).click()
      const lyrics = overlay.locator('.nowplaying-lyrics-pane')
      await expect(lyrics.getByTestId('lyrics-inline')).toContainText('Is this the real life?')
      const pane = (await lyrics.boundingBox())!
      const main = (await overlay.locator('.nowplaying-main').boundingBox())!
      // In the lower half of the column, at most half of it.
      expect(pane.y).toBeGreaterThanOrEqual(main.y + main.height / 2 - 60)
      expect(pane.height).toBeLessThanOrEqual(main.height / 2 + 1)
      for (const selector of ['.nowplaying-head', '.nowplaying-ctl', '.art-nowplaying']) {
        const box = (await overlay.locator(selector).boundingBox())!
        expect(box.y + box.height, selector).toBeLessThanOrEqual(pane.y + 0.5)
      }
      const controls = overlay.locator('.nowplaying-controls-main button')
      for (const button of await controls.all()) {
        const box = (await button.boundingBox())!
        expect(box.x).toBeGreaterThanOrEqual(main.x)
        expect(box.x + box.width).toBeLessThanOrEqual(main.x + main.width + 0.5)
      }
      // The plays line gives way.
      await expect(overlay.locator('.nowplaying-stats')).toBeHidden()
      // The volume stays upright right of the artwork, within its height,
      // left of the title and controls.
      const art = (await overlay.locator('.art-nowplaying').boundingBox())!
      const volume = (await overlay.getByRole('slider', { name: 'Volume' }).boundingBox())!
      const head = (await overlay.locator('.nowplaying-head').boundingBox())!
      expect(volume.height).toBeGreaterThan(volume.width)
      expect(volume.height).toBeGreaterThan(art.height * 0.6)
      expect(volume.x).toBeGreaterThanOrEqual(art.x + art.width - 1)
      // Side by side from 1200px; stacked (volume above the title) below.
      if (width >= 1200) expect(volume.x + volume.width).toBeLessThanOrEqual(head.x + 1)
      else expect(volume.y + volume.height).toBeLessThanOrEqual(head.y + 1)
      expect(volume.y).toBeGreaterThanOrEqual(art.y - 1)
      expect(volume.y + volume.height).toBeLessThanOrEqual(art.y + art.height + 1)
      await expect(overlay.locator('.nowplaying-volume-side')).toHaveCount(0)
    })
  }

  test('Now playing is a tab between Playlists and Radio that opens the page', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await installApiMock(page)
    await playSong(page)
    const nav = page.locator('.sidebar nav.tabs')
    const labels = (await nav.getByRole('button').allTextContents()).map((t) => t.trim())
    const at = labels.findIndex((l) => l.startsWith('Now playing'))
    expect(labels[at - 1]).toMatch(/^Playlists/)
    expect(labels[at + 1]).toMatch(/^Radio/)
    const tab = nav.getByRole('button', { name: /^Now playing/ })
    await tab.click()
    await expect(page.locator('.nowplaying-page')).toBeVisible()
    await expect(tab).toHaveAttribute('aria-current', 'page')
    await expect(nav.getByRole('button', { name: /^Songs/ })).not.toHaveAttribute('aria-current', 'page')
    expect(page.url()).toContain('#/now-playing')
    await page.goBack()
    await expect(page.locator('.nowplaying-page')).toHaveCount(0)
    await expect(nav.getByRole('button', { name: /^Songs/ })).toHaveAttribute('aria-current', 'page')
    expect(page.url()).toContain('#/songs')
    // Forward opens it again; a refresh there lands on it.
    await page.goForward()
    await expect(page.locator('.nowplaying-page')).toBeVisible()
    await page.reload()
    await expect(page.locator('.nowplaying-page')).toBeVisible()
    await page.goBack()
    await expect(page.locator('.nowplaying-page')).toHaveCount(0)
  })

  test('the Now playing tab with nothing on says so', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await installApiMock(page)
    await page.goto('/#/songs')
    await page.locator('.sidebar nav.tabs').getByRole('button', { name: /^Now playing/ }).click()
    await expect(page.locator('.nowplaying-page')).toContainText('Nothing playing')
  })

  test('on a phone the art and controls keep their size when the lyrics tab is chosen', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await installApiMock(page)
    await playSong(page)
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    const overlay = page.locator(':is(.nowplaying-overlay, .nowplaying-page)')
    const art = () => overlay.locator('.art-nowplaying').boundingBox()
    const seek = () => overlay.getByRole('slider', { name: 'Seek' }).boundingBox()
    const before = [await art(), await seek()]
    await overlay.getByRole('tab', { name: 'Lyrics' }).click()
    await expect(overlay.getByTestId('lyrics-inline')).toBeVisible()
    expect([await art(), await seek()]).toEqual(before)
    await overlay.getByRole('tab', { name: 'Up next' }).click()
    await expect(overlay.locator('.queue-row')).toHaveCount(3)
    expect([await art(), await seek()]).toEqual(before)
  })

  for (const width of [1440, 1024, 800, 390, 320]) {
    test(`has no horizontal overflow at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 800 })
      await installApiMock(page)
      await playSong(page)
      const overflow = () =>
        page.evaluate(() => {
          const offenders: string[] = []
          for (const el of document.querySelectorAll<HTMLElement>(':is(.nowplaying-overlay, .nowplaying-page) *')) {
            const r = el.getBoundingClientRect()
            if (r.width > 1 && (r.right > window.innerWidth + 0.5 || r.left < -0.5) && !el.closest('.marquee, .visually-hidden')) {
              offenders.push(`${el.tagName.toLowerCase()}.${el.className}`)
            }
          }
          return { doc: document.documentElement.scrollWidth - document.documentElement.clientWidth, offenders: offenders.slice(0, 5) }
        })
      expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBe(0)
      await player(page).getByRole('button', { name: 'Open now playing' }).click()
      await expect(page.locator(':is(.nowplaying-overlay, .nowplaying-page)')).toBeVisible()
      expect(await overflow()).toEqual({ doc: 0, offenders: [] })
      const lyricsControl = page
        .locator(':is(.nowplaying-overlay, .nowplaying-page)')
        .getByRole('button', { name: 'Lyrics', exact: true })
        .or(page.locator(':is(.nowplaying-overlay, .nowplaying-page)').getByRole('tab', { name: 'Lyrics' }))
      await lyricsControl.click()
      await expect(page.getByTestId('lyrics-inline')).toBeVisible()
      expect(await overflow()).toEqual({ doc: 0, offenders: [] })
    })
  }
})
