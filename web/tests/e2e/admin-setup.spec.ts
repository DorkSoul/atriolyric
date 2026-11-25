import { expect, test } from './fixtures'
import { answerDialog } from './dialogs'
import { installApiMock } from './mockApi'

test('scans are grouped: Library scan, Setup scan, and single steps tucked away', async ({ page }) => {
  await installApiMock(page)
  await page.goto('/#/admin')
  const root = page.locator('.admin-root-row').first()
  await expect(root.getByRole('button', { name: 'Scan', exact: true })).toBeVisible()
  const headings = await root.getByRole('heading', { level: 3 }).allTextContents()
  expect(headings).toEqual(['Library scan', 'Setup scan'])
  const buttons = (group: string) =>
    root.locator('.admin-tks').nth(group === 'library' ? 0 : 1).locator('button:not(.admin-help-button)').allTextContents()
  expect(await buttons('library')).toEqual(['Scan', 'Rescan metadata', 'Full rescan'])
  expect(await buttons('setup')).toEqual(['Set up new entries', 'Full setup scan'])

  const steps = root.locator('details.admin-steps')
  await expect(steps).not.toHaveAttribute('open', '')
  await expect(root.getByRole('button', { name: 'Fetch MBIDs', exact: true })).toBeHidden()
  await steps.getByText('Run a single step instead').click()
  expect(await steps.locator('button:not(.admin-help-button)').allTextContents()).toEqual([
    'Fetch MBIDs',
    'Fetch missing MusicBrainz data',
    'Fetch missing ListenBrainz tags',
    'Fetch all ListenBrainz tags',
    'Artist info',
  ])
  await expect(steps).toContainText('Both setup scans already do all of these.')
  await expect(steps).not.toContainText('Last.fm')
})

test('every scan and fetch button has its own info control that opens its text', async ({ page }) => {
  await installApiMock(page)
  await page.goto('/#/admin')
  const root = page.locator('.admin-root-row').first()
  await root.locator('details.admin-steps').getByText('Run a single step instead').click()
  const names = [
    'Scan', 'Rescan metadata', 'Full rescan', 'Set up new entries', 'Full setup scan',
    'Fetch MBIDs', 'Fetch missing MusicBrainz data', 'Fetch missing ListenBrainz tags', 'Fetch all ListenBrainz tags', 'Artist info',
  ]
  for (const name of names) {
    const tip = root.getByRole('button', { name: `What ${name} does`, exact: true })
    await expect(tip, name).toBeVisible()
    const box = await tip.boundingBox()
    expect(box!.width, `${name} width`).toBeGreaterThanOrEqual(23.5)
    expect(box!.height, `${name} height`).toBeGreaterThanOrEqual(23.5)
    // A tap opens it, as on a phone, and says something.
    await tip.click()
    await expect(page.getByRole('tooltip'), name).toHaveText(/\S{3,}/)
    await page.keyboard.press('Escape')
    await page.mouse.move(0, 0)
    await expect(page.getByRole('tooltip')).toHaveCount(0)
  }
  await root.getByRole('button', { name: 'What Full setup scan does', exact: true }).click()
  await expect(page.getByRole('tooltip')).toContainText('Rescans every file, then refetches everything')
})

test('the single steps call the same endpoints as before', async ({ page }) => {
  const mock = await installApiMock(page)
  mock.integrations[0].linked = true
  await page.goto('/#/admin')
  const root = page.locator('.admin-root-row').first()
  const steps = root.locator('details.admin-steps')
  await steps.getByText('Run a single step instead').click()
  const modes: Array<[string, string]> = [
    ['Fetch MBIDs', 'mbids'],
    ['Fetch missing MusicBrainz data', 'musicbrainz_missing'],
    ['Fetch missing ListenBrainz tags', 'listenbrainz_missing'],
    ['Fetch all ListenBrainz tags', 'listenbrainz_all'],
    ['Artist info', 'artist_info'],
  ]
  for (const [label, mode] of modes) {
    await steps.getByRole('button', { name: label, exact: true }).click()
    await expect.poll(() =>
      mock.calls.some((c) => c.method === 'POST' && c.path === '/api/admin/roots/1/enrich' && c.body.mode === mode),
    ).toBe(true)
  }
})

test('the ListenBrainz tag steps need no account or key', async ({ page }) => {
  await installApiMock(page)
  await page.goto('/#/admin')
  const steps = page.locator('.admin-root-row').first().locator('details.admin-steps')
  await steps.getByText('Run a single step instead').click()
  await expect(steps.getByRole('button', { name: 'Fetch missing ListenBrainz tags', exact: true })).toBeEnabled()
  await expect(steps.getByRole('button', { name: 'Fetch all ListenBrainz tags', exact: true })).toBeEnabled()
  await expect(steps.getByRole('button', { name: 'Artist info', exact: true })).toBeEnabled()
})

