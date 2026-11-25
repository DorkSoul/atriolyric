import type { Locator, Page } from 'playwright/test'
import { expect, test } from './fixtures'
import { installApiMock, type MediaItem } from './mockApi'
import { installWantListMock } from './wantListMock'
import { devicePickerToggle } from './app-helpers'

// Every menu and dialog on a phone (390 and 320 wide) stays inside the screen
// with at least 12px to spare on each side, and nothing inside it runs off
// its sides (long labels wrap, tall ones scroll inside).

const MARGIN = 12
const HEIGHT = 700

const PIXEL =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='

function collab(id: number, values: Partial<MediaItem>): MediaItem {
  return {
    id,
    library_root_id: 1,
    kind: 'track',
    title: `Track ${id}`,
    artist: 'Artist',
    album: 'Album',
    genre: 'Rock',
    track_number: 1,
    performer: null,
    description: null,
    duration_seconds: 200,
    is_favorite: false,
    is_disliked: false,
    excluded_by: null,
    format_label: null,
    ...values,
  }
}

async function setup(page: Page) {
  const mock = await installApiMock(page)
  await installWantListMock(page)
  mock.tracks.push(
    collab(60, {
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
  await page.route(/\/api\/art\/album\/[^/]+\/candidates$/, (route) =>
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify([
        { id: 'embedded:1', source: 'Embedded', label: 'In “Bohemian Rhapsody”', url: '/x.png', thumb_url: '/x.png' },
        {
          id: 'url:https://coverartarchive.org/f.jpg',
          source: 'Cover Art Archive',
          label: 'Front · 1975 · GB · a rather long description of this particular release',
          url: '/y.png',
          thumb_url: '/y.png',
        },
        { id: 'url:https://upload.wikimedia.org/c.jpg', source: 'Wikimedia Commons', label: 'Jane Photographer · CC BY-SA 4.0', url: '/z.png', thumb_url: '/z.png' },
      ]),
    }),
  )
  await page.route(/\/api\/library\/labels\?/, (route) =>
    route.fulfill({
      json: {
        genres: [{ name: 'Rock', track_count: 12 }, { name: 'Progressive rock and other very long genre names', track_count: 3 }],
        tags: [{ name: 'classic', track_count: 2 }, { name: 'seventies', track_count: 1 }],
      },
    }),
  )
  await page.route(/\/(x|y|z)\.png$/, (route) => route.fulfill({ contentType: 'image/png', body: Buffer.from(PIXEL, 'base64') }))
  return mock
}

const menu = (page: Page, name: string | RegExp) => page.getByRole('menu', { name })
const dialog = (page: Page, name: string | RegExp) => page.getByRole('dialog', { name })

async function trackMenu(page: Page) {
  await page.goto('/#/songs')
  await page.getByRole('button', { name: 'More actions for Bohemian Rhapsody' }).click()
  return menu(page, 'Actions for Bohemian Rhapsody')
}

async function albumCardMenu(page: Page) {
  await page.goto('/#/albums')
  await page.getByRole('button', { name: 'More actions for A Night at the Opera' }).click()
  return menu(page, /Actions for A Night at the Opera/)
}

async function artistCardMenu(page: Page) {
  await page.goto('/#/artists')
  await page.getByRole('button', { name: 'More actions for Queen', exact: true }).click()
  return menu(page, /Actions for Queen/)
}

const OPENERS: Record<string, (page: Page) => Promise<Locator>> = {
  'song menu': trackMenu,
  'song menu, add to playlist': async (page) => {
    const panel = await trackMenu(page)
    await panel.getByRole('menuitem', { name: 'Add to playlist' }).click()
    await expect(panel.getByRole('menuitem', { name: 'Back' })).toBeVisible()
    return panel
  },
  'album card menu': albumCardMenu,
  'artist card menu': artistCardMenu,
  'sort menu': async (page) => {
    await page.goto('/#/albums')
    await page.getByRole('button', { name: /^Sort/ }).click()
    return menu(page, 'Sort by')
  },
  'playlist card menu': async (page) => {
    await page.goto('/#/playlists')
    await page.getByRole('button', { name: 'More actions for Workout' }).click()
    return menu(page, /Actions for Workout/)
  },
  'settings menu': async (page) => {
    await page.goto('/#/home')
    await page.getByRole('button', { name: 'Settings', exact: true }).click()
    return menu(page, 'Settings')
  },
  'device picker': async (page) => {
    await page.goto('/#/home')
    await devicePickerToggle(page).click()
    return menu(page, 'Playback devices')
  },
  'song info': async (page) => {
    await (await trackMenu(page)).getByRole('menuitem', { name: 'Info', exact: true }).click()
    return dialog(page, 'Bohemian Rhapsody')
  },
  'album info': async (page) => {
    await (await albumCardMenu(page)).getByRole('menuitem', { name: 'Info', exact: true }).click()
    return dialog(page, 'A Night at the Opera')
  },
  'artist info': async (page) => {
    await (await artistCardMenu(page)).getByRole('menuitem', { name: 'Info', exact: true }).click()
    return dialog(page, 'Queen')
  },
  'genres and tags': async (page) => {
    await page.goto('/#/albums/Queen/A%20Night%20at%20the%20Opera')
    await page.getByRole('button', { name: 'Genres/tags' }).click()
    return page.getByRole('dialog')
  },
  'edit tags': async (page) => {
    await (await trackMenu(page)).getByRole('menuitem', { name: 'Edit tags' }).click()
    return dialog(page, 'Edit tags')
  },
  'edit tags of a selection': async (page) => {
    await (await trackMenu(page)).getByRole('menuitem', { name: 'Select', exact: true }).click()
    await page.getByRole('button', { name: 'Edit tags', exact: true }).click()
    return dialog(page, /^Edit \d+ tracks?$|^Edit tags$/)
  },
  'art picker': async (page) => {
    await (await albumCardMenu(page)).getByRole('menuitem', { name: 'Choose from found artwork…' }).click()
    const picker = dialog(page, 'Choose artwork')
    await expect(picker.getByRole('region', { name: 'Wikimedia Commons' })).toBeVisible()
    return picker
  },
  'artist picker': async (page) => {
    await page.goto('/#/songs')
    await page.getByRole('link', { name: 'Queen feat. David Bowie' }).click()
    return dialog(page, 'Choose an artist')
  },
  'confirm dialog': async (page) => {
    await page.goto('/#/playlists')
    await page.getByRole('button', { name: 'Delete Workout' }).click()
    return page.getByRole('dialog', { name: /Delete/ })
  },
  'specify mix': async (page) => {
    await page.goto('/#/home')
    await page.getByRole('button', { name: 'Specify mix' }).first().click()
    return dialog(page, 'Specify mix')
  },
}

/** Where `popup` overflows the screen's 12px frame, or what inside it runs
 * past its sides (outside a sideways scroller). Empty when it all fits. */
async function problems(popup: Locator): Promise<string[]> {
  return popup.evaluate((el, margin) => {
    const out: string[] = []
    const box = el.getBoundingClientRect()
    if (box.left < margin - 0.5) out.push(`left ${box.left}`)
    if (box.top < margin - 0.5) out.push(`top ${box.top}`)
    if (box.right > window.innerWidth - margin + 0.5) out.push(`right ${window.innerWidth - box.right}`)
    if (box.bottom > window.innerHeight - margin + 0.5) out.push(`bottom ${window.innerHeight - box.bottom}`)
    if (el.scrollWidth > el.clientWidth + 1) out.push(`scrolls sideways ${el.scrollWidth} > ${el.clientWidth}`)
    const inScroller = (node: Element) => {
      for (let a = node.parentElement; a && a !== el; a = a.parentElement) {
        const style = getComputedStyle(a)
        if ((style.overflowX === 'auto' || style.overflowX === 'scroll') && a.scrollWidth > a.clientWidth) return true
      }
      return false
    }
    for (const child of el.querySelectorAll('*')) {
      const r = child.getBoundingClientRect()
      if (r.width === 0 || r.height === 0) continue
      if ((r.right > box.right + 1 || r.left < box.left - 1) && !inScroller(child)) {
        out.push(`${child.tagName.toLowerCase()}.${child.className} "${(child.textContent ?? '').slice(0, 30)}" sticks out`)
      }
    }
    return out
  }, MARGIN)
}

for (const width of [390, 320]) {
  test.describe(`pop-ups at ${width} wide`, () => {
    test.use({ viewport: { width, height: HEIGHT } })

    for (const [name, open] of Object.entries(OPENERS)) {
      test(name, async ({ page }) => {
        await setup(page)
        const popup = await open(page)
        await expect(popup).toBeVisible()
        // Let a placed menu settle where it is put (it is measured, then moved).
        await expect.poll(() => popup.evaluate((el) => getComputedStyle(el).visibility)).toBe('visible')
        expect(await problems(popup)).toEqual([])
      })
    }
  })
}
