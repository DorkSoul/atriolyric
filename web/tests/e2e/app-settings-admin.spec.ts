// Audio settings, account settings, Admin and the update prompt.

import { expect, test } from './fixtures'
import { readFileSync } from 'node:fs'
import { answerDialog } from './dialogs'
import { installApiMock } from './mockApi'
import { LISTENBRAINZ_TOKEN } from './mock/account'
import { openApp, openTab, player, sawCall, settle, openSettings, chooseOption, selectMenu } from './app-helpers'

test.describe('Atriolyric browser flows', () => {
  test('volume levelling is off by default and saved per account when toggled', async ({ page }) => {
    const mock = await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    const volumeOf = (selector: string) =>
      page.locator(selector).first().evaluate((element) => (element as HTMLMediaElement).volume)

    // Off by default: full volume, nothing routed through Web Audio.
    await expect.poll(() => volumeOf('audio[src*="/api/stream/1"]')).toBe(1)
    await expect(page.locator('html')).not.toHaveAttribute('data-audio-routed', /.+/)

    await openSettings(page, 'Playback')
    const playback = page.locator('#settings-playback')
    const toggle = playback.getByRole('switch', { name: 'Volume levelling' })
    await expect(toggle).not.toBeChecked()
    await toggle.check()
    // Track gain -6 dB + 4 dB (-14 LUFS target) = -2 dB, cut only -> element.volume.
    await expect.poll(() => volumeOf('audio[src*="/api/stream/1"]')).toBeCloseTo(0.794, 2)
    // The standby element's track has no loudness data.
    await expect.poll(() => volumeOf('audio[src*="/api/stream/2"]')).toBe(1)
    await expect.poll(() => mock.calls.filter((c) => c.method === 'PUT' && c.path === '/api/auth/audio-settings').length).toBe(1)
    expect(mock.calls.find((c) => c.method === 'PUT' && c.path === '/api/auth/audio-settings')?.body)
      .toEqual({ volume_leveling: true })
    await toggle.uncheck()
    await expect.poll(() => volumeOf('audio[src*="/api/stream/1"]')).toBe(1)
  })

  test('equaliser saves custom and preset curves and stays off the Web Audio path when flat', async ({ page }) => {
    const mock = await openApp(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    await openSettings(page, 'Playback')
    const playback = page.locator('#settings-playback')
    const puts = () => mock.calls.filter((c) => c.method === 'PUT' && c.path === '/api/auth/audio-settings')
    const preset = selectMenu(playback, 'Equaliser preset')

    await expect(preset).toHaveAccessibleName(/Flat$/)
    await expect(playback.getByRole('slider')).toHaveCount(10)
    await expect(page.locator('html')).not.toHaveAttribute('data-audio-routed', /.+/)

    // Dragging a slider switches to Custom and sends all ten gains (debounced).
    await playback.getByRole('slider', { name: '125 Hz gain' }).fill('6')
    await playback.getByRole('slider', { name: '8 kHz gain' }).fill('-3')
    await expect(preset).toHaveAccessibleName(/Custom$/)
    await expect.poll(() => puts().length).toBe(1)
    expect(puts()[0].body).toEqual({
      eq_preset: 'custom',
      eq_gains: [0, 0, 6, 0, 0, 0, 0, 0, -3, 0],
    })

    await chooseOption(page, preset, 'Bass Boost')
    await expect.poll(() => puts().length).toBe(2)
    expect(puts()[1].body).toEqual({
      eq_preset: 'bass_boost',
      eq_gains: [6, 5, 4, 2, 0, 0, 0, 0, 0, 0],
    })
    await expect(playback.getByRole('slider', { name: '31 Hz gain' })).toHaveValue('6')
    // A non-flat curve routes both music elements through Web Audio, but only
    // once the context runs, and a browser with no audio output device (Firefox
    // in a CI container; no cubeb preference substitutes for one) never starts
    // it: the context stays "suspended" and nothing is routed. Probe that
    // in-page, after the clicks above gave the page user activation, and skip
    // just this assertion where the environment cannot play audio.
    const canRunAudioContext = await page.evaluate(async () => {
      const context = new AudioContext()
      await Promise.race([context.resume().catch(() => {}), new Promise((resolve) => setTimeout(resolve, 3_000))])
      const running = context.state === 'running'
      void context.close().catch(() => {})
      return running
    })
    if (canRunAudioContext) {
      await expect(page.locator('html')).toHaveAttribute('data-audio-routed', '2', { timeout: 3000 })
    } else {
      test.info().annotations.push({ type: 'skipped assertion', description: 'AudioContext cannot run here (no audio device): routed-count not checked' })
    }

    await playback.getByRole('button', { name: 'Reset to Flat' }).click()
    await expect(preset).toHaveAccessibleName(/Flat$/)
    await expect.poll(() => puts().length).toBe(3)
    expect(puts()[2].body).toEqual({ eq_preset: 'flat', eq_gains: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0] })
  })

  test('equaliser follows audio_settings_changed from another device', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    await openSettings(page, 'Playback')
    const playback = page.locator('#settings-playback')
    mock.playbackSocket?.send(JSON.stringify({
      type: 'audio_settings_changed',
      volume_leveling: true,
      eq_preset: 'rock',
      eq_gains: [5, 4, 2, -1, -2, -1, 2, 4, 5, 5],
    }))
    await expect(selectMenu(playback, 'Equaliser preset')).toHaveAccessibleName(/Rock$/)
    await expect(playback.getByRole('switch', { name: 'Volume levelling' })).toBeChecked()
    await expect(playback.getByRole('slider', { name: '16 kHz gain' })).toHaveValue('5')
    // A remote change must not echo back as a save (saves are debounced, so
    // move the clock past the debounce).
    await page.clock.fastForward(5_000)
    await settle(page)
    expect(mock.calls.filter((c) => c.method === 'PUT' && c.path === '/api/auth/audio-settings')).toHaveLength(0)
  })

  test('equaliser fits a 375px phone viewport', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 700 })
    await openApp(page)
    await openSettings(page, 'Playback')
    const slider = page.locator('#settings-playback').getByRole('slider', { name: '16 kHz gain' })
    await slider.scrollIntoViewIfNeeded()
    const box = await slider.boundingBox()
    expect(box!.height).toBeGreaterThan(box!.width * 3)
    expect(box!.x + box!.width).toBeLessThanOrEqual(375)
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375)
  })

  test('configures account, appearance, navigation, discovery, and scrobbling', async ({ page }) => {
    const mock = await openApp(page)

    const openSettingsSection = (label: string) => openSettings(page, label)

    const homeRailBefore = await page.evaluate(() => document.documentElement.style.getPropertyValue('--rail-tile-size'))
    const appearanceCard = await openSettingsSection('Appearance')
    const appearanceHelpButton = appearanceCard.getByRole('button', { name: 'About appearance settings' })
    const appearanceHelpIcon = appearanceHelpButton.locator('svg')
    await expect(appearanceHelpIcon).toBeVisible()
    await expect(appearanceHelpIcon.locator('circle')).toHaveAttribute('r', '9')
    const helpIconBox = await appearanceHelpIcon.boundingBox()
    expect(helpIconBox?.width).toBeGreaterThan(0)
    expect(helpIconBox?.height).toBeGreaterThan(0)
    const helpButtonPadding = await appearanceHelpButton.evaluate((element) =>
      Number.parseFloat(getComputedStyle(element).paddingLeft),
    )
    expect(helpButtonPadding).toBeLessThan(4)
    await appearanceHelpButton.click()
    await expect(page.getByRole('tooltip')).toContainText('accent colour')
    const appearance = appearanceCard
    await appearance.getByRole('button', { name: 'Light', exact: true }).click()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
    await appearance.getByRole('button', { name: 'Use violet as accent colour' }).click()
    await expect(page.locator('html')).toHaveCSS('--accent', '#8b5cf6')
    await appearance.getByRole('button', { name: 'Extra Large', exact: true }).first().click()
    await openTab(page, 'Home')
    // Home's cards are sized by the browse agent's layout; the setting itself
    // shows in the tile-size variable the rails and grids read.
    await expect(page.locator('html')).toHaveCSS('--rail-tile-size', /^\d/)
    expect(await page.evaluate(() => document.documentElement.style.getPropertyValue('--rail-tile-size'))).not.toBe(homeRailBefore)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    const songArt = page.locator('.track-row .appearance-list-art').first()
    await expect(songArt).toBeVisible()
    await expect.poll(async () => (await songArt.boundingBox())?.width ?? 0).toBeGreaterThan(60)

    const playbackCard = await openSettingsSection('Playback')
    await playbackCard.getByRole('button', { name: '320 kbps' }).click()
    const adaptiveQuality = playbackCard.getByRole('switch', {
      name: 'Automatically lower quality on slow networks',
    })
    await adaptiveQuality.check()
    await expect(adaptiveQuality).toBeChecked()
    await adaptiveQuality.uncheck()

    const navigation = await openSettingsSection('Navigation')
    const radioTab = navigation.locator('.tab-order-row').filter({ hasText: 'Radio' })
    await radioTab.getByLabel('Visible').uncheck()
    await expect(page.locator('nav.tabs').getByRole('button', { name: 'Radio', exact: true })).toHaveCount(0)
    const songsTab = navigation.locator('.tab-order-row').filter({ hasText: 'Songs' })
    await songsTab.getByRole('button', { name: 'Reorder Songs' }).focus()
    await page.keyboard.press('ArrowUp')

    const discovery = await openSettingsSection('Discovery')
    // The ListenBrainz privacy switch shares this section: pick the refresh one.
    const enabled = discovery.getByRole('switch', { name: 'Enabled' })
    await enabled.uncheck()
    await expect(selectMenu(discovery, 'Refresh time')).toBeDisabled()
    await enabled.check()
    await chooseOption(page, selectMenu(discovery, 'Refresh time'), '07:00')
    expect(sawCall(mock, '/api/library/discovery/settings', 'PUT')).toBe(true)

    const scrobbling = await openSettingsSection('Scrobbling')
    const listenbrainzRow = scrobbling.locator('[data-provider="listenbrainz"]')
    await listenbrainzRow.getByRole('button', { name: 'Connect ListenBrainz' }).click()
    await expect(scrobbling.getByText('Paste your ListenBrainz user token.')).toBeVisible()
    // Not shaped like a token: refused before anything is sent.
    const sentBefore = mock.calls.filter((call) => call.path === '/api/integrations/listenbrainz').length
    await listenbrainzRow.getByPlaceholder('ListenBrainz user token').fill('not-a-token')
    await listenbrainzRow.getByRole('button', { name: 'Connect ListenBrainz' }).click()
    await expect(scrobbling.getByText(/That is not a ListenBrainz user token/)).toBeVisible()
    expect(mock.calls.filter((call) => call.path === '/api/integrations/listenbrainz')).toHaveLength(sentBefore)
    await listenbrainzRow.getByPlaceholder('ListenBrainz user token').fill('00000000-0000-4000-8000-000000000000')
    await listenbrainzRow.getByRole('button', { name: 'Connect ListenBrainz' }).click()
    await expect(scrobbling.getByText("ListenBrainz didn't accept that token.")).toBeVisible()
    const tokenField = listenbrainzRow.getByPlaceholder('ListenBrainz user token')
    await expect(tokenField).toHaveAttribute('autocomplete', 'new-password')
    await expect(tokenField).toHaveAttribute('spellcheck', 'false')
    await expect(listenbrainzRow.getByRole('button', { name: 'Show token' })).toBeVisible()
    await tokenField.fill(` ${LISTENBRAINZ_TOKEN} `)
    await listenbrainzRow.getByRole('button', { name: 'Connect ListenBrainz' }).click()
    await expect(listenbrainzRow.getByText('connected as rj')).toBeVisible()
    // The swapped-in control takes focus, and the change is announced.
    await expect(listenbrainzRow.getByRole('button', { name: 'Disconnect' })).toBeFocused()
    await expect(listenbrainzRow.getByRole('status')).toContainText('connected as rj')
    expect(mock.calls.filter((call) => call.path === '/api/integrations/listenbrainz').at(-1)?.body).toEqual({
      token: LISTENBRAINZ_TOKEN,
    })
    await listenbrainzRow.getByRole('button', { name: 'Disconnect' }).click()
    await answerDialog(page, 'accept')
    await expect(listenbrainzRow.getByPlaceholder('ListenBrainz user token')).toBeVisible()
    await expect(listenbrainzRow.getByPlaceholder('ListenBrainz user token')).toBeFocused()
    // The earlier error is gone once a later action succeeds.
    await expect(scrobbling.getByText("ListenBrainz didn't accept that token.")).toHaveCount(0)

    const account = await openSettingsSection('Account')
    await account.getByPlaceholder('New username').fill('renamed-user')
    await account.getByPlaceholder('Current password').first().fill(mock.password)
    await account.getByRole('button', { name: 'Change username' }).click()
    await expect(account.getByText('Username changed.', { exact: true })).toBeVisible()
    await account.getByPlaceholder('Current password').nth(1).fill(mock.password)
    await account.getByPlaceholder('New password').fill('a-new-password')
    await account.getByRole('button', { name: 'Change password' }).click()
    await expect(account.getByText(/Password changed/)).toBeVisible()
  })

  test('manages users, roots, access, scans, and metadata from Admin', async ({ page }) => {
    const mock = await openApp(page)
    mock.integrations[0].linked = true
    await page.getByRole('button', { name: 'Admin', exact: true }).click()
    const panel = page.locator('.admin-panel')
    await expect(panel.getByRole('heading', { name: 'Users' })).toBeVisible()
    await expect(panel.getByRole('heading', { name: 'Library roots' })).toBeVisible()
    await panel.getByRole('button', { name: 'About library roots' }).click()
    await expect(page.getByRole('tooltip')).toContainText('Add or remove indexed library paths')
    await panel.getByRole('button', { name: 'About library roots' }).click()

    const users = panel.locator('.admin-section').first()
    await users.getByPlaceholder('Username').fill('newadmin')
    await users.getByPlaceholder('Password (min. 8 characters)').fill('long-enough-password')
    await users.getByLabel('Admin').check()
    await users.getByRole('button', { name: 'Add user' }).click()
    await expect(users).toContainText('newadmin (admin)')

    const musicRoot = panel.locator('.admin-root-row').filter({ hasText: /^Music\b/ })
    await musicRoot.getByRole('button', { name: 'Scan', exact: true }).click()
    await expect(musicRoot).toContainText('Last scan: +2 ~1 -0')
    expect(sawCall(mock, '/api/admin/roots/1/scan', 'POST')).toBe(true)
    await musicRoot.getByRole('button', { name: 'Rescan metadata', exact: true }).click()
    await expect.poll(() => mock.calls.some((c) => c.method === 'POST' && c.url === '/api/admin/roots/1/scan?mode=metadata')).toBe(true)
    await musicRoot.getByRole('button', { name: 'Full rescan', exact: true }).click()
    await expect.poll(() => mock.calls.some((c) => c.method === 'POST' && c.url === '/api/admin/roots/1/scan?mode=full')).toBe(true)
    await musicRoot.getByText('Run a single step instead').click()
    await expect(musicRoot.getByRole('button', { name: 'Fetch missing ListenBrainz tags', exact: true })).toBeEnabled()
    await musicRoot.getByRole('button', { name: 'Fetch missing ListenBrainz tags', exact: true }).click()
    await expect(musicRoot).toContainText('Missing ListenBrainz tags: saved metadata for 2 of 3 tracks')
    const missingCall = mock.calls.find((call) => call.path === '/api/admin/roots/1/enrich')
    expect(missingCall?.body).toEqual({ mode: 'listenbrainz_missing' })

    const helpTip = musicRoot.locator('.admin-help-tip').first()
    const adminTip = page.getByRole('tooltip').filter({ hasText: 'Finds new, changed, or removed media' })
    await helpTip.hover()
    await expect(adminTip).toBeVisible()
    await helpTip.getByRole('button', { name: 'What Scan does' }).click()
    await expect(adminTip).toContainText('Finds new, changed, or removed media')

    const videoRoot = panel.locator('.admin-root-row').filter({ hasText: 'Video vault' })
    const listenerAccess = videoRoot.getByLabel('listener')
    await listenerAccess.check()
    await expect(listenerAccess).toBeChecked()
    expect(sawCall(mock, '/api/admin/roots/2/access/2', 'POST')).toBe(true)

    await expect(users.getByRole('button', { name: 'Delete dork_soul' })).toHaveCount(0)
    const listenerRow = users.locator('li').filter({ hasText: 'listener' })
    await listenerRow.getByRole('button', { name: 'Make admin' }).click()
    await answerDialog(page, 'accept')
    await expect(listenerRow).toContainText('(admin)')
    expect(mock.calls.find((call) => call.path === '/api/admin/users/2' && call.method === 'PATCH')?.body)
      .toEqual({ is_admin: true })
    await listenerRow.getByRole('button', { name: 'Reset password' }).click()
    await listenerRow.getByPlaceholder('New password for listener').fill('fresh-password')
    await listenerRow.getByRole('button', { name: 'Save' }).click()
    await expect(panel.getByRole('status').filter({ hasText: 'Password reset for "listener"' })).toBeVisible()
    expect(mock.calls.find((call) => call.path === '/api/admin/users/2/password')?.body)
      .toEqual({ new_password: 'fresh-password' })

    await users.getByRole('button', { name: 'Delete listener' }).click()
    await answerDialog(page, 'accept')
    await expect(users).not.toContainText('listener')
    expect(sawCall(mock, '/api/admin/users/2', 'DELETE')).toBe(true)

    await panel.getByPlaceholder('Filesystem path').fill('/media/archive')
    await panel.getByPlaceholder('Display name').fill('Archive')
    await panel.getByRole('button', { name: 'Add root', exact: true }).click()
    const archive = panel.locator('.admin-root-row').filter({ hasText: 'Archive' })
    await expect(archive).toBeVisible()
    await archive.getByRole('button', { name: 'Remove path' }).click()
    const removeConfirmation = await answerDialog(page, 'accept')
    await expect(archive).toHaveCount(0)
    expect(removeConfirmation).toContain('indexed tracks and associated database records will be removed')
    expect(removeConfirmation).toContain('music files themselves will not be changed or deleted')
    await expect(panel.getByRole('status')).toContainText('music files themselves were not changed or deleted')
    expect(sawCall(mock, '/api/admin/roots/3', 'DELETE')).toBe(true)
  })

  test('can stop an active metadata fetch from Admin', async ({ page }) => {
    const mock = await openApp(page)
    const now = new Date().toISOString()
    mock.roots[0].active_task = {
      id: 901,
      kind: 'enrich',
      status: 'running',
      total: 100,
      processed: 25,
      added: 0,
      updated: 0,
      removed: 0,
      matched: 10,
      error: null,
      started_at: now,
      updated_at: now,
      completed_at: null,
      items_per_second: 0.5,
      eta_seconds: 150,
    }
    await page.getByRole('button', { name: 'Admin', exact: true }).click()
    const musicRoot = page.locator('.admin-root-row').filter({ hasText: /^Music\b/ })
    await expect(musicRoot.getByRole('button', { name: 'Stop', exact: true })).toBeVisible()
    await musicRoot.getByRole('button', { name: 'Stop', exact: true }).click()
    await expect(musicRoot).toContainText('stopped at 25 of 100 tracks')
    expect(sawCall(mock, '/api/admin/roots/1/tasks/901/stop', 'POST')).toBe(true)
  })
})

