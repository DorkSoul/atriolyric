import { expect, test } from './fixtures'
import { type Locator, type Page } from 'playwright/test'
import { installApiMock } from './mockApi'
import { player, openSettings } from './app-helpers'

function intersects(a: { x: number; y: number; width: number; height: number }, b: typeof a) {
  return !(a.x + a.width <= b.x + 0.5 || b.x + b.width <= a.x + 0.5 || a.y + a.height <= b.y + 0.5 || b.y + b.height <= a.y + 0.5)
}

async function boxes(locator: Locator) {
  const out = []
  for (let i = 0; i < (await locator.count()); i++) out.push((await locator.nth(i).boundingBox())!)
  return out
}

async function playTrack(page: Page, title: string) {
  await page.getByRole('button', { name: 'Songs', exact: true }).click()
  await page.getByRole('button', { name: `Play ${title}` }).click()
  await expect(player(page)).toContainText(title)
}

async function openNowPlaying(page: Page) {
  await player(page).getByRole('button', { name: 'Open now playing' }).click()
  const overlay = page.locator(':is(.nowplaying-overlay, .nowplaying-page)')
  await expect(overlay).toBeVisible()
  return overlay
}

test.describe('entity menus', () => {
  test.use({ viewport: { width: 375, height: 800 } })

  test('header more menus drop Play/Shuffle and long names wrap without overlap', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/albums')
    await page.locator('.browse-card-album').filter({ hasText: 'A Night at the Opera' }).click()
    await expect(page.locator('.collection-detail-actions').getByRole('button', { name: 'Play', exact: true })).toBeVisible()
    await page.locator('.collection-detail-actions').getByRole('button', { name: /^More actions for/ }).click()
    const menu = page.locator('.track-menu-panel')
    await expect(menu.getByRole('menuitem', { name: 'Add next in queue' })).toBeVisible()
    await expect(menu.getByRole('menuitem', { name: 'Play', exact: true })).toHaveCount(0)
    await expect(menu.getByRole('menuitem', { name: 'Shuffle', exact: true })).toHaveCount(0)

    // Long text grows the item instead of running over its neighbour.
    await page.evaluate(() => {
      const long = [...document.querySelectorAll('.track-menu-panel button')].find((b) => /Create playlist/.test(b.textContent ?? ''))!
      long.textContent = 'Create playlist from A Very Long Album Name That Goes On And On Forever And Ever Amen · Some Extremely Long Artist Name Here'
    })
    const items = menu.locator('button')
    const rects = await boxes(items)
    expect(rects.length).toBeGreaterThan(3)
    for (let i = 0; i < rects.length; i++) for (let j = i + 1; j < rects.length; j++) expect(intersects(rects[i], rects[j])).toBe(false)
    const panel = (await menu.boundingBox())!
    expect(panel.width).toBeLessThanOrEqual(375 * 0.9 + 1)
    expect(await menu.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true)
    const tall = Math.max(...rects.map((r) => r.height))
    expect(tall).toBeGreaterThan(50)
  })

  test('card menus drop Play and Shuffle', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/artists')
    await page.locator('.browse-card').filter({ hasText: 'Queen' }).click({ button: 'right' })
    const panel = page.locator('.collection-menu-panel')
    await expect(panel.getByRole('menuitem', { name: 'Add next in queue' })).toBeVisible()
    await expect(panel.getByRole('menuitem', { name: 'Play', exact: true })).toHaveCount(0)
    await expect(panel.getByRole('menuitem', { name: 'Shuffle', exact: true })).toHaveCount(0)
  })
})

