import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'
import type { Page } from 'playwright/test'

// The browser's debug tracing is off unless /api/version says `debug_log: true`.
// The mock answers /api/version with `{version}` only, so by default it is off.

type PatchWindow = Window & { __playBefore?: unknown }

async function rememberPlay(page: Page) {
  // Registered after the mock's own init script, so this sees whatever
  // `play` the page starts with; the app's patch (if any) comes later.
  await page.addInitScript(() => {
    ;(window as PatchWindow).__playBefore = HTMLMediaElement.prototype.play
  })
}

const playIsUntouched = (page: Page) =>
  page.evaluate(() => HTMLMediaElement.prototype.play === (window as PatchWindow).__playBefore)

test('debug log: with the default server answer nothing is patched or posted', async ({ page }) => {
  const posts: string[] = []
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/api/client-log/debug') posts.push(request.method())
  })
  await installApiMock(page)
  await rememberPlay(page)
  await page.goto('/')
  await expect(page.getByRole('navigation').first()).toBeVisible()
  // Longer than the 2 s flush interval, so a batch would have gone out.
  await page.waitForTimeout(2_600)
  expect(posts).toEqual([])
  expect(await playIsUntouched(page)).toBe(true)
})

test('debug log: when the server asks for it, batches are posted and the player is patched', async ({ page }) => {
  await installApiMock(page)
  await rememberPlay(page)
  await page.route('**/api/version', (route) =>
    route.fulfill({ contentType: 'application/json', body: JSON.stringify({ version: null, debug_log: true }) }),
  )
  const posted = page.waitForRequest(
    (request) => new URL(request.url()).pathname === '/api/client-log/debug' && request.method() === 'POST',
  )
  await page.goto('/')
  const batch = (await posted).postDataJSON() as { session: string; events: Array<{ event: string }> }
  expect(batch.events.map((event) => event.event)).toContain('app.start')
  expect(await playIsUntouched(page)).toBe(false)
})

// On the login page every flush would be a 401, and a burst of those can get
// a visitor IP-banned by a proxy's CrowdSec rules.
test('debug log: nothing is posted while signed out, even when the server asks for it', async ({ page }) => {
  const posts: string[] = []
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/api/client-log/debug') posts.push(request.method())
  })
  await installApiMock(page, false)
  await page.route('**/api/version', (route) =>
    route.fulfill({ contentType: 'application/json', body: JSON.stringify({ version: null, debug_log: true }) }),
  )
  await page.goto('/')
  await page.waitForTimeout(4_500)
  expect(posts).toEqual([])
})

// A batch over the server's limit of 50 events is refused with 422; put back
// and sent again every two seconds, it would keep any debug line from that
// browser out of the log.
test('debug log: batches stay within the server limit and a refused one is not sent again', async ({ page }) => {
  await installApiMock(page)
  await page.route('**/api/version', (route) =>
    route.fulfill({ contentType: 'application/json', body: JSON.stringify({ version: null, debug_log: true }) }),
  )
  const sizes: number[] = []
  const marks: number[] = []
  await page.route('**/api/client-log/debug', (route) => {
    const body = route.request().postDataJSON() as { events: Array<{ event: string; fields: { n?: number } }> }
    sizes.push(body.events.length)
    for (const event of body.events) if (event.event === 'test.mark') marks.push(event.fields.n as number)
    // The first batch carrying the marks is refused the way the server refuses.
    const refuse = marks.length > 0 && marks.length <= 50
    return route.fulfill({ status: refuse ? 422 : 204, body: '' })
  })
  const enabled = page.waitForRequest(
    (request) => new URL(request.url()).pathname === '/api/client-log/debug' && request.method() === 'POST',
  )
  await page.goto('/')
  await expect(page.getByRole('navigation').first()).toBeVisible()
  // The server's answer arrives once the page is idle; tracing is on from
  // then (its first batch says so).
  await enabled
  await page.evaluate(async () => {
    // The app's own instance of the module (the same URL it was loaded from).
    const url = '/src/clientDebugLog.ts'
    const { debugLog } = await import(/* @vite-ignore */ url)
    for (let n = 0; n < 120; n += 1) debugLog('test.mark', { n })
  })
  await expect.poll(() => marks.length, { timeout: 15_000 }).toBe(120)
  await page.waitForTimeout(2_600)
  expect(Math.max(...sizes)).toBeLessThanOrEqual(50)
  // Each event went out once: the refused batch was dropped, not repeated.
  expect(marks.length).toBe(120)
  expect(new Set(marks).size).toBe(120)
})
