// Settings as a page: one column of cards, section chips, deep links, lazy data.
import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'
import type { AppMockState } from './mockApi'
import { openSettings } from './app-helpers'
import type { Page } from 'playwright/test'

const SECTIONS = ['account', 'playback', 'appearance', 'navigation', 'discovery', 'scrobbling', 'about']
const LAZY_PATHS = [
  '/api/integrations',
  '/api/library/discovery/settings',
  '/api/auth/privacy-settings',
]
const called = (mock: AppMockState, path: string) => mock.calls.some((c) => c.method === 'GET' && c.path === path)

async function scrollToEnd(page: Page) {
  await page.evaluate(async () => {
    const el = document.querySelector('.app-main')!
    for (let y = 0; y <= el.scrollHeight; y += 300) {
      el.scrollTop = y
      await new Promise((resolve) => setTimeout(resolve, 60))
    }
  })
}

test('Settings shows every section in one full-width column, in order', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 })
  await installApiMock(page)
  await page.goto('/#/settings')
  await expect(page.getByRole('heading', { name: 'Settings', level: 1 })).toBeVisible()
  const cards = page.locator('.settings-column > .settings-section')
  await expect(cards).toHaveCount(7)
  expect(await cards.evaluateAll((els) => els.map((el) => el.id))).toEqual(SECTIONS.map((s) => `settings-${s}`))
  const boxes = await cards.evaluateAll((els) =>
    els.map((el) => {
      const r = el.getBoundingClientRect()
      return { x: Math.round(r.x), width: Math.round(r.width) }
    }),
  )
  expect(new Set(boxes.map((b) => b.x)).size).toBe(1)
  expect(new Set(boxes.map((b) => b.width)).size).toBe(1)
  const main = (await page.locator('.app-main').boundingBox())!
  expect(boxes[0].width).toBeGreaterThan(main.width * 0.8)
  await expect(page.getByRole('dialog')).toHaveCount(0)
})

test('the section chips scroll to a section, move focus there and follow in the address', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 700 })
  await installApiMock(page)
  // Start on Home and move to Settings, so there is an entry to go Back to
  // (Firefox drops the blank first page from the history).
  await page.goto('/#/home')
  await expect(page.getByRole('navigation').first()).toBeVisible()
  await page.goto('/#/settings')
  const chips = page.getByRole('navigation', { name: 'Settings sections' })
  await expect(chips.getByRole('button')).toHaveCount(7)
  for (const section of ['Navigation', 'About', 'Playback']) {
    await chips.getByRole('button', { name: section, exact: true }).click()
    const card = page.locator(`#settings-${section.toLowerCase()}`)
    await expect(card).toBeFocused()
    await expect(chips.getByRole('button', { name: section, exact: true })).toHaveAttribute('aria-current', 'true')
    await expect(page).toHaveURL(new RegExp(`#/settings/${section.toLowerCase()}$`))
    const main = (await page.locator('.app-main').boundingBox())!
    const top = (await card.boundingBox())!.y
    expect(top).toBeGreaterThanOrEqual(main.y - 1)
    expect(top).toBeLessThan(main.y + main.height)
  }
  // A chip is a replacement of the address, not a step: Back leaves Settings.
  await page.goBack()
  await expect(page).not.toHaveURL(/#\/settings/)
})

test('#/settings/<section> opens on that section; an unknown one opens the page', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 700 })
  await installApiMock(page)
  await page.goto('/#/settings/discovery')
  await expect(page.locator('#settings-discovery')).toBeFocused()
  await expect(page.locator('#settings-discovery')).toBeInViewport()
  await page.goto('/#/settings/nonsense')
  await expect(page.getByRole('heading', { name: 'Settings', level: 1 })).toBeVisible()
  await expect(page.locator('.settings-column > .settings-section')).toHaveCount(7)
})

