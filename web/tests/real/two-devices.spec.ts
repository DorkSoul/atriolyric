import { expect, test } from 'playwright/test'
import {
  dropSocket,
  listenRows,
  isSilent,
  nameOf,
  openDevice,
  openDevicePicker,
  playAlbum,
  playerBar,
  playingTime,
  activeDeviceButton,
  shownPosition,
  trackTitle,
  type Device,
} from './devices'

// Real web app, real backend, two browser contexts acting as two devices of
// one user. No mocks. State-based waits only (never a fixed sleep).
test.describe.configure({ mode: 'serial' })

let a: Device
let b: Device

test.beforeEach(async ({ browser }) => {
  // Fresh contexts per scenario: new devices, no leftover page state. The
  // scenario's first step (A plays the album) replaces the server queue.
  a = await openDevice(browser, 'Chrome')
  b = await openDevice(browser, 'Edge')
})

test.afterEach(async () => {
  // A scenario may leave the server "playing" on a device whose context is
  // closed here; the next scenario's Play still plays (the requesting device
  // claims output from an offline active device - see the last test).
  await a.context.close()
  await b.context.close()
})

test('a remote shows what the active device plays, makes no sound, and can pause it', async () => {
  await playAlbum(a)
  await expect(trackTitle(b.page)).toContainText('One')
  await expect(activeDeviceButton(b.page)).toContainText(a.name)
  expect(await isSilent(b.page)).toBe(true)

  await playerBar(b.page).getByRole('button', { name: 'Pause', exact: true }).click()
  await expect.poll(() => isSilent(a.page)).toBe(true)
  await expect(playerBar(a.page).getByRole('button', { name: 'Play', exact: true })).toBeVisible()
  expect(await isSilent(b.page)).toBe(true)
})

test('Next on the remote starts the next track on the active device from the beginning', async () => {
  await playAlbum(a)
  await expect(trackTitle(b.page)).toContainText('One')
  // Let track one run past its start so a leaked position would show.
  await expect.poll(() => playingTime(a.page)).toBeGreaterThan(2.5)

  await playerBar(b.page).getByRole('button', { name: 'Next', exact: true }).click()
  await expect(trackTitle(a.page)).toContainText('Two')
  await expect(trackTitle(b.page)).toContainText('Two')
  await expect.poll(() => playingTime(a.page)).not.toBeNull()
  expect(await playingTime(a.page)).toBeLessThan(2)
  expect(await isSilent(b.page)).toBe(true)
})

test('a track that ends on its own advances exactly one track', async () => {
  await playAlbum(a)
  await playerBar(a.page).getByRole('button', { name: 'Next', exact: true }).click()
  await expect(trackTitle(a.page)).toContainText('Two') // the 3 second track
  // ...ends by itself and "Three" follows, starting near zero.
  await expect(trackTitle(a.page)).toContainText('Three', { timeout: 30_000 })
  await expect(trackTitle(b.page)).toContainText('Three')
  await expect.poll(() => playingTime(a.page)).not.toBeNull()
  expect(await playingTime(a.page)).toBeLessThan(2)
  // Not two tracks: after more than 2 s of "Three" it is still "Three".
  await expect.poll(() => playingTime(a.page)).toBeGreaterThan(2)
  await expect(trackTitle(a.page)).toContainText('Three')
  await expect(trackTitle(b.page)).toContainText('Three')

  // Nothing beyond what actually played is recorded: never "Four", and no
  // track twice.
  expect(new Set(a.played).size).toBe(a.played.length)
  expect(b.played).toEqual([])
  const recent = (await (await a.context.request.get('/api/library/recently-played?limit=10')).json()) as Array<{ title: string }>
  expect(recent.map((t) => t.title)).not.toContain('Four')
})

// A play counts once it has been listened to (half its length or four
// minutes), not when a source is loaded, so a next track the standby element
// prefetched is recorded too. These tracks are 20 s, 3 s and 20 s, so each is
// played past its half here (none scrobbles: 30 s or less).
test('every track that is listened to is recorded in the play history', async () => {
  const recent = async () => {
    const rows = (await (await a.context.request.get('/api/library/recently-played?limit=10')).json()) as Array<{ title: string }>
    return rows.map((t) => t.title)
  }
  await playAlbum(a)
  await expect.poll(() => playingTime(a.page), { timeout: 20_000 }).toBeGreaterThan(11.5)
  await expect.poll(recent).toContain('One')
  await playerBar(a.page).getByRole('button', { name: 'Next', exact: true }).click()
  await expect(trackTitle(a.page)).toContainText('Two')
  // "Two" plays to its end by itself, "Three" (on the preloaded standby) follows.
  await expect(trackTitle(a.page)).toContainText('Three', { timeout: 30_000 })
  await expect.poll(recent, { timeout: 20_000 }).toEqual(expect.arrayContaining(['Two']))
  await expect.poll(async () => (await recent()).slice(0, 3), { timeout: 20_000 }).toEqual(['Three', 'Two', 'One'])
  // Each once, and only by the device that played them.
  expect(new Set(a.played).size).toBe(3)
  expect(a.played).toHaveLength(3)
  expect(b.played).toEqual([])
})

