import { readFileSync } from 'node:fs'
import { openSettings } from './app-helpers'
import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'

// AGPL section 13: a network user is offered the source. Settings -> About
// shows the running version, the licence and the repository link.

const { version } = JSON.parse(readFileSync(new URL('../../package.json', import.meta.url), 'utf8')) as { version: string }

test('Settings About shows the version, the licence and a source link', async ({ page }) => {
  await installApiMock(page)
  await page.goto('/#/home')
  await expect(page.locator('.home-view')).toBeVisible()
  await openSettings(page, 'About')

  const section = page.locator('#settings-about')
  await expect(section).toContainText(`Version ${version}`)
  await expect(section).toContainText('AGPL-3.0-or-later')
  const link = section.getByRole('link', { name: 'Source code' })
  await expect(link).toHaveAttribute('href', /^https:\/\/github\.com\/DorkSoul\/atriolyric$/)
  await expect(link).toHaveAttribute('target', '_blank')
  await expect(link).toHaveAttribute('rel', /noreferrer/)
})
