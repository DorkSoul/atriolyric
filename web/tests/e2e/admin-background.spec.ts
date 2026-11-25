import { type Page } from 'playwright/test'
import { expect, test } from './fixtures'
import { installApiMock, type AppMockState } from './mockApi'

// Admin → Background work: how far volume levelling (the loudness pass) and
// sound analysis have got, from GET /api/admin/background-status.

type Pass = AppMockState['backgroundStatus']['loudness']

const TOTAL = 135_402

function pass(values: Partial<Pass>): Pass {
  return {
    state: 'idle',
    total: TOTAL,
    done: TOTAL,
    failed: 0,
    pending: 0,
    run_checked: null,
    run_stored: null,
    rate_per_minute: null,
    ...values,
  }
}

async function openAdmin(page: Page, loudness: Partial<Pass>, analysis: Partial<Pass>) {
  const mock = await installApiMock(page)
  mock.backgroundStatus = { loudness: pass(loudness), audio_analysis: pass(analysis) }
  await page.goto('/#/admin')
  const section = page.getByRole('region', { name: 'Background work' })
  await expect(section).toBeVisible()
  return { mock, section }
}

const statusCalls = (mock: AppMockState) => mock.calls.filter((call) => call.path === '/api/admin/background-status').length

// Thousands separators follow the browser's locale.
const n = (digits: string) => new RegExp(digits.replace(/,/g, '.?'))

test('a pass measuring now shows its progress, failures and a rough time left; analysis waits for it', async ({ page }) => {
  const { section } = await openAdmin(
    page,
    { state: 'running', done: 14_210, failed: 318, pending: 120_874, run_checked: 3000, run_stored: 2987, rate_per_minute: 900 },
    { state: 'waiting', done: 2_000, pending: TOTAL - 2_000, rate_per_minute: 500 },
  )
  const levelling = section.getByRole('listitem').filter({ hasText: /^Volume levelling/ })
  await expect(levelling).toContainText('Measuring now')
  await expect(levelling).toContainText(n('14,210 of 135,402 tracks'))
  await expect(levelling).toContainText(n('120,874 to go'))
  // 120,874 tracks at 900 a minute: 2.2 hours.
  await expect(levelling).toContainText('about 2 hours left')
  await expect(levelling).toContainText('318 could not be measured')
  const bar = section.getByRole('progressbar', { name: 'Volume levelling' })
  await expect(bar).toHaveAttribute('value', '14210')
  await expect(bar).toHaveAttribute('max', String(TOTAL))
  await expect(bar).toHaveAttribute('aria-valuetext', n('14,210 of 135,402 tracks'))

  const analysis = section.getByRole('listitem').filter({ hasText: /^Sound analysis/ })
  await expect(analysis).toContainText('Waiting for volume levelling to finish')
  await expect(analysis).toContainText(n('2,000 of 135,402 tracks'))
  await expect(analysis).not.toContainText('left')
  await expect(analysis).not.toContainText('could not be')
  await expect(section.getByRole('progressbar', { name: 'Sound analysis' })).toHaveAttribute('value', '2000')
})

test('no time left until the rate is known; under an hour says so', async ({ page }) => {
  const { section } = await openAdmin(
    page,
    { state: 'running', done: 100, pending: TOTAL - 100, rate_per_minute: null },
    { state: 'running', done: TOTAL - 1_000, failed: 12, pending: 988, rate_per_minute: 400 },
  )
  const levelling = section.getByRole('listitem').filter({ hasText: /^Volume levelling/ })
  await expect(levelling).toContainText('Measuring now')
  await expect(levelling).not.toContainText('left')
  const analysis = section.getByRole('listitem').filter({ hasText: /^Sound analysis/ })
  await expect(analysis).toContainText('Measuring now')
  await expect(analysis).toContainText('less than an hour left')
  await expect(analysis).toContainText('12 could not be analysed')
})

test('up to date, and analysis turned off by the server setting', async ({ page }) => {
  const { section } = await openAdmin(page, {}, { state: 'disabled', done: 40_000, pending: TOTAL - 40_000 })
  const levelling = section.getByRole('listitem').filter({ hasText: /^Volume levelling/ })
  await expect(levelling).toContainText('Up to date')
  await expect(levelling).toContainText(n('135,402 of 135,402 tracks'))
  await expect(levelling).not.toContainText('to go')
  await expect(levelling).not.toContainText('could not be')
  const analysis = section.getByRole('listitem').filter({ hasText: /^Sound analysis/ })
  await expect(analysis).toContainText('Turned off')
  await expect(analysis).toContainText(n('40,000 of 135,402 tracks'))
  await expect(analysis).not.toContainText('to go')
})

test('someone who is not an admin gets no Background work and no status request', async ({ page }) => {
  const mock = await installApiMock(page)
  mock.user.is_admin = false
  await page.goto('/#/admin')
  await expect(page.getByRole('alert').filter({ hasText: 'Admin access required.' })).toBeVisible()
  await page.waitForLoadState('networkidle')
  await expect(page.getByRole('region', { name: 'Background work' })).toHaveCount(0)
  expect(statusCalls(mock)).toBe(0)
})

test('polls every half minute while Admin is shown, and not while another tab is', async ({ page }) => {
  await page.clock.install()
  const { mock, section } = await openAdmin(page, {}, {})
  // The first load (twice in a development build, whose StrictMode runs
  // effects twice) is done once the page is idle.
  await page.waitForLoadState('networkidle')
  const first = statusCalls(mock)
  expect(first).toBeGreaterThan(0)
  await page.clock.runFor(30_000)
  await expect.poll(() => statusCalls(mock)).toBe(first + 1)
  // A newer state shows on the next poll.
  mock.backgroundStatus.loudness = pass({ state: 'running', done: 10, pending: TOTAL - 10 })
  await page.clock.runFor(30_000)
  await expect(section.getByRole('listitem').filter({ hasText: /^Volume levelling/ })).toContainText('Measuring now')
  expect(statusCalls(mock)).toBe(first + 2)

  await page.getByRole('button', { name: 'Songs', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
  await page.clock.runFor(120_000)
  expect(statusCalls(mock)).toBe(first + 2)

  // Back on Admin it asks again at once.
  await page.getByRole('button', { name: 'Admin', exact: true }).click()
  await expect.poll(() => statusCalls(mock)).toBeGreaterThan(first + 2)
})