// Every listen the playing device leaves is stored with how it
// ended: "One" skipped by Next (the user started it from the album), "Two"
// (3 s) played to its end after the queue moved on to it.
test('a skipped track and a finished track are stored with how they ended', async () => {
  const before = Math.max(0, ...listenRows().map((row) => row.id))
  const fresh = () => listenRows().filter((row) => row.id > before)
  await playAlbum(a)
  await expect.poll(() => playingTime(a.page), { timeout: 20_000 }).toBeGreaterThan(4)
  await playerBar(a.page).getByRole('button', { name: 'Next', exact: true }).click()
  await expect(trackTitle(a.page)).toContainText('Two')
  await expect(trackTitle(a.page)).toContainText('Three', { timeout: 30_000 })
  await expect.poll(() => fresh().map((row) => [row.title, row.end_reason]), { timeout: 20_000 })
    .toEqual([['One', 'skipped'], ['Two', 'finished']])
  const [one, two] = fresh()
  expect(one).toMatchObject({ start_kind: 'user', source_kind: 'album' })
  expect(one.listened_seconds).toBeGreaterThan(3)
  expect(one.listened_seconds).toBeLessThan(20)
  expect(two).toMatchObject({ start_kind: 'auto', source_kind: 'album' })
  expect(two.listened_seconds).toBeGreaterThan(2)
})

test('after the active device reconnects, a command from the remote still reaches it', async () => {
  await playAlbum(a)
  await expect(trackTitle(b.page)).toContainText('One')
  const sockets = a.sockets.length

  // The old connection is left open on the server's side, as after a network
  // drop; the replacement registers the same device while it still exists.
  await dropSocket(a)
  expect(a.sockets.length).toBeGreaterThan(sockets)
  await expect(activeDeviceButton(b.page)).toContainText(a.name)

  await playerBar(b.page).getByRole('button', { name: 'Next', exact: true }).click()
  await expect(trackTitle(a.page)).toContainText('Two')
  await expect(trackTitle(b.page)).toContainText('Two')
  await expect.poll(() => playingTime(a.page)).not.toBeNull()
  expect(await playingTime(a.page)).toBeLessThan(2)

  const menu = await openDevicePicker(b.page)
  await expect(menu.getByRole('menuitemradio', { name: nameOf(a) })).toBeVisible()
  await expect(menu.getByRole('menuitemradio', { name: nameOf(a) })).toBeChecked()
})

test('a seek on the remote moves the active device and does not bounce back', async () => {
  await playAlbum(a)
  await expect(trackTitle(b.page)).toContainText('One')
  const track = playerBar(b.page).getByRole('slider', { name: 'Seek' })
  await expect(track).toHaveAttribute('aria-valuemax', /^(19|20)$/)
  const box = await track.boundingBox()
  expect(box).not.toBeNull()
  await track.click({ position: { x: box!.width / 2, y: box!.height / 2 } })

  const target = 10
  await expect.poll(() => playingTime(a.page)).toBeGreaterThan(target - 2)
  expect(await playingTime(a.page)).toBeLessThan(target + 4)
  // Wait for later position reports from A to reach B, then check neither
  // device went back.
  await expect.poll(() => shownPosition(b.page)).toBeGreaterThan(target + 1)
  expect(await playingTime(a.page)).toBeGreaterThan(target - 2)
  expect(await shownPosition(b.page)).toBeGreaterThan(target - 2)
})

test('transferring playback to the other device continues from the same position', async () => {
  await playAlbum(a)
  await expect(trackTitle(b.page)).toContainText('One')
  await expect.poll(() => playingTime(a.page)).toBeGreaterThan(10)
  const before = (await playingTime(a.page)) ?? 0

  const menu = await openDevicePicker(b.page)
  await menu.getByRole('menuitemradio', { name: /\(this device\)/ }).click()

  // B plays from about where A was (a restart from 0 would need 7+ s to get
  // past this bound, longer than the wait), not from the start of the track.
  await expect.poll(() => playingTime(b.page), { timeout: 5_000 }).toBeGreaterThan(before - 3)
  expect(await playingTime(b.page)).toBeLessThan(before + 10)
  await expect.poll(() => isSilent(a.page)).toBe(true)
  await expect(trackTitle(a.page)).toContainText('One')
  await expect(activeDeviceButton(a.page)).toContainText(b.name)
})

// When the active device goes away while playing (tab closed, phone locked
// and dropped), the state stays "playing" on it; a new device that starts an
// album must not become a remote of nothing, silent everywhere. An explicit
// new queue from a connected device claims output when the active device is
// offline (playback.claim_output_for_requester).
test('a new device that starts an album plays it when the active device is gone', async ({ browser }) => {
  await playAlbum(a)
  await a.context.close()
  const c = await openDevice(browser, 'Chrome')
  try {
    await playAlbum(c)
    const menu = await openDevicePicker(c.page)
    await expect(menu.getByRole('menuitemradio', { name: /\(this device\)/ })).toBeChecked()
  } finally {
    await c.context.close()
  }
})
