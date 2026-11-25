import type { Locator, Page } from 'playwright/test'
import { expect, test } from './fixtures'
import { installApiMock, type MediaItem } from './mockApi'

// Song titles that do not fit end in "…" at rest, and scroll (the player
// bar's marquee) only while their row is hovered or focused, or is the
// current song; nothing moves with reduced motion; row heights never change.

const LONG =
  'Bohemian Rhapsody (Live at Wembley Stadium, 1986, Remastered 2011 Deluxe Edition with an Extremely Long Subtitle)'
const LONG_ALBUM = 'A Night at the Opera (Collector’s Edition with Bonus Material and a Great Many More Words)'
const LIBRARY: Array<Partial<MediaItem> & { id: number; title: string }> = [
  { id: 41, title: LONG, artist: 'Queen', album: LONG_ALBUM, year: 1975, play_count: 12 },
  { id: 42, title: 'Short', artist: 'Band', album: 'Tiny', year: 1999, play_count: 3 },
  { id: 43, title: 'Another One That Is Quite Long Too, Really Rather Long Indeed, Longer Than Any Column Here', artist: 'Band', album: 'Tiny', year: 2001, play_count: 1 },
]

async function openSongs(page: Page, width = 1440, height = 900) {
  await page.setViewportSize({ width, height })
  const mock = await installApiMock(page)
  const template = mock.tracks.find((track) => track.kind === 'track')!
  mock.tracks = LIBRARY.map((values) => ({ ...template, artists: undefined, album_artist: null, genre: 'Rock', ...values }))
  await page.goto('/#/songs')
  await expect(page.getByRole('button', { name: `Play ${LONG}` })).toBeVisible()
  return mock
}

const row = (page: Page, title: string) => page.locator('.track-row', { has: page.getByRole('button', { name: `Play ${title}` }) })
const titleOf = (r: Locator) => r.locator('.track-title')
const state = (el: Locator) => el.getAttribute('data-marquee')
/** How many animations run on the text inside `el`, and where the first is. */
const motion = (el: Locator) =>
  el.locator('.marquee-text').evaluate((text) => {
    const [animation] = text.getAnimations()
    return { running: text.getAnimations().length, transform: getComputedStyle(text).transform, time: Number(animation?.currentTime ?? 0) }
  })
const height = async (r: Locator) => (await r.boundingBox())!.height

test('at 1440 with Year and Plays a long title is clipped in its cell and ends in an ellipsis', async ({ page }) => {
  await openSongs(page)
  await expect(page.locator('.track-table-head')).toContainText('Plays')
  const r = row(page, LONG)
  const title = titleOf(r)
  const facts = await title.evaluate((el) => {
    const style = getComputedStyle(el)
    const cell = el.closest('.track-title-btn')!.getBoundingClientRect()
    const box = el.getBoundingClientRect()
    return {
      textOverflow: style.textOverflow,
      overflow: style.overflowX,
      wider: el.scrollWidth > el.clientWidth,
      insideCell: box.right <= cell.right + 0.5,
      // The text node is an inline span at rest (an atomic inline-block would be clipped hard).
      textDisplay: getComputedStyle(el.querySelector('.marquee-text')!).display,
    }
  })
  expect(facts).toEqual({ textOverflow: 'ellipsis', overflow: 'hidden', wider: true, insideCell: true, textDisplay: 'inline' })
  expect(await state(title)).toBe('static')
  // Nothing moves at rest.
  expect((await motion(title)).running).toBe(0)
  // The artist and album cells are cut the same way.
  for (const cls of ['.track-col-artist', '.track-col-album']) {
    const cell = r.locator(cls)
    expect(await cell.evaluate((el) => getComputedStyle(el).textOverflow)).toBe('ellipsis')
  }
  const album = r.locator('.track-col-album .marquee')
  expect(await album.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true)
})

test('hovering the row scrolls the title, leaving restores it; the row height never changes', async ({ page }) => {
  await openSongs(page)
  const r = row(page, LONG)
  const title = titleOf(r)
  const before = await height(r)
  await r.hover()
  await expect.poll(() => state(title)).toBe('scroll')
  await expect.poll(async () => (await motion(title)).running).toBe(1)
  // The artist and album move with it.
  await expect.poll(() => state(r.locator('.track-col-album .marquee'))).toBe('scroll')
  // Waits out the hold at the start, then the text really moves.
  await expect.poll(async () => (await motion(title)).transform, { timeout: 8000 }).not.toBe('none')
  expect(await height(r)).toBe(before)
  expect(await height(row(page, 'Short'))).toBe(before)

  await page.mouse.move(2, 2)
  await expect.poll(() => state(title)).toBe('static')
  await expect.poll(async () => (await motion(title)).running).toBe(0)
  expect(await title.evaluate((el) => getComputedStyle(el.querySelector('.marquee-text')!).display)).toBe('inline')
  expect(await height(r)).toBe(before)
})

