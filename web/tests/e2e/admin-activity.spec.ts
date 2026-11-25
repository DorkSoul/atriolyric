// Admin -> Server activity: fetched lazily, filtered, paged, downloadable.
import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'
import type { AppMockState } from './mockApi'
import { chooseOption, selectMenu } from './app-helpers'

const activityCalls = (mock: AppMockState) =>
  mock.calls.filter((call) => call.method === 'GET' && call.path === '/api/admin/activity')
const lastQuery = (mock: AppMockState) => new URL(`http://x${activityCalls(mock).at(-1)!.url}`).searchParams

async function openAdmin(page: Parameters<typeof installApiMock>[0]) {
  // A short window, so the card is below the fold.
  await page.setViewportSize({ width: 1280, height: 600 })
  const mock = await installApiMock(page)
  await page.goto('/#/admin')
  await expect(page.getByRole('heading', { name: 'Users', exact: true })).toBeVisible()
  return mock
}

async function showLog(page: Parameters<typeof installApiMock>[0]) {
  await page.locator('.admin-activity').scrollIntoViewIfNeeded()
  const log = page.getByRole('log', { name: 'Server activity log' })
  await expect(log).toBeVisible()
  return log
}

test('the log is not requested with the rest of Admin, only when its card comes near', async ({ page }) => {
  await page.clock.install()
  const mock = await openAdmin(page)
  await expect.poll(() => mock.calls.some((c) => c.path === '/api/admin/users')).toBe(true)
  await page.clock.runFor(500)
  expect(activityCalls(mock)).toHaveLength(0)
  await expect(page.locator('.admin-activity')).toContainText('The log loads when you scroll here.')

  const log = await showLog(page)
  expect(activityCalls(mock)).toHaveLength(1)
  const query = lastQuery(mock)
  expect(query.get('limit')).toBe('100')
  expect(query.get('tz_offset_minutes')).toBe(String(await page.evaluate(() => new Date().getTimezoneOffset())))
  expect(query.get('day')).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  await expect(log.locator('.log-line')).toHaveCount(100)
})

test('an Admin tab left open and idle loads the log by itself', async ({ page }) => {
  await page.clock.install()
  const mock = await openAdmin(page)
  expect(activityCalls(mock)).toHaveLength(0)
  await page.clock.runFor(5_000)
  await expect.poll(() => activityCalls(mock).length).toBe(1)
})

test('chips, the event type and the day narrow the request', async ({ page }) => {
  const mock = await openAdmin(page)
  await showLog(page)
  const card = page.locator('.admin-activity')

  await card.getByRole('button', { name: 'Errors', exact: true }).click()
  await expect.poll(() => lastQuery(mock).get('level')).toBe('error')
  await expect(card.getByRole('log').locator('.log-line')).not.toHaveCount(0)
  await expect(card.locator('.log-line:not(.log-error)')).toHaveCount(0)

  // Problems: warnings and errors together.
  await card.getByRole('button', { name: 'Problems', exact: true }).click()
  await expect.poll(() => lastQuery(mock).get('level')).toBe('problems')
  await expect(card.getByRole('log').locator('.log-line')).not.toHaveCount(0)
  await expect(card.locator('.log-line:not(.log-error):not(.log-warning)')).toHaveCount(0)
  await expect(card.locator('.log-line.log-warning')).not.toHaveCount(0)

  await card.getByRole('button', { name: 'Sign-ins', exact: true }).click()
  await expect.poll(() => lastQuery(mock).get('kind')).toBe('auth.login')
  expect(lastQuery(mock).get('level')).toBeNull()

  // A group of several kinds shows them all (the prefix), until one is picked.
  await card.getByRole('button', { name: 'Tasks', exact: true }).click()
  await expect.poll(() => lastQuery(mock).get('kind')).toBe('task.')
  await expect(card.locator('.log-kind', { hasText: 'task.start' })).not.toHaveCount(0)
  await expect(card.locator('.log-kind', { hasText: 'task.finish' })).not.toHaveCount(0)
  await chooseOption(page, selectMenu(card, 'Event type'), 'task.start')
  await expect.poll(() => lastQuery(mock).get('kind')).toBe('task.start')
  await chooseOption(page, selectMenu(card, 'Event type'), 'All tasks')
  await expect.poll(() => lastQuery(mock).get('kind')).toBe('task.')

  await card.getByRole('button', { name: 'All', exact: true }).click()
  await expect.poll(() => lastQuery(mock).get('kind')).toBeNull()
  await chooseOption(page, selectMenu(card, 'Day'), 'Yesterday')
  const yesterday = await page.evaluate(() => {
    const d = new Date()
    d.setDate(d.getDate() - 1)
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  })
  await expect.poll(() => lastQuery(mock).get('day')).toBe(yesterday)
  await expect(card).toContainText('No activity matches these filters.')
  await chooseOption(page, selectMenu(card, 'Day'), 'All days')
  await expect.poll(() => lastQuery(mock).get('day')).toBeNull()
  await expect(card.getByRole('log').locator('.log-line')).toHaveCount(100)
})