test.describe('specify mix tiles', () => {
  async function openDialog(page: Page, width: number) {
    await installApiMock(page)
    await page.route('**/api/library/mixes/options', async (route) => {
      await route.fulfill({
        json: [
          { kind: 'genre', name: 'singer-songwriter', track_count: 120 },
          { kind: 'tag', name: 'an-extraordinarily-long-tag-name-without-spaces-at-all', track_count: 40 },
          { kind: 'genre', name: 'Rock', track_count: 12 },
          { kind: 'tag', name: 'Workout', track_count: 3 },
        ],
      })
    })
    await page.setViewportSize({ width, height: 700 })
    await page.goto('/#/home')
    await page.getByRole('button', { name: 'Specify mix' }).click()
    return page.getByRole('dialog', { name: 'Specify mix' })
  }

  test('desktop shows Genre/Tag and track counts', async ({ page }) => {
    const dialog = await openDialog(page, 1280)
    const tile = dialog.locator('.specify-mix-options > button').filter({ hasText: 'Rock' })
    await expect(tile).toContainText('Genre · 12 tracks')
    await expect(dialog.locator('.specify-mix-options > button').filter({ hasText: 'Workout' })).toContainText('Tag · 3 tracks')
  })

  test('phone shows only counts, pads tiles and stretches rows to wrapped names', async ({ page }) => {
    const dialog = await openDialog(page, 375)
    const options = dialog.locator('.specify-mix-options > button')
    await expect(options).toHaveCount(4)
    const rock = options.filter({ hasText: 'Rock' })
    await expect(rock.locator('.mix-detail-count')).toBeVisible()
    await expect(rock.locator('.mix-detail-full')).toBeHidden()

    const grid = dialog.locator('.specify-mix-options')
    expect(await grid.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true)
    // Rows equal height; content (name + count) fits inside each tile with padding.
    const rects = await boxes(options)
    expect(Math.abs(rects[0].y - rects[1].y)).toBeLessThan(1)
    expect(Math.abs(rects[0].height - rects[1].height)).toBeLessThan(1)
    expect(Math.abs(rects[2].height - rects[3].height)).toBeLessThan(1)
    for (let i = 0; i < 4; i++) {
      const tile = options.nth(i)
      const fits = await tile.evaluate((el) => {
        const t = el.getBoundingClientRect()
        const kids = [...el.children].map((c) => c.getBoundingClientRect())
        return kids.every((k) => k.top >= t.top + 8 && k.bottom <= t.bottom - 8 && k.right <= t.right && k.left >= t.left)
      })
      expect(fits).toBe(true)
    }
  })
})

test.describe('specify mix wrapped names', () => {
  for (const [name, width, cols] of [['desktop', 1280, 3], ['phone', 375, 2]] as const) {
    test(`rows grow to the tallest tile and centre short ones (${name})`, async ({ page }) => {
      await installApiMock(page)
      await page.route('**/api/library/mixes/options', async (route) => {
        await route.fulfill({
          json: [
            { kind: 'genre', name: 'industrial jungle pussy punch', track_count: 196 },
            { kind: 'genre', name: 'Rock', track_count: 12 },
            { kind: 'tag', name: 'singer-songwriter extremely long name here', track_count: 30 },
            { kind: 'tag', name: 'Pop', track_count: 30 },
            { kind: 'tag', name: 'Jazz', track_count: 30 },
            { kind: 'tag', name: 'Folk', track_count: 30 },
            { kind: 'tag', name: 'Soul', track_count: 30 },
            { kind: 'tag', name: 'Blues', track_count: 30 },
          ],
        })
      })
      await page.setViewportSize({ width, height: 800 })
      await page.goto('/#/home')
      await page.getByRole('button', { name: 'Specify mix' }).click()
      const options = page.getByRole('dialog', { name: 'Specify mix' }).locator('.specify-mix-options > button')
      await expect(options).toHaveCount(8)
      const data = await options.evaluateAll((els) =>
        els.map((el) => {
          const t = el.getBoundingClientRect()
          const kids = [...el.children].map((c) => c.getBoundingClientRect())
          const top = Math.min(...kids.map((k) => k.top))
          const bottom = Math.max(...kids.map((k) => k.bottom))
          return { y: t.y, h: t.height, above: top - t.top, below: t.bottom - bottom }
        }),
      )
      // Group into rows; every tile in a row is equally tall and content fits and is vertically centred.
      let tallestSeen = 0
      for (let i = 0; i < data.length; i += cols) {
        const row = data.slice(i, i + cols)
        for (const tile of row) {
          expect(Math.abs(tile.h - row[0].h)).toBeLessThan(1)
          expect(tile.above).toBeGreaterThanOrEqual(8)
          expect(tile.below).toBeGreaterThanOrEqual(8)
          expect(Math.abs(tile.above - tile.below)).toBeLessThan(2)
        }
        tallestSeen = Math.max(tallestSeen, row[0].h)
      }
      // A wrapped name made at least one row taller than a one-line tile.
      expect(tallestSeen).toBeGreaterThan(Math.min(...data.map((d) => d.h)) + 15)
    })
  }
})

