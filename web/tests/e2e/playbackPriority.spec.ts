import { expect, test } from './fixtures'
import type { Page } from 'playwright/test'

// There is no unit-test runner in web/, so the module is loaded into a bare
// page through Vite. Each test gets its own copy (a unique query string), so
// nothing leaks between tests, and a page clock makes the 4 s cap exact.
type Priority = typeof import('../../src/playbackPriority')
type PriorityWindow = Window & { __priority?: Priority; __changes?: boolean[] }

async function loadModule(page: Page) {
  await page.clock.install()
  await page.route('**/priority-blank', (route) =>
    route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>blank</title>' }),
  )
  await page.goto('/priority-blank')
  await page.evaluate(async (stamp) => {
    const w = window as PriorityWindow
    w.__priority = await import(/* @vite-ignore */ `/src/playbackPriority.ts?isolated=${stamp}`)
    w.__changes = []
    w.__priority!.onPlaybackStartingChange(() => w.__changes!.push(w.__priority!.isPlaybackStarting()))
  }, String(Math.random()).slice(2))
}

const starting = (page: Page) => page.evaluate(() => (window as PriorityWindow).__priority!.isPlaybackStarting())
const changes = (page: Page) => page.evaluate(() => (window as PriorityWindow).__changes!)

test('playback priority: set and clear notify once per change', async ({ page }) => {
  await loadModule(page)
  expect(await starting(page)).toBe(false)
  await page.evaluate(() => {
    const { setPlaybackStarting } = (window as PriorityWindow).__priority!
    setPlaybackStarting(true)
    setPlaybackStarting(true)
    setPlaybackStarting(false)
    setPlaybackStarting(false)
  })
  expect(await starting(page)).toBe(false)
  expect(await changes(page)).toEqual([true, false])
})

test('playback priority: the hold gives up after 4 seconds, and a new hold restarts the count', async ({ page }) => {
  await loadModule(page)
  await page.evaluate(() => (window as PriorityWindow).__priority!.setPlaybackStarting(true))
  await page.clock.runFor(3_900)
  expect(await starting(page)).toBe(true)
  // Another `loadstart`/`waiting` while still holding restarts the cap.
  await page.evaluate(() => (window as PriorityWindow).__priority!.setPlaybackStarting(true))
  await page.clock.runFor(3_900)
  expect(await starting(page)).toBe(true)
  await page.clock.runFor(200)
  expect(await starting(page)).toBe(false)
  expect(await changes(page)).toEqual([true, false])
})

test('playback priority: whenPlaybackStarted resolves at once, on clear, on the cap, and on abort', async ({ page }) => {
  await loadModule(page)
  type Waiters = Window & {
    __resolved?: Record<string, boolean>
    __wait?: (name: string, signal?: AbortSignal) => void
  }
  await page.evaluate(() => {
    const w = window as PriorityWindow & Waiters
    w.__resolved = {}
    w.__wait = (name, signal = new AbortController().signal) => {
      w.__resolved![name] = false
      void w.__priority!.whenPlaybackStarted(signal).then(() => { w.__resolved![name] = true })
    }
  })
  const resolved = () => page.evaluate(() => (window as Waiters).__resolved!)
  const wait = (name: string) => page.evaluate((n) => (window as Waiters).__wait!(n), name)

  // Not starting: nothing to wait for.
  await wait('immediate')
  await expect.poll(resolved).toMatchObject({ immediate: true })

  // Starting: everything waits...
  await page.evaluate(() => (window as PriorityWindow).__priority!.setPlaybackStarting(true))
  await wait('onClear')
  await wait('onCap')
  await page.evaluate(() => {
    const w = window as PriorityWindow & Waiters
    const controller = new AbortController()
    ;(window as Window & { __abort?: () => void }).__abort = () => controller.abort()
    w.__wait!('onAbort', controller.signal)
  })
  await page.clock.runFor(1_000)
  expect(await resolved()).toMatchObject({ onClear: false, onCap: false, onAbort: false })
  // ...an abort releases only the aborted waiter (its caller then skips the work)...
  await page.evaluate(() => (window as Window & { __abort?: () => void }).__abort!())
  await expect.poll(resolved).toMatchObject({ onClear: false, onCap: false, onAbort: true })
  // ...and clearing, or the cap, releases the rest.
  await page.evaluate(() => (window as PriorityWindow).__priority!.setPlaybackStarting(false))
  await expect.poll(resolved).toMatchObject({ onClear: true, onCap: true })

  await page.evaluate(() => (window as PriorityWindow).__priority!.setPlaybackStarting(true))
  // The prefetch waits longer than album art does: 15 seconds, not 4.
  await wait('afterCap')
  await page.clock.runFor(14_900)
  expect(await resolved()).toMatchObject({ afterCap: false })
  expect(await starting(page)).toBe(false)
  await page.clock.runFor(200)
  await expect.poll(resolved).toMatchObject({ afterCap: true })
})
