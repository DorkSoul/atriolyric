import { expect, test } from './fixtures'
import { openSettings, settle } from './app-helpers'
import { installApiMock } from './mockApi'
import { installWantListMock } from './wantListMock'

test('a want-list cover asks again after 30 s, three times at most', async ({ page }) => {
  await page.clock.install()
  await installApiMock(page)
  await installWantListMock(page)
  const urls: string[] = []
  let answered = 0
  await page.route(/\/api\/catalog\/art\//, async (route) => {
    urls.push(route.request().url())
    await route.fulfill({ status: 204, headers: { 'Cache-Control': 'private, max-age=30' } })
    answered += 1
  })
  await page.goto('/#/want-list')
  await page.getByRole('searchbox', { name: /Search MusicBrainz/ }).fill('bowie')
  await page.keyboard.press('Enter')
  await expect(page.getByRole('region', { name: 'Artists' })).toContainText('David Bowie')
  await expect.poll(() => urls.length).toBeGreaterThan(0)
  await expect(page.locator('.art-fallback').first()).toBeVisible()
  // Every cover has been asked for, answered and handled before the clock
  // moves on: a retry timer is set when its cover's answer is handled, so one
  // handled late would start its 30 s after the clock had already moved.
  const answeredAndHandled = async (count?: number) => {
    await expect
      .poll(async () => {
        const asked = urls.length
        await settle(page)
        return answered === asked && urls.length === asked && (count === undefined || asked === count)
      })
      .toBe(true)
  }
  await answeredAndHandled()
  const covers = urls.length
  for (const attempt of [1, 2, 3]) {
    await page.clock.runFor(30_000)
    await expect.poll(() => urls.length).toBe(covers * (attempt + 1))
    await answeredAndHandled(covers * (attempt + 1))
    expect(urls.at(-1)).toContain(`retry=${attempt}`)
    await expect(page.locator('.art-fallback').first()).toBeVisible()
  }
  await page.clock.runFor(120_000)
  expect(urls).toHaveLength(covers * 4)
})

test('Settings says when ListenBrainz needs connecting again', async ({ page }) => {
  const mock = await installApiMock(page)
  mock.integrations[0].needs_reconnect = true
  await page.goto('/#/home')
  await openSettings(page, 'Scrobbling')
  await expect(page.getByText('ListenBrainz no longer accepts your token - paste a new one.')).toBeVisible()
})

test('the device picker names an offline active device', async ({ page }) => {
  const mock = await installApiMock(page)
  mock.playback.active_device_id = 2
  mock.playback.devices[0].is_active = false
  mock.playback.devices[1].is_active = true
  mock.playback.devices[1].is_connected = false
  mock.playback.queue_media_item_ids = [3]
  mock.playback.queue_item_ids = [7001]
  mock.playback.queue_context_count = 1
  mock.playback.media_item_id = 3
  await page.goto('/#/home')
  await expect(page.locator('.device-picker-toggle')).toContainText('Living room (offline)')
})