test('keyboard focus in the row scrolls it, leaving the row stops it', async ({ page }) => {
  await openSongs(page)
  const r = row(page, LONG)
  const title = titleOf(r)
  await r.getByRole('button', { name: `Play ${LONG}` }).focus()
  await expect.poll(() => state(title)).toBe('scroll')
  // Tabbing to the row's own links keeps it going; tabbing out of the row stops it.
  await page.keyboard.press('Tab')
  await expect(r.getByRole('link', { name: 'Queen' })).toBeFocused()
  expect(await state(title)).toBe('scroll')
  await row(page, 'Short').getByRole('button', { name: 'Play Short' }).focus()
  await expect.poll(() => state(title)).toBe('static')
  expect((await motion(title)).running).toBe(0)
})

test('the current song scrolls without hover; a short title never animates', async ({ page }) => {
  await openSongs(page)
  const r = row(page, LONG)
  await r.getByRole('button', { name: `Play ${LONG}` }).click()
  await page.mouse.move(2, 2)
  await expect(r).toHaveClass(/track-row-current/)
  await expect.poll(() => state(titleOf(r))).toBe('scroll')
  expect((await motion(titleOf(r))).running).toBe(1)
  // Others stay still.
  expect(await state(titleOf(row(page, 'Another One That Is Quite Long Too, Really Rather Long Indeed, Longer Than Any Column Here')))).toBe('static')

  const short = row(page, 'Short')
  await short.hover()
  await page.waitForTimeout(600)
  expect(await state(titleOf(short))).toBe('static')
  expect((await motion(titleOf(short))).running).toBe(0)
  expect(await titleOf(short).evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true)
})

test('a press held on a touch screen starts the scroll, a quick touch does not', async ({ page }) => {
  await openSongs(page)
  const r = row(page, LONG)
  const title = titleOf(r)
  await r.evaluate((el) => el.dispatchEvent(new PointerEvent('pointerenter', { pointerType: 'touch' })))
  await page.waitForTimeout(100)
  expect(await state(title)).toBe('static')
  await r.evaluate((el) => el.dispatchEvent(new PointerEvent('pointerleave', { pointerType: 'touch' })))
  await page.waitForTimeout(500)
  expect(await state(title)).toBe('static')

  await r.evaluate((el) => el.dispatchEvent(new PointerEvent('pointerenter', { pointerType: 'touch' })))
  await expect.poll(() => state(title)).toBe('scroll')
  await r.evaluate((el) => el.dispatchEvent(new PointerEvent('pointercancel', { pointerType: 'touch' })))
  await expect.poll(() => state(title)).toBe('static')
})

test('with reduced motion nothing moves, and the full title is in the tooltip', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await openSongs(page)
  const r = row(page, LONG)
  await r.getByRole('button', { name: `Play ${LONG}` }).click()
  await r.hover()
  await page.waitForTimeout(700)
  const title = titleOf(r)
  expect(await state(title)).toBe('static')
  expect((await motion(title)).running).toBe(0)
  await expect(title).toHaveAttribute('title', LONG)
  expect(await title.evaluate((el) => getComputedStyle(el).textOverflow)).toBe('ellipsis')
})

test('Cards and a phone list all end a long title in an ellipsis', async ({ page }) => {
  await openSongs(page)
  const check = async (what: string) => {
    const title = titleOf(row(page, LONG))
    await expect(title, what).toBeVisible()
    const facts = await title.evaluate((el) => ({
      overflow: getComputedStyle(el).textOverflow,
      wider: el.scrollWidth > el.clientWidth,
      display: getComputedStyle(el.querySelector('.marquee-text')!).display,
    }))
    expect(facts, what).toEqual({ overflow: 'ellipsis', wider: true, display: 'inline' })
  }
  await page.getByRole('button', { name: 'Cards', exact: true }).click()
  await page.setViewportSize({ width: 900, height: 900 })
  await check('cards')
  await page.getByRole('button', { name: 'Table', exact: true }).click()
  await page.setViewportSize({ width: 390, height: 800 })
  await check('phone list rows')
  const r = row(page, LONG)
  const before = await height(r)
  await r.hover()
  await expect.poll(() => state(titleOf(r))).toBe('scroll')
  expect(await height(r)).toBe(before)
})