test.describe('lyrics availability', () => {
  test.use({ viewport: { width: 1280, height: 800 } })

  test('toggle only exists for songs with lyrics and falls back to art', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/home')
    await playTrack(page, 'Bohemian Rhapsody')
    const overlay = await openNowPlaying(page)
    await overlay.getByRole('button', { name: 'Lyrics', exact: true }).click()
    await expect(overlay.getByTestId('lyrics-inline')).toBeVisible()
    await overlay.getByRole('button', { name: 'Next', exact: true }).click()
    await expect(overlay.getByRole('button', { name: 'Lyrics', exact: true })).toHaveCount(0)
    await expect(overlay.getByTestId('lyrics-inline')).toHaveCount(0)
    await expect(overlay.locator('.art-nowplaying')).toBeVisible()
    await overlay.getByRole('button', { name: /^More actions for/ }).click()
    await expect(page.getByRole('menuitem', { name: 'View lyrics' })).toHaveCount(0)
  })
})

test.describe('lyrics display', () => {
  async function setTime(page: Page, seconds: number) {
    await page.evaluate((t) => {
      for (const audio of document.querySelectorAll('audio')) {
        audio.currentTime = t
        audio.dispatchEvent(new Event('timeupdate'))
      }
    }, seconds)
  }

  async function openLyrics(page: Page) {
    await page.goto('/#/home')
    await playTrack(page, 'Bohemian Rhapsody')
    const overlay = await openNowPlaying(page)
    // A desktop's toggle button, a phone's tab under the controls.
    await overlay.getByRole('button', { name: 'Lyrics', exact: true }).or(overlay.getByRole('tab', { name: 'Lyrics' })).click()
    return overlay.getByTestId('lyrics-inline')
  }

  for (const [name, width, height] of [['desktop', 1280, 900], ['phone', 375, 800]] as const) {
    test(`timed lyrics (LRC in the plain field) show clean lines and follow the song (${name})`, async ({ page }) => {
      await page.setViewportSize({ width, height })
      await installApiMock(page)
      const lyrics = await openLyrics(page)
      await expect(lyrics).toContainText('Is this the real life?')
      const text = await lyrics.innerText()
      expect(text).not.toContain('[')
      expect(text).not.toContain('Queen')
      await expect(lyrics.locator('.lyrics-inline-line')).toHaveCount(4)

      await setTime(page, 12)
      const active = lyrics.locator('.lyrics-inline-active')
      await expect(active).toHaveText('Is this just fantasy?')
      await setTime(page, 25)
      await expect(active).toHaveText('Caught in a landslide')
      // A seek backwards moves the highlight back.
      await setTime(page, 2)
      await expect(active).toHaveText('Is this the real life?')
      await setTime(page, 25)
      await expect(active).toHaveText('Caught in a landslide')
      // The current line is kept near the vertical centre of the lyrics box.
      await expect
        .poll(async () => {
          const box = (await lyrics.boundingBox())!
          const line = (await active.boundingBox())!
          return Math.abs(line.y + line.height / 2 - (box.y + box.height / 2))
        })
        .toBeLessThan(40)
    })
  }

  test('plain lyrics are shown as text without a highlighted line', async ({ page }) => {
    await installApiMock(page)
    await page.route('**/api/library/media/1/lyrics', (route) =>
      route.fulfill({ json: { plain: 'First line\nSecond line', synced: null, source: 'embedded' } }),
    )
    const lyrics = await openLyrics(page)
    await expect(lyrics).toContainText('Second line')
    await expect(lyrics.locator('.lyrics-inline-active')).toHaveCount(0)
    expect(await lyrics.innerText()).not.toContain('[')
  })
})

