import { type Page } from 'playwright/test'
import { expect, test } from './fixtures'
import { installApiMock, type AppMockState } from './mockApi'

// The Admin panel's forms run as form actions: a refused submit says why and
// keeps everything that was typed, and a second press or Enter while one is
// out sends nothing more.

async function openAdmin(page: Page): Promise<AppMockState> {
  const mock = await installApiMock(page)
  await page.goto('/#/admin')
  await expect(page.getByRole('heading', { name: 'Users', exact: true })).toBeVisible()
  return mock
}

/** Refuses `method` requests to `path` with `detail`; returns how many came. */
async function refuse(page: Page, method: string, path: RegExp, detail: string, status = 400) {
  const seen = { count: 0 }
  await page.route(path, async (route) => {
    if (route.request().method() !== method) return route.fallback()
    seen.count += 1
    await route.fulfill({ status, json: { detail } })
  })
  return seen
}

const usersSection = (page: Page) => page.locator('.admin-panel .admin-section').first()
const rootsSection = (page: Page) => page.locator('.admin-panel .admin-section').nth(1)

test('a refused new user keeps the name, password and admin box', async ({ page }) => {
  await openAdmin(page)
  await refuse(page, 'POST', /\/api\/admin\/users$/, 'That username is taken.', 409)
  const users = usersSection(page)
  await users.getByPlaceholder('Username').fill('listener')
  await users.getByPlaceholder('Password (min. 8 characters)').fill('long-enough-password')
  await users.getByLabel('Admin').check()
  await users.getByRole('button', { name: 'Add user' }).click()
  await expect(users.getByRole('alert')).toHaveText('That username is taken.')
  await expect(users.getByPlaceholder('Username')).toHaveValue('listener')
  await expect(users.getByPlaceholder('Password (min. 8 characters)')).toHaveValue('long-enough-password')
  await expect(users.getByLabel('Admin')).toBeChecked()
  await expect(users.getByRole('button', { name: 'Add user' })).toBeEnabled()
})

test('a new user is checked before sending, and the message clears on the next try', async ({ page }) => {
  const mock = await openAdmin(page)
  const users = usersSection(page)
  await users.getByPlaceholder('Password (min. 8 characters)').fill('long-enough-password')
  await users.getByRole('button', { name: 'Add user' }).click()
  await expect(users.getByRole('alert')).toHaveText('Username cannot be blank.')
  await expect(users.getByPlaceholder('Password (min. 8 characters)')).toHaveValue('long-enough-password')
  await users.getByPlaceholder('Username').fill('newbie')
  await users.getByPlaceholder('Password (min. 8 characters)').fill('short')
  // Enter in a field submits the form.
  await users.getByPlaceholder('Password (min. 8 characters)').press('Enter')
  await expect(users.getByRole('alert')).toHaveText('Password must be at least 8 characters.')
  expect(mock.calls.filter((call) => call.method === 'POST' && call.path === '/api/admin/users')).toEqual([])
  await users.getByPlaceholder('Password (min. 8 characters)').fill('long-enough-password')
  await users.getByLabel('Admin').check()
  await users.getByRole('button', { name: 'Add user' }).click()
  await expect(users).toContainText('newbie (admin)')
  await expect(users.getByRole('alert')).toHaveCount(0)
  // A success clears the form, the box included.
  await expect(users.getByPlaceholder('Username')).toHaveValue('')
  await expect(users.getByPlaceholder('Password (min. 8 characters)')).toHaveValue('')
  await expect(users.getByLabel('Admin')).not.toBeChecked()
})

test('adding a user sends one request however often it is submitted', async ({ page }) => {
  const mock = await openAdmin(page)
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route(/\/api\/admin\/users$/, async (route) => {
    if (route.request().method() === 'POST') await gate
    await route.fallback()
  })
  const users = usersSection(page)
  await users.getByPlaceholder('Username').fill('once')
  await users.getByPlaceholder('Password (min. 8 characters)').fill('long-enough-password')
  await users.getByRole('button', { name: 'Add user' }).click()
  await expect(users.getByRole('button', { name: 'Adding…' })).toBeDisabled()
  await users.getByPlaceholder('Username').press('Enter')
  release()
  await expect(users).toContainText('once')
  await expect(users.getByRole('button', { name: 'Add user' })).toBeEnabled()
  expect(mock.calls.filter((call) => call.method === 'POST' && call.path === '/api/admin/users')).toHaveLength(1)
})

test('a refused password reset keeps the new password and the form open', async ({ page }) => {
  await openAdmin(page)
  await refuse(page, 'POST', /\/api\/admin\/users\/2\/password$/, 'That password is too common.')
  const row = usersSection(page).locator('li').filter({ hasText: 'listener' })
  await row.getByRole('button', { name: 'Reset password' }).click()
  await row.getByPlaceholder('New password for listener').fill('password123')
  await row.getByRole('button', { name: 'Save' }).click()
  await expect(usersSection(page).getByRole('alert')).toHaveText('That password is too common.')
  await expect(row.getByPlaceholder('New password for listener')).toHaveValue('password123')
  await expect(row.getByRole('button', { name: 'Save' })).toBeEnabled()
})