test('Load older pages by next_before_id', async ({ page }) => {
  const mock = await openAdmin(page)
  const log = await showLog(page)
  await expect(log.locator('.log-line')).toHaveCount(100)
  await page.getByRole('button', { name: 'Load older' }).click()
  await expect(log.locator('.log-line')).toHaveCount(130)
  // The 100th newest of 130 events has id 31.
  expect(lastQuery(mock).get('before_id')).toBe('31')
  await expect(page.getByRole('button', { name: 'Load older' })).toHaveCount(0)
})

test('Download log asks for the same filters', async ({ page }) => {
  const mock = await openAdmin(page)
  await showLog(page)
  const card = page.locator('.admin-activity')
  await card.getByRole('button', { name: 'Errors', exact: true }).click()
  await expect.poll(() => lastQuery(mock).get('level')).toBe('error')
  const link = card.getByRole('link', { name: 'Download log' })
  await expect(link).toHaveAttribute('href', /level=error/)
  await expect(link).toHaveAttribute('href', /tz_offset_minutes=/)
  const download = page.waitForEvent('download')
  await link.click()
  const url = new URL((await download).url())
  expect(url.pathname).toBe('/api/admin/activity/download')
  expect(url.searchParams.get('level')).toBe('error')
  expect(url.searchParams.get('tz_offset_minutes')).not.toBeNull()
})

test('a failed load shows an error with Retry', async ({ page }) => {
  const mock = await openAdmin(page)
  mock.activityFailures = 1
  await page.locator('.admin-activity').scrollIntoViewIfNeeded()
  const alert = page.getByRole('alert').filter({ hasText: 'Could not load the activity log.' })
  await expect(alert).toBeVisible()
  await alert.getByRole('button', { name: 'Retry' }).click()
  await expect(page.getByRole('log', { name: 'Server activity log' })).toBeVisible()
  await expect(alert).toHaveCount(0)
})

test('the log shows an empty state', async ({ page }) => {
  const mock = await openAdmin(page)
  mock.activityEvents = []
  await page.locator('.admin-activity').scrollIntoViewIfNeeded()
  await expect(page.locator('.admin-activity')).toContainText('No activity matches these filters.')
})

test('someone who is not an admin never asks for the log', async ({ page }) => {
  const mock = await installApiMock(page)
  mock.user.is_admin = false
  await page.goto('/#/admin')
  await expect(page.getByRole('alert').filter({ hasText: 'Admin access required.' })).toBeVisible()
  await page.waitForLoadState('networkidle')
  expect(mock.calls.filter((c) => c.path.startsWith('/api/admin/activity'))).toEqual([])
})

test('the log refreshes every half minute while on screen, and not while the tab is hidden', async ({ page }) => {
  await page.clock.install()
  const mock = await openAdmin(page)
  const log = await showLog(page)
  await expect(log.locator('.log-line')).toHaveCount(100)
  const before = activityCalls(mock).length
  mock.activityEvents.push({
    id: 131, created_at: new Date().toISOString().replace(/\.\d+Z$/, ''), kind: 'auth.login', level: 'info',
    actor: 'admin', summary: 'Brand new event', root_id: null, context: null, request_id: null,
  })
  await page.clock.runFor(31_000)
  await expect(log).toContainText('Brand new event')
  expect(activityCalls(mock).length).toBeGreaterThan(before)

  await page.getByRole('button', { name: 'Songs', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
  const hidden = activityCalls(mock).length
  await page.clock.runFor(95_000)
  expect(activityCalls(mock).length).toBe(hidden)
})