test.describe('Now Playing layout', () => {
  test('desktop: two columns, the controls under the art and Up next beside them', async ({ page }) => {
    // Below the side panel's 1200px, so the page carries Up next itself.
    await page.setViewportSize({ width: 1100, height: 800 })
    await installApiMock(page)
    await page.goto('/#/home')
    await playTrack(page, 'Bohemian Rhapsody')
    const overlay = await openNowPlaying(page)
    const back = (await overlay.getByRole('button', { name: 'Back', exact: true }).boundingBox())!
    const art = (await overlay.locator('.art-nowplaying').boundingBox())!
    const more = (await overlay.locator('.nowplaying-more').boundingBox())!
    const title = (await overlay.locator('.nowplaying-title').boundingBox())!
    const play = (await overlay.getByRole('button', { name: /^(Play|Pause)$/ }).boundingBox())!
    const side = (await overlay.locator('.nowplaying-side').boundingBox())!
    expect(intersects(back, art)).toBe(false)
    // The title row (with the song menu) sits under the art, the controls under that.
    expect(title.y).toBeGreaterThanOrEqual(art.y + art.height - 0.5)
    expect(more.y).toBeGreaterThanOrEqual(art.y + art.height - 0.5)
    expect(play.y).toBeGreaterThan(title.y + title.height)
    // Up next is a column of its own to the right.
    expect(side.x).toBeGreaterThanOrEqual(art.x + art.width)
    await expect(overlay.locator('.nowplaying-side').getByRole('heading', { name: 'Up next' })).toBeVisible()
    await expect(overlay.locator('.nowplaying-side .queue-row')).toHaveCount(3)
    await expect(overlay.getByRole('slider', { name: 'Volume' })).toBeVisible()
  })

  test('phone: one column, Up next and Lyrics as tabs, the art never resized', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 800 })
    await installApiMock(page)
    await page.goto('/#/home')
    await playTrack(page, 'Bohemian Rhapsody')
    const overlay = await openNowPlaying(page)
    const artBefore = (await overlay.locator('.art-nowplaying').boundingBox())!
    const playBefore = (await overlay.getByRole('button', { name: /^(Play|Pause)$/ }).boundingBox())!
    const tabs = overlay.getByRole('tablist', { name: 'Up next and lyrics' })
    await expect(tabs.getByRole('tab', { name: 'Up next' })).toHaveAttribute('aria-selected', 'true')
    await expect(overlay.locator('.queue-row')).toHaveCount(3)
    await tabs.getByRole('tab', { name: 'Lyrics' }).click()
    await expect(tabs.getByRole('tab', { name: 'Lyrics' })).toHaveAttribute('aria-selected', 'true')
    await expect(overlay.getByTestId('lyrics-inline')).toBeVisible()
    await expect(overlay.locator('.queue-row')).toHaveCount(0)
    const artAfter = (await overlay.locator('.art-nowplaying').boundingBox())!
    const playAfter = (await overlay.getByRole('button', { name: /^(Play|Pause)$/ }).boundingBox())!
    expect(artAfter).toEqual(artBefore)
    expect(playAfter).toEqual(playBefore)
    // The volume is still there on a phone, in the row under the controls.
    await expect(overlay.getByRole('slider', { name: 'Volume' })).toBeVisible()
  })

  test('the player bar heart sits with the title, before the controls and the volume', async ({ page }) => {
    await page.setViewportSize({ width: 1100, height: 800 })
    await installApiMock(page)
    await page.goto('/#/home')
    await playTrack(page, 'Bohemian Rhapsody')
    const bar = page.locator('.player-bar')
    const heart = (await bar.locator('.player-track-info .player-pref-btn').boundingBox())!
    const prev = (await bar.getByRole('button', { name: 'Previous' }).boundingBox())!
    const barVolume = (await bar.getByRole('slider', { name: 'Volume' }).boundingBox())!
    expect(heart.x + heart.width).toBeLessThanOrEqual(prev.x + 1)
    expect(prev.x).toBeLessThan(barVolume.x)
    expect(Math.abs(heart.y + heart.height / 2 - (barVolume.y + barVolume.height / 2))).toBeLessThan(20)
  })
})