test('the button that owns a running task says Stop (and Stopping…), the others wait', async ({ page }) => {
  const mock = await installApiMock(page)
  const now = new Date().toISOString()
  mock.roots[0].active_task = {
    id: 905, kind: 'full_setup_new', status: 'running', stage: 'Scan', total: 100, processed: 10, added: 0,
    updated: 0, removed: 0, matched: 0, error: null, started_at: now, updated_at: now, completed_at: null,
    items_per_second: 1, eta_seconds: 90,
  } as never
  await page.goto('/#/admin')
  const root = page.locator('.admin-root-row').first()
  await expect(root.getByRole('button', { name: 'Stop', exact: true })).toBeVisible()
  await expect(root.getByRole('button', { name: 'Set up new entries', exact: true })).toHaveCount(0)
  await expect(root.getByRole('button', { name: 'Full setup scan', exact: true })).toBeDisabled()
  await expect(root.getByRole('button', { name: 'Scan', exact: true })).toBeDisabled()
  await expect(root.locator('.admin-task-progress')).toContainText('Setup for new entries')
  await root.getByRole('button', { name: 'Stop', exact: true }).click()
  expect(mock.calls.some((c) => c.method === 'POST' && c.path === '/api/admin/roots/1/tasks/905/stop')).toBe(true)
})

test('Full setup scan confirms first', async ({ page }) => {
  const mock = await installApiMock(page)
  await page.goto('/#/admin')
  const root = page.locator('.admin-root-row').first()
  await root.getByRole('button', { name: 'Full setup scan', exact: true }).click()
  await answerDialog(page, 'dismiss')
  expect(mock.calls.some((c) => c.path.endsWith('/full-setup'))).toBe(false)

  await root.getByRole('button', { name: 'Full setup scan', exact: true }).click()
  await answerDialog(page, 'accept')
  await expect(root).toContainText('Full setup scan started.')
  expect(mock.calls.some((c) => c.path.endsWith('/full-setup') && c.method === 'POST')).toBe(true)
})

test('Set up new entries starts the new-only run without a confirm', async ({ page }) => {
  const mock = await installApiMock(page)
  await page.goto('/#/admin')
  const root = page.locator('.admin-root-row').first()
  await root.getByRole('button', { name: 'Set up new entries', exact: true }).click()
  await expect(root).toContainText('Setup for new entries started.')
  expect(mock.calls.some((c) => c.path.endsWith('/full-setup-new') && c.method === 'POST')).toBe(true)
  expect(mock.calls.some((c) => c.path.endsWith('/full-setup'))).toBe(false)
})

test('Admin shows no Last.fm advice or server key any more', async ({ page }) => {
  const mock = await installApiMock(page)
  await page.goto('/#/admin')
  await expect(page.getByRole('heading', { name: 'Users', exact: true })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Last.fm advice' })).toHaveCount(0)
  await expect(page.getByRole('region', { name: 'Last.fm server key' })).toHaveCount(0)
  expect(mock.calls.some((c) => c.path.includes('lastfm'))).toBe(false)
})

test('a scan that held back missing tracks warns and removes them only after a confirm', async ({ page }) => {
  const mock = await installApiMock(page)
  const now = new Date().toISOString()
  mock.roots[0].last_task = {
    id: 90,
    kind: 'scan',
    status: 'completed',
    total: 0,
    processed: 0,
    added: 0,
    updated: 0,
    removed: 0,
    matched: 0,
    error: null,
    started_at: now,
    updated_at: now,
    completed_at: now,
    missing_held: 42,
    note: '42 tracks look missing - the folder may be unmounted or unreadable. Nothing was removed.',
    items_per_second: null,
    eta_seconds: null,
  }
  await page.goto('/#/admin')
  const root = page.locator('.admin-root-row').first()
  await expect(root.getByRole('alert')).toContainText('42 tracks look missing')
  const remove = root.getByRole('button', { name: 'Remove missing tracks' })

  await remove.click()
  await answerDialog(page, 'dismiss')
  expect(mock.calls.some((c) => c.url.includes('mode=confirm_removals'))).toBe(false)

  await remove.click()
  expect(await answerDialog(page, 'accept')).toContain('Remove 42 missing tracks')
  await expect(root).toContainText('missing tracks will be removed')
  expect(mock.calls.some((c) => c.method === 'POST' && c.url.endsWith('/scan?mode=confirm_removals'))).toBe(true)
  await expect(root.getByRole('button', { name: 'Remove missing tracks' })).toHaveCount(0)
})
