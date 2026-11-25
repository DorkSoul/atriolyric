import { type Page } from 'playwright/test'
import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'
import { player } from './app-helpers'

// Track rows and Home's track cards: the play/select button and the artist and
// album links are siblings, never nested.

async function songs(page: Page) {
  const mock = await installApiMock(page)
  await page.goto('/#/songs')
  const row = page.locator('.track-row').filter({ hasText: 'Bohemian Rhapsody' })
  await expect(row).toBeVisible()
  return { mock, row }
}

const started = (mock: Awaited<ReturnType<typeof installApiMock>>) =>
  mock.wsMessages.some((m) => m.type === 'set_queue_context' || m.type === 'set_queue' || m.type === 'play')

test.describe('song rows', () => {
  test('a row is a container with a play button and sibling links, nothing nested', async ({ page }) => {
    await songs(page)
    const nested = await page.evaluate(() => {
      const interactive = 'a[href], button, input, select, textarea, [role="button"], [role="link"], [tabindex]:not([tabindex="-1"])'
      const found: string[] = []
      for (const row of document.querySelectorAll('.track-row')) {
        if (row.matches('[role="button"]') || row.getAttribute('tabindex') != null) found.push('row is interactive')
        for (const el of row.querySelectorAll(interactive)) {
          if (el.parentElement?.closest(interactive) && el.parentElement.closest(interactive) !== row) {
            found.push(`${el.tagName} inside ${el.parentElement.closest(interactive)?.tagName}`)
          }
        }
      }
      return found
    })
    expect(nested).toEqual([])
  })

  test('Enter on the artist link follows the link and does not play the song', async ({ page }) => {
    const { mock, row } = await songs(page)
    const link = row.getByRole('link', { name: 'Queen' })
    await link.focus()
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/#\/artists\/Queen$/)
    expect(started(mock)).toBe(false)
    await expect(player(page)).not.toContainText('Bohemian Rhapsody')
  })

  test('Enter and Space on the play button play, and the tab order runs button, links, heart, menu', async ({ page }) => {
    const { mock, row } = await songs(page)
    const play = row.getByRole('button', { name: 'Play Bohemian Rhapsody' })
    await play.focus()
    // Forward through the row's controls.
    const order: string[] = []
    for (let i = 0; i < 5; i++) {
      order.push(
        await page.evaluate(() => {
          const el = document.activeElement as HTMLElement
          return el.tagName === 'A' ? `link:${el.textContent}` : (el.getAttribute('aria-label') ?? el.textContent ?? '')
        }),
      )
      await page.keyboard.press('Tab')
    }
    expect(order[0]).toBe('Play Bohemian Rhapsody')
    expect(order[1]).toBe('link:Queen')
    expect(order[2]).toBe('link:A Night at the Opera')
    expect(order[3]).toMatch(/^Not liked: Bohemian Rhapsody/)
    expect(order[4]).toBe('More actions for Bohemian Rhapsody')

    await play.focus()
    await page.keyboard.press('Space')
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    expect(started(mock)).toBe(true)
  })

  test('presses on the art, the title and the empty part of the row play it; the links, heart and menu do their own thing', async ({ page }) => {
    const { mock, row } = await songs(page)
    // What is under the pointer at each place.
    const under = (selector: string, where: 'center' | 'left') =>
      row.locator(selector).first().evaluate((el, at) => {
        const rect = el.getBoundingClientRect()
        const x = at === 'left' ? rect.left + 4 : rect.left + rect.width / 2
        const hit = document.elementFromPoint(x, rect.top + rect.height / 2) as HTMLElement
        const control = hit.closest('a, button') ?? hit
        return `${control.tagName}:${control.getAttribute('class') ?? ''}`
      }, where)
    expect(await under('.art-wrap', 'center')).toContain('track-play-btn')
    expect(await under('.track-title', 'left')).toContain('track-play-btn')
    expect(await under('.library-links a', 'center')).toContain('A:')
    expect(await under('.favorite-btn', 'center')).toContain('favorite-btn')
    expect(await under('.track-menu button', 'center')).toContain('BUTTON:')

    const artBox = (await row.locator('.art-wrap').first().boundingBox())!
    await page.mouse.click(artBox.x + artBox.width / 2, artBox.y + artBox.height / 2)
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    expect(started(mock)).toBe(true)

    // The convenience of a click anywhere on the row still plays it: here the
    // padding left of the art.
    const midnight = page.locator('.track-row').filter({ hasText: 'Midnight City' })
    mock.wsMessages.length = 0
    const box = (await midnight.boundingBox())!
    await page.mouse.click(box.x + 3, box.y + box.height / 2)
    await expect(player(page)).toContainText('Midnight City')
  })

  test('the row keeps its size and the button covers exactly the old target', async ({ page }) => {
    const { row } = await songs(page)
    const box = async (selector: string) => (await row.locator(selector).first().boundingBox())!
    const target = await box('.track-title-btn')
    const button = await box('.track-play-btn')
    expect(button).toEqual(target)
    const rowBox = (await row.boundingBox())!
    // Rows stay the fixed height the virtual list assumes: art + padding.
    const heights = await page.locator('.track-row').evaluateAll((els) => els.map((el) => el.getBoundingClientRect().height))
    expect(new Set(heights.map((h) => Math.round(h))).size).toBe(1)
    expect(target.x).toBeGreaterThanOrEqual(rowBox.x)
    // The heart and the menu button stay to the right of the target.
    const heart = await box('.favorite-btn')
    expect(heart.x).toBeGreaterThanOrEqual(target.x + target.width - 1)
  })

  test('in selection mode the buttons say Select / Deselect and expose aria-pressed', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/songs')
    const row = page.locator('.track-row').filter({ hasText: 'Bohemian Rhapsody' })
    await row.getByRole('button', { name: /^More actions for/ }).click()
    await page.getByRole('menuitem', { name: 'Select' }).click()
    await expect(page.locator('.bulk-action-bar')).toContainText('1 selected')

    const selected = page.getByRole('button', { name: 'Deselect Bohemian Rhapsody' })
    const other = page.getByRole('button', { name: 'Select Midnight City' })
    await expect(selected).toHaveAttribute('aria-pressed', 'true')
    await expect(other).toHaveAttribute('aria-pressed', 'false')
    await expect(page.getByRole('button', { name: /^Play / })).toHaveCount(0)

    await other.click()
    await expect(page.locator('.bulk-action-bar')).toContainText('2 selected')
    await expect(page.getByRole('button', { name: 'Deselect Midnight City' })).toHaveAttribute('aria-pressed', 'true')
    await selected.focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('button', { name: 'Select Bohemian Rhapsody' })).toHaveAttribute('aria-pressed', 'false')

    await page.locator('.bulk-action-bar').getByRole('button', { name: 'Cancel' }).click()
    await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).not.toHaveAttribute('aria-pressed', /./)
  })

  test('right-click still opens the row menu from the play button', async ({ page }) => {
    const { row } = await songs(page)
    await row.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click({ button: 'right' })
    await expect(page.getByRole('menu', { name: 'Actions for Bohemian Rhapsody' })).toBeVisible()
  })
})