test('gear menu items land on the right section and Back returns to where you were', async ({ page }) => {
  await installApiMock(page)
  await page.goto('/#/songs')
  await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
  for (const section of ['Playback', 'Scrobbling']) {
    const card = await openSettings(page, section)
    await expect(card).toBeInViewport()
    await expect(page).toHaveURL(new RegExp(`#/settings/${section.toLowerCase()}$`))
  }
  await page.goBack()
  await expect(page).toHaveURL(/#\/settings\/playback$/)
  await page.goBack()
  await expect(page).toHaveURL(/#\/songs$/)
  await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
})

test('section data is requested only when its card comes near the screen', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 500 })
  const mock = await installApiMock(page)
  await page.goto('/#/settings')
  await expect(page.locator('#settings-account')).toBeVisible()
  await expect(page.locator('#settings-discovery')).not.toBeInViewport()
  await page.waitForLoadState('networkidle')
  for (const path of LAZY_PATHS) expect(called(mock, path), path).toBe(false)
  await expect(page.locator('#settings-discovery')).toContainText('Loading discovery settings')

  await scrollToEnd(page)
  for (const path of LAZY_PATHS) await expect.poll(() => called(mock, path), path).toBe(true)
  await expect(page.locator('#settings-discovery').getByRole('switch', { name: 'Enabled' })).toBeVisible()
})

test('the equaliser loads with the Playback card, not before', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 500 })
  await installApiMock(page)
  await page.goto('/#/settings/about')
  await expect(page.locator('#settings-about')).toBeFocused()
  await expect(page.locator('.eq-bands')).toHaveCount(0)
  await page.getByRole('navigation', { name: 'Settings sections' }).getByRole('button', { name: 'Playback' }).click()
  await expect(page.locator('#settings-playback').getByRole('slider')).toHaveCount(10)
})

test('a failing section request shows an error in its card and leaves the page working', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 700 })
  await installApiMock(page)
  let failing = true
  await page.route(/\/api\/(integrations|library\/discovery\/settings)$/, (route) =>
    failing ? route.fulfill({ status: 500, json: { detail: 'The server broke.' } }) : route.fallback(),
  )
  await page.goto('/#/settings/discovery')
  const discovery = page.locator('#settings-discovery')
  await expect(discovery.getByRole('alert')).toContainText('Could not load the discovery settings.')
  // The privacy setting of the same card still loaded.
  await expect(discovery.getByRole('switch', { name: 'ListenBrainz recommendations' })).toBeVisible()
  await page.getByRole('navigation', { name: 'Settings sections' }).getByRole('button', { name: 'Scrobbling' }).click()
  const scrobbling = page.locator('#settings-scrobbling')
  await expect(scrobbling.getByRole('alert')).toContainText('Could not load the ListenBrainz status.')
  // The rest of the page is fine.
  await page.locator('#settings-appearance').getByRole('button', { name: 'Light', exact: true }).scrollIntoViewIfNeeded()
  await page.locator('#settings-appearance').getByRole('button', { name: 'Light', exact: true }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')

  failing = false
  await scrobbling.getByRole('button', { name: 'Retry' }).click()
  await expect(scrobbling.getByRole('button', { name: 'Connect ListenBrainz' })).toBeVisible()
  await discovery.getByRole('button', { name: 'Retry' }).click()
  await expect(discovery.getByRole('switch', { name: 'Enabled' })).toBeVisible()
})