test('a refused new library root keeps its path, name and shared box', async ({ page }) => {
  await openAdmin(page)
  await refuse(page, 'POST', /\/api\/admin\/roots$/, 'That folder does not exist.')
  const roots = rootsSection(page)
  const addRow = roots.locator('form.admin-create-row').last()
  await addRow.getByPlaceholder('Filesystem path').fill('/media/nowhere')
  await addRow.getByPlaceholder('Display name').fill('Nowhere')
  await addRow.getByLabel('Shared (visible to everyone)').uncheck()
  await addRow.getByRole('button', { name: 'Add root' }).click()
  await expect(roots.getByRole('alert')).toHaveText('That folder does not exist.')
  await expect(addRow.getByPlaceholder('Filesystem path')).toHaveValue('/media/nowhere')
  await expect(addRow.getByPlaceholder('Display name')).toHaveValue('Nowhere')
  await expect(addRow.getByLabel('Shared (visible to everyone)')).not.toBeChecked()
})

test('a refused library root edit keeps what was changed', async ({ page }) => {
  await openAdmin(page)
  await refuse(page, 'PATCH', /\/api\/admin\/roots\/1$/, 'That folder does not exist.')
  const roots = rootsSection(page)
  const music = roots.locator('.admin-root-row').filter({ hasText: /^Music\b/ })
  await music.getByRole('button', { name: 'Edit' }).click()
  const form = music.locator('form.admin-root-edit')
  await form.getByPlaceholder('Filesystem path').fill('/media/moved')
  await form.getByPlaceholder('Display name').fill('Moved music')
  await form.getByLabel('Shared (visible to everyone)').uncheck()
  await form.getByRole('button', { name: 'Save' }).click()
  await expect(roots.getByRole('alert')).toHaveText('That folder does not exist.')
  await expect(form.getByPlaceholder('Filesystem path')).toHaveValue('/media/moved')
  await expect(form.getByPlaceholder('Display name')).toHaveValue('Moved music')
  await expect(form.getByLabel('Shared (visible to everyone)')).not.toBeChecked()
})

test('refused artist splitting rules stay as typed', async ({ page }) => {
  await openAdmin(page)
  await refuse(page, 'PUT', /\/api\/admin\/artist-split-rules$/, 'Something broke on the server.', 500)
  const section = page.getByRole('region', { name: 'Artist splitting' })
  const separators = section.getByLabel('Separators')
  await expect(separators).toHaveValue('";", "/"')
  await separators.fill('";", " & "')
  await section.getByLabel('Never split (one name per line)').fill('AC/DC\nSimon & Garfunkel')
  await section.getByRole('button', { name: 'Save and re-link' }).click()
  await expect(section.getByRole('alert')).toHaveText('Something broke on the server.')
  await expect(separators).toHaveValue('";", " & "')
  await expect(section.getByLabel('Never split (one name per line)')).toHaveValue('AC/DC\nSimon & Garfunkel')
})

for (const width of [390, 320]) {
  test(`at ${width}px the user and library root buttons are whole and the add forms use full lines`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 })
    await openAdmin(page)
    const users = usersSection(page)
    const row = users.locator('.admin-user-row').filter({ hasText: 'listener' })
    const buttons = row.locator('.admin-user-actions button')
    await expect(buttons).toHaveCount(3)
    const name = (await row.locator('.admin-user-summary > span:first-child').boundingBox())!
    for (let i = 0; i < 3; i++) {
      const box = (await buttons.nth(i).boundingBox())!
      expect(box.x).toBeGreaterThanOrEqual(0)
      expect(box.x + box.width).toBeLessThanOrEqual(width)
      expect(box.height).toBeGreaterThanOrEqual(24)
      expect(box.y).toBeGreaterThanOrEqual(name.y + name.height - 1)
      // The label is whole, not clipped.
      expect(await buttons.nth(i).evaluate((el) => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(0)
    }
    const username = (await users.getByPlaceholder('Username').boundingBox())!
    const password = (await users.getByPlaceholder('Password (min. 8 characters)').boundingBox())!
    expect(username.width).toBeGreaterThan(width * 0.7)
    expect(password.y).toBeGreaterThanOrEqual(username.y + username.height)
    const add = (await users.getByRole('button', { name: 'Add user' }).boundingBox())!
    expect(add.y).toBeGreaterThanOrEqual(password.y + password.height)

    const roots = rootsSection(page)
    for (const name of ['Edit', 'Remove path']) {
      const box = (await roots.getByRole('button', { name, exact: true }).first().boundingBox())!
      expect(box.x + box.width).toBeLessThanOrEqual(width)
      expect(box.height).toBeGreaterThanOrEqual(24)
    }
    const rootForm = roots.locator('form.admin-create-row').last()
    const path = (await rootForm.getByPlaceholder('Filesystem path').boundingBox())!
    const display = (await rootForm.getByPlaceholder('Display name').boundingBox())!
    expect(path.width).toBeGreaterThan(width * 0.7)
    expect(display.y).toBeGreaterThanOrEqual(path.y + path.height)
  })
}

test('the new user\'s name is not capitalised or corrected by a phone keyboard, and is sent trimmed', async ({ page }) => {
  const mock = await openAdmin(page)
  const form = usersSection(page).locator('form.admin-create-row')
  const username = form.getByLabel('Username')
  await expect(username).toHaveAttribute('autocapitalize', 'none')
  await expect(username).toHaveAttribute('autocorrect', 'off')
  await expect(username).toHaveAttribute('spellcheck', 'false')
  await username.fill(' newbie ')
  await form.getByPlaceholder('Password (min. 8 characters)').fill('long enough password')
  await form.getByRole('button', { name: 'Add user' }).click()
  await expect.poll(() => mock.calls.find((call) => call.path === '/api/admin/users' && call.method === 'POST')?.body.username).toBe('newbie')
})