test.describe('Update prompt', () => {
  test('offers a restart when the server serves a different version', async ({ page }) => {
    const mock = await installApiMock(page)
    mock.appVersion = '99.0.0'
    await page.goto('/#/home')
    const prompt = page.getByRole('region', { name: 'Update available' })
    await expect(prompt).toContainText('Version 99.0.0 is ready')
    await prompt.getByRole('button', { name: 'Later' }).click()
    await expect(prompt).toBeHidden()

    await page.reload()
    await expect(prompt).toBeVisible()
    const reloaded = page.waitForEvent('load')
    await prompt.getByRole('button', { name: 'Restart' }).click()
    await reloaded
  })

  test('stays hidden when the versions match or the server has none', async ({ page }) => {
    const mock = await installApiMock(page)
    const ownVersion = JSON.parse(readFileSync(new URL('../../package.json', import.meta.url), 'utf8')).version
    mock.appVersion = ownVersion
    await page.goto('/#/home')
    await expect(page.locator('.home-view')).toBeVisible()
    await expect.poll(() => mock.calls.some((call) => call.path === '/api/version')).toBe(true)
    await expect(page.getByRole('region', { name: 'Update available' })).toHaveCount(0)
    // One check serves the update prompt and the debug tracing alike.
    await page.waitForTimeout(500)
    expect(mock.calls.filter((call) => call.path === '/api/version')).toHaveLength(1)
  })
})