test('Navigation shows the phone bar and More groups; moving a tab up puts it on the bar', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await installApiMock(page)
  await page.goto('/#/settings/navigation')
  const nav = page.locator('#settings-navigation')
  await expect(nav).toBeFocused()
  const bar = nav.getByRole('list', { name: 'Phone bottom bar' })
  const more = nav.getByRole('list', { name: 'Under More on a phone' })
  await expect(bar.locator('.tab-order-label')).toHaveText(['Artists', 'Albums', 'Songs'])
  await expect(more.locator('.tab-order-label')).toHaveText(['Playlists', 'Now playing', 'Radio', 'Videos', 'Files', 'Want list'])

  const phoneBar = page.getByRole('navigation', { name: 'Quick tab navigation' })
  await expect(phoneBar.getByRole('button', { name: 'Playlists', exact: true })).toHaveCount(0)
  await expect(nav.getByRole('button', { name: /^Move / })).toHaveCount(0)
  await nav.getByRole('button', { name: 'Reorder Playlists' }).focus()
  await page.keyboard.press('ArrowUp')
  await expect(bar.locator('.tab-order-label')).toHaveText(['Artists', 'Albums', 'Playlists'])
  await expect(more.locator('.tab-order-label')).toHaveText(['Songs', 'Now playing', 'Radio', 'Videos', 'Files', 'Want list'])
  await expect(phoneBar.getByRole('button', { name: 'Playlists', exact: true })).toBeVisible()
  await expect(phoneBar.getByRole('button', { name: 'Songs', exact: true })).toHaveCount(0)

  // A hidden tab drops out of the top three: the next one takes its place.
  await bar.locator('.tab-order-row').filter({ hasText: 'Albums' }).getByLabel('Visible').uncheck()
  await expect(phoneBar.getByRole('button', { name: 'Albums', exact: true })).toHaveCount(0)
  await expect(phoneBar.getByRole('button', { name: 'Songs', exact: true })).toBeVisible()
})

test('List density is a Comfortable / Compact choice', async ({ page }) => {
  await installApiMock(page)
  await page.goto('/#/settings/appearance')
  const group = page.locator('#settings-appearance').getByRole('group', { name: 'List density' })
  await expect(group.getByRole('button', { name: 'Comfortable' })).toHaveAttribute('aria-pressed', 'true')
  await group.getByRole('button', { name: 'Compact' }).click()
  await expect(group.getByRole('button', { name: 'Compact' })).toHaveAttribute('aria-pressed', 'true')
  await expect(page.locator('html')).toHaveAttribute('data-density', 'compact')
  await group.getByRole('button', { name: 'Comfortable' }).click()
  await expect(page.locator('html')).not.toHaveAttribute('data-density', 'compact')
})

test('Discovery offers Customise Home sections', async ({ page }) => {
  await installApiMock(page)
  await page.goto('/#/settings/discovery')
  const discovery = page.locator('#settings-discovery')
  await discovery.getByText('Customise Home sections').click()
  await expect(discovery.locator('.home-sections-row').first()).toBeVisible()
})

test('Install as app is offered in About while the app is not installed', async ({ page }) => {
  await installApiMock(page)
  await page.goto('/#/settings/about')
  await expect(page.locator('#settings-about').getByRole('button', { name: 'Install as app' })).toBeVisible()
})

for (const width of [1440, 1024, 800, 390, 320]) {
  test(`Settings and Admin have no horizontal overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 })
    await installApiMock(page)
    for (const hash of ['/#/settings', '/#/admin']) {
      await page.goto(hash)
      await expect(page.locator('.settings-page, .admin-panel').first()).toBeVisible()
      await scrollToEnd(page)
      await page.waitForTimeout(300)
      const overflow = await page.evaluate(() => {
        const main = document.querySelector('.app-main')!
        return {
          page: document.documentElement.scrollWidth - window.innerWidth,
          main: main.scrollWidth - main.clientWidth,
        }
      })
      expect(overflow.page, `${hash} page`).toBeLessThanOrEqual(0)
      expect(overflow.main, `${hash} main`).toBeLessThanOrEqual(0)
    }
  })
}

test('the new username box is not capitalised or corrected by a phone keyboard', async ({ page }) => {
  await installApiMock(page)
  await page.goto('/#/settings/account')
  const username = page.getByLabel('New username')
  await expect(username).toHaveAttribute('autocapitalize', 'none')
  await expect(username).toHaveAttribute('autocorrect', 'off')
  await expect(username).toHaveAttribute('spellcheck', 'false')
})