test.describe('Home track cards', () => {
  async function home(page: Page) {
    const mock = await installApiMock(page)
    await page.goto('/#/home')
    const rail = page.locator('.home-rail').filter({ hasText: 'Recommended from your library' })
    const card = rail.locator('.rail-card').first()
    await expect(card).toBeVisible()
    return { mock, card }
  }

  test('a card is a container: a play button named for the song and sibling links', async ({ page }) => {
    const { card } = await home(page)
    await card.scrollIntoViewIfNeeded()
    await expect(card).not.toHaveAttribute('role', 'button')
    await expect(card).not.toHaveAttribute('tabindex', /./)
    await expect(card.getByRole('button', { name: /^Play / })).toHaveCount(1)
    expect(await card.evaluate((el) => el.querySelectorAll('button a, a button, [role="button"] a').length)).toBe(0)
    // The overlay fills the card and the art lets the press through.
    const cardBox = (await card.boundingBox())!
    const buttonBox = (await card.locator('.rail-card-play').boundingBox())!
    expect(buttonBox).toEqual(cardBox)
    expect(
      await card.locator('.rail-card-art').evaluate((el) => {
        const r = el.getBoundingClientRect()
        return (document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2) as HTMLElement).className
      }),
    ).toContain('rail-card-play')
  })

  test('Enter on a card\'s artist link follows it and does not play', async ({ page }) => {
    const { mock, card } = await home(page)
    const link = card.getByRole('link').first()
    await link.focus()
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/#\/(artists|albums)\//)
    expect(started(mock)).toBe(false)
  })

  test('the card plays from its button', async ({ page }) => {
    const { mock, card } = await home(page)
    const play = card.getByRole('button', { name: /^Play / })
    await play.focus()
    await page.keyboard.press('Enter')
    await expect.poll(() => started(mock)).toBe(true)
  })
})