test.describe('equaliser', () => {
  for (const width of [375, 320]) test(`vertical sliders side by side, labels in one row without overlap at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 })
    await installApiMock(page)
    await page.goto('/#/home')
    await openSettings(page, 'Playback')
    const sliders = page.locator('#settings-playback .eq-band input[type="range"]')
    await expect(sliders).toHaveCount(10)
    await sliders.first().scrollIntoViewIfNeeded()
    const rects = await boxes(sliders)
    for (const r of rects) expect(r.height).toBeGreaterThan(r.width * 3)
    for (let i = 1; i < rects.length; i++) expect(rects[i].x).toBeGreaterThan(rects[i - 1].x)
    const labels = await boxes(page.locator('#settings-playback .eq-band-freq'))
    for (let i = 0; i < labels.length; i++) for (let j = i + 1; j < labels.length; j++) expect(intersects(labels[i], labels[j])).toBe(false)
    // One row: every frequency label shares a baseline (no staggering).
    for (const l of labels) expect(Math.abs(l.y - labels[0].y)).toBeLessThan(1)
    const dbs = await boxes(page.locator('#settings-playback .eq-band-db'))
    for (let i = 0; i < dbs.length; i++) for (let j = i + 1; j < dbs.length; j++) expect(intersects(dbs[i], dbs[j])).toBe(false)
    for (const r of [...rects, ...labels, ...dbs]) expect(r.x + r.width).toBeLessThanOrEqual(width)
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)

    // Keyboard still drives a band (up = more gain).
    const band = page.getByRole('slider', { name: '1 kHz gain' })
    await band.focus()
    await band.press('ArrowUp')
    await expect(band).toHaveValue('0.5')
    await expect(band).toHaveAttribute('aria-valuetext', '+0.5 dB')
  })

  // The sliders are vertical through writing-mode (no legacy appearance in the
  // engines tested): the top end is the most gain, the bottom the least, and
  // the arrow keys move the value the way they point.
  test('a vertical band has its maximum at the top, and the arrow keys follow', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 800 })
    await installApiMock(page)
    await page.goto('/#/home')
    await openSettings(page, 'Playback')
    const band = page.getByRole('slider', { name: '250 Hz gain' })
    await band.scrollIntoViewIfNeeded()
    const box = (await band.boundingBox())!
    expect(box.height).toBeGreaterThan(box.width * 3)
    const value = async () => Number(await band.inputValue())

    await page.mouse.click(box.x + box.width / 2, box.y + box.height * 0.1)
    await expect.poll(value).toBeGreaterThan(0)
    await page.mouse.click(box.x + box.width / 2, box.y + box.height * 0.9)
    await expect.poll(value).toBeLessThan(0)

    // From the low end (possibly the minimum itself): up twice, down once.
    await band.focus()
    const low = await value()
    await band.press('ArrowUp')
    await band.press('ArrowUp')
    await expect.poll(value).toBe(low + 1)
    await band.press('ArrowDown')
    await expect.poll(value).toBe(low + 0.5)
  })
})

test.describe('settings page', () => {
  test('desktop: a page in the main area, not a dialog over the app', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 })
    await installApiMock(page)
    await page.goto('/#/home')
    const card = await openSettings(page, 'Appearance')
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(page.getByRole('heading', { name: 'Settings', level: 1 })).toBeVisible()
    const main = (await page.locator('.app-main').boundingBox())!
    const box = (await card.boundingBox())!
    expect(box.x).toBeGreaterThanOrEqual(main.x)
    expect(box.x + box.width).toBeLessThanOrEqual(main.x + main.width)
  })

  test('phone: the cards fill the width of the page', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 800 })
    await installApiMock(page)
    await page.goto('/#/settings/appearance')
    const card = page.locator('#settings-appearance')
    await expect(card).toBeFocused()
    const box = (await card.boundingBox())!
    expect(box.x).toBeGreaterThanOrEqual(0)
    expect(box.width).toBeGreaterThan(375 - 40)
    await expect(page.getByRole('dialog')).toHaveCount(0)
  })
})

test.describe('Now Playing theme', () => {
  function luminance([r, g, b]: number[]) {
    const lin = (c: number) => {
      const s = c / 255
      return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
    }
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
  }

  for (const theme of ['light', 'dark']) {
    // A phone's full-screen sheet keeps its dark backdrop in every theme.
    test(`${theme} theme: the title stays light on the dark backdrop`, async ({ page }) => {
      await page.setViewportSize({ width: 390, height: 844 })
      await page.addInitScript((value) => {
        localStorage.setItem('atriolyric.settings', JSON.stringify({ theme: value }))
      }, theme)
      await installApiMock(page)
      await page.goto('/')
      if (theme === 'light') await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
      await playTrack(page, 'Bohemian Rhapsody')
      const overlay = await openNowPlaying(page)
      const color = await overlay.locator('.nowplaying-title').evaluate((el) => getComputedStyle(el).color)
      const rgb = color.match(/[\d.]+/g)!.slice(0, 3).map(Number)
      expect(luminance(rgb)).toBeGreaterThan(0.6)
    })
  }
})
