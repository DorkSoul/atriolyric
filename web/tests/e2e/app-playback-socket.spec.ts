// The playback socket: commands while it is down, queue item ids, expired sessions, claiming output.

import { expect, test } from './fixtures'
import type { Page, Route } from 'playwright/test'
import { type AppMockState, installApiMock, playbackSnapshot, pushPlaybackState, releaseHeldPlaybackMessages } from './mockApi'
import { devicePickerToggle, dropPlaybackSocket, installMediaSessionStub, markActive, openApp, player, settle, silentWav } from './app-helpers'

// Every return to the foreground replaces the playback socket, and a locked
// phone's socket is suspended for minutes. Commands given in that gap are not
// dropped: Pause still pauses, and a pause the OS made (headphones unplugged)
// is kept, so the reconnect's "still playing" snapshot does not start the
// music again from the speaker.
test.describe('Playback commands while the socket is down', () => {
  /** Plays Bohemian Rhapsody from decodable files and returns its element. */
  async function startPlaying(page: Page) {
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(30) }))
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    const active = page.locator('audio[data-stream-src*="/api/stream/1"]').first()
    await expect.poll(() => active.evaluate((el) => (el as HTMLMediaElement).paused)).toBe(false)
    return active
  }

  /** Drops the socket and keeps refusing new ones until `reconnect`. */
  async function goOffline(page: Page, mock: AppMockState) {
    mock.playbackSocketMode = 'refuse'
    await dropPlaybackSocket(page, mock)
  }

  /** Lets the next connection through and runs the clock past any backoff. */
  async function reconnect(page: Page, mock: AppMockState) {
    const registrations = mock.wsMessages.filter((m) => m.type === 'register_device').length
    mock.playbackSocketMode = 'accept'
    await page.clock.runFor(31_000)
    await expect.poll(() => mock.wsMessages.filter((m) => m.type === 'register_device').length)
      .toBeGreaterThan(registrations)
  }

  const sentSince = (mock: AppMockState, from: number, types: string[]) =>
    mock.wsMessages.slice(from).map((m) => m.type).filter((type) => types.includes(String(type)))

  const isPaused = (element: ReturnType<Page['locator']>) =>
    element.evaluate((el) => (el as HTMLMediaElement).paused)

  test('a pause pressed while the socket is down pauses at once and reaches the server later', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    const active = await startPlaying(page)
    await goOffline(page, mock)
    const from = mock.wsMessages.length

    await player(page).getByRole('button', { name: 'Pause', exact: true }).click()
    expect(await isPaused(active)).toBe(true)
    await expect(player(page).getByRole('button', { name: 'Play', exact: true })).toBeVisible()
    expect(sentSince(mock, from, ['pause'])).toEqual([])

    await reconnect(page, mock)
    await expect.poll(() => sentSince(mock, from, ['pause', 'play'])).toEqual(['pause'])
    await expect.poll(() => mock.playback.is_playing).toBe(false)
    expect(await isPaused(active)).toBe(true)
  })

  test('Next pressed while the socket is down is delivered once after the reconnect', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    await startPlaying(page)
    await goOffline(page, mock)
    const from = mock.wsMessages.length

    await player(page).getByRole('button', { name: 'Next' }).click()
    expect(sentSince(mock, from, ['next'])).toEqual([])
    await reconnect(page, mock)
    await expect(player(page)).toContainText("Don't Stop Me Now")
    await page.clock.runFor(31_000)
    expect(sentSince(mock, from, ['next'])).toEqual(['next'])
    expect(mock.playback.media_item_id).toBe(2)
  })

  test('a command still waiting after 10 seconds is dropped with one message', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    await startPlaying(page)
    await goOffline(page, mock)
    const from = mock.wsMessages.length

    const bar = player(page)
    await bar.getByRole('button', { name: 'Next' }).click()
    await bar.getByRole('button', { name: 'Previous' }).click()
    await page.clock.runFor(10_500)
    await expect(page.locator('.toast').filter({ hasText: 'Not connected - try again' })).toHaveCount(1)

    await reconnect(page, mock)
    await page.clock.runFor(1_000)
    expect(sentSince(mock, from, ['next', 'prev'])).toEqual([])
    await expect(bar).toContainText('Bohemian Rhapsody')
  })

  test('play then pause while the socket is down sends only the pause', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    const active = await startPlaying(page)
    const bar = player(page)
    await bar.getByRole('button', { name: 'Pause', exact: true }).click()
    await expect.poll(() => mock.playback.is_playing).toBe(false)
    await goOffline(page, mock)
    const from = mock.wsMessages.length

    await bar.getByRole('button', { name: 'Play', exact: true }).click()
    expect(await isPaused(active)).toBe(false)
    await bar.getByRole('button', { name: 'Pause', exact: true }).click()
    expect(await isPaused(active)).toBe(true)

    await reconnect(page, mock)
    await page.clock.runFor(1_000)
    expect(sentSince(mock, from, ['pause', 'play'])).toEqual(['pause'])
    expect(await isPaused(active)).toBe(true)
  })

  test('a pause the OS makes while the socket is down is not undone by the reconnect', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    const active = await startPlaying(page)
    await goOffline(page, mock)
    const from = mock.wsMessages.length
    // Past the app's own media-change window (the drop re-ran the sync effect).
    await page.clock.fastForward(1_300)
    await active.evaluate((el) => {
      // Headphones unplugged: the element is paused without the app asking.
      ;(el as HTMLMediaElement).pause()
      el.dispatchEvent(new Event('pause'))
    })
    await page.clock.fastForward(300)
    // The server has heard nothing and still says playing.
    expect(mock.playback.is_playing).toBe(true)

    await reconnect(page, mock)
    await expect.poll(() => sentSince(mock, from, ['pause'])).toEqual(['pause'])
    await expect.poll(() => mock.playback.is_playing).toBe(false)
    await page.clock.runFor(6_000)
    expect(await isPaused(active)).toBe(true)
    expect(sentSince(mock, from, ['play'])).toEqual([])
  })

  test('the lock-screen pause works while the socket is down', async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    })
    await installMediaSessionStub(page)
    await page.clock.install()
    const mock = await openApp(page)
    const active = await startPlaying(page)
    await goOffline(page, mock)
    const from = mock.wsMessages.length

    await page.evaluate(() => {
      const stub = (window as Window & { __mediaSession?: { handlers: Record<string, () => void> } }).__mediaSession
      stub?.handlers.pause?.()
    })
    expect(await isPaused(active)).toBe(true)

    await reconnect(page, mock)
    await expect.poll(() => sentSince(mock, from, ['pause', 'play'])).toEqual(['pause'])
    expect(await isPaused(active)).toBe(true)
  })

  // The pause carries the position where the element stopped itself (no
  // separate `seek`).
  test('a pause the OS makes while the socket is down reaches the server as one pause with its position', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    const active = await startPlaying(page)
    await goOffline(page, mock)
    const from = mock.wsMessages.length
    await page.clock.fastForward(1_300)
    await active.evaluate((el) => {
      ;(el as HTMLMediaElement).pause()
      el.dispatchEvent(new Event('pause'))
    })
    await page.clock.fastForward(300)
    const position = await active.evaluate((el) => (el as HTMLMediaElement).currentTime)

    await reconnect(page, mock)
    await expect.poll(() => sentSince(mock, from, ['pause'])).toEqual(['pause'])
    await page.clock.runFor(1_000)
    expect(sentSince(mock, from, ['pause', 'seek'])).toEqual(['pause'])
    expect(mock.wsMessages.slice(from).find((m) => m.type === 'pause')?.position_seconds).toBe(position)
    expect(mock.playback.position_seconds).toBe(position)
  })
})

// The messages about the playing item name its queue row (the same track can
// be queued twice), and snapshots carry a revision.
test.describe('Playback messages name their queue item', () => {
  const sent = (mock: AppMockState, type: string, from = 0) =>
    mock.wsMessages.slice(from).filter((m) => m.type === type)
  const heldEnds = (mock: AppMockState) =>
    mock.heldPlaybackMessages.filter((m) => m.type === 'track_ended').map((m) => m.queue_item_id)

  async function playBohemianRhapsody(page: Page) {
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
  }

  /** The playing audio element reaches its end. */
  async function endPlayingTrack(page: Page) {
    // The bar names the track before its element has started.
    await expect
      .poll(() => page.evaluate(() => Array.from(document.querySelectorAll('audio')).some((audio) => !audio.paused)))
      .toBe(true)
    await page.evaluate(() => {
      const playing = Array.from(document.querySelectorAll('audio')).find((audio) => !audio.paused)
      if (!playing) throw new Error('no audio element is playing')
      playing.dispatchEvent(new Event('ended'))
    })
  }

  test('track_ended names the queue item that ended, also for a track queued twice in a row', async ({ page }) => {
    const mock = await openApp(page)
    await playBohemianRhapsody(page)
    // Bohemian Rhapsody twice, then Don't Stop Me Now: three queue rows.
    mock.playback.queue_media_item_ids = [1, 1, 2]
    mock.playback.queue_item_ids = [2001, 2002, 2003]
    mock.playback.queue_index = 0
    mock.playback.media_item_id = 1
    mock.playback.queue_context_count = 3
    pushPlaybackState(mock)
    await settle(page)
    mock.holdPlaybackTypes.add('track_ended')

    await endPlayingTrack(page)
    await expect.poll(() => heldEnds(mock)).toEqual([2001])
    // The page has handled the first end (it moved on to the second copy)
    // before the second end arrives.
    await settle(page)
    // The second copy (the element restarted the same track) ends before the
    // server has answered the first end.
    await endPlayingTrack(page)
    await expect.poll(() => heldEnds(mock)).toEqual([2001, 2002])

    releaseHeldPlaybackMessages(mock)
    await expect.poll(() => mock.playback.queue_index).toBe(2)
    await expect(player(page)).toContainText("Don't Stop Me Now")
  })

  test('the reconnect replay names the same item and a stale answer settles it without a second advance', async ({ page }) => {
    const mock = await openApp(page)
    await playBohemianRhapsody(page)
    const bar = player(page)
    const endedItem = playbackSnapshot(mock).current_queue_item_id
    mock.holdPlaybackTypes.add('track_ended')
    await endPlayingTrack(page)
    await expect(bar).toContainText("Don't Stop Me Now")
    await expect.poll(() => heldEnds(mock)).toEqual([endedItem])

    // The write looked successful, but the socket was already dead.
    mock.heldPlaybackMessages.length = 0
    const registrations = sent(mock, 'register_device').length
    await dropPlaybackSocket(page, mock)
    await expect.poll(() => sent(mock, 'register_device').length).toBeGreaterThan(registrations)
    // The registration snapshot still names the ended item: the end is replayed.
    await expect.poll(() => heldEnds(mock)).toEqual([endedItem])

    // Another device pressed Next meanwhile, and the server processed that
    // first: the replay is stale and only the sender hears back.
    mock.playback.queue_index = 1
    mock.playback.media_item_id = 2
    mock.playback.position_seconds = 0
    const settled = page.waitForEvent('console', (message) =>
      message.text().includes('pending_track_ended_cleared_by_stale_reply'))
    releaseHeldPlaybackMessages(mock)
    await settled
    expect(mock.playback.queue_index).toBe(1)
    expect(sent(mock, 'track_ended').map((m) => m.queue_item_id)).toEqual([endedItem, endedItem])
    await expect(bar).toContainText("Don't Stop Me Now")
  })

  // The save every five seconds names a queue item, not only a time. After a
  // local advance the element is on the next track while the server (and this
  // page's snapshot) still names the one that ended.
  test('the position report names the element\'s queue item, also right after a local advance', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(30) }))
    await playBohemianRhapsody(page)
    // The standby element holds the next track in memory.
    await expect(page.locator('audio[data-stream-src*="/api/stream/2"]')).toHaveAttribute('src', /^blob:/)
    const items = playbackSnapshot(mock).queue_item_ids
    let from = mock.wsMessages.length
    await page.clock.runFor(5_000)
    await expect.poll(() => sent(mock, 'position_report', from).map((m) => m.queue_item_id)).toEqual([items[0]])

    mock.holdPlaybackTypes.add('track_ended')
    await page.evaluate(() => {
      const audios = Array.from(document.querySelectorAll('audio'))
      const playing = audios.find((audio) => !audio.paused)
      const next = audios.find((audio) => audio.dataset.streamSrc?.includes('/api/stream/2'))
      if (!playing || !next) throw new Error('no playing element or standby')
      Object.defineProperty(next, 'readyState', { configurable: true, get: () => HTMLMediaElement.HAVE_ENOUGH_DATA })
      playing.dispatchEvent(new Event('ended'))
    })
    await expect(player(page)).toContainText("Don't Stop Me Now")
    await expect.poll(() => heldEnds(mock)).toEqual([items[0]])
    // The standby took over: the element now playing is on the next track.
    await expect.poll(() => page.evaluate(() => Array.from(document.querySelectorAll('audio'))
      .find((audio) => !audio.paused)?.dataset.streamSrc ?? '')).toMatch(/\/api\/stream\/2/)
    from = mock.wsMessages.length
    await page.clock.runFor(10_000)
    // Every report names the item the element is on now, never the ended one.
    await expect.poll(() => sent(mock, 'position_report', from).length).toBeGreaterThan(0)
    expect(sent(mock, 'position_report', from).map((m) => m.queue_item_id))
      .toEqual(sent(mock, 'position_report', from).map(() => items[1]))
  })

  test('the echo of this device\'s own position report never moves its element, however late', async ({ page }) => {
    await page.clock.install()
    // The mock's empty stream never loads, so the element reports whatever
    // position it is given.
    const mock = await openApp(page)
    await playBohemianRhapsody(page)
    const active = page.locator('audio[data-stream-src*="/api/stream/1"]').first()
    await active.evaluate((element) => { (element as HTMLMediaElement).currentTime = 60 })
    // Well past the 30 s for which this device remembers the positions it sent.
    await page.clock.runFor(31_000)
    mock.playback.position_seconds = 7
    pushPlaybackState(mock, { position_changed: true, source_device_id: 1, source_command: 'position_report' })
    await settle(page)
    expect(await active.evaluate((element) => (element as HTMLMediaElement).currentTime)).toBe(60)
  })

  test('a scrub sends a seek naming the playing queue item', async ({ page }) => {
    const mock = await openApp(page)
    await playBohemianRhapsody(page)
    const track = player(page).locator('.player-progress-track')
    const bounds = (await track.boundingBox())!
    await page.mouse.click(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2)
    await expect.poll(() => sent(mock, 'seek').at(-1)?.queue_item_id)
      .toBe(playbackSnapshot(mock).current_queue_item_id)
  })

  test('a pause from the playing device carries where its element stopped', async ({ page }) => {
    const mock = await openApp(page)
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(30) }))
    await playBohemianRhapsody(page)
    const active = page.locator('audio[data-stream-src*="/api/stream/1"]').first()
    await expect.poll(() => active.evaluate((el) => (el as HTMLMediaElement).readyState)).toBeGreaterThan(0)
    await player(page).getByRole('button', { name: 'Pause', exact: true }).click()
    await expect.poll(() => sent(mock, 'pause').length).toBe(1)
    const position = await active.evaluate((el) => (el as HTMLMediaElement).currentTime)
    expect(sent(mock, 'pause')[0].position_seconds).toBe(position)
  })

  // track_ended must name a row. The page cannot always tell the
  // row from its own bookkeeping; then it names the row the server's last
  // state says is playing, if that state is on the element's song.
  test('a song queued twice that ends names the row the server says is playing', async ({ page }) => {
    const mock = await openApp(page)
    await playBohemianRhapsody(page)
    // Bohemian Rhapsody at the first and the last row, the last one playing.
    mock.playback.queue_media_item_ids = [1, 2, 1]
    mock.playback.queue_item_ids = [2001, 2002, 2003]
    mock.playback.queue_index = 2
    mock.playback.media_item_id = 1
    mock.playback.queue_context_count = 3
    pushPlaybackState(mock)
    await settle(page)
    mock.holdPlaybackTypes.add('track_ended')

    await endPlayingTrack(page)
    await expect.poll(() => heldEnds(mock)).toEqual([2003])
  })

  // The element can still be on a song the server has left (the snapshot that
  // moved it is applied, but the new item is still being fetched). Its end
  // names nothing the server plays: no track_ended (one without a row would
  // skip whatever the server had moved on to), no local advance along this
  // page's old queue - the page follows the server's state.
  test('an element that ends on an item the server has left sends no track_ended and follows the server', async ({ page }) => {
    const mock = await openApp(page)
    await playBohemianRhapsody(page)
    const bar = player(page)
    let releaseLookup = () => {}
    const lookupHeld = new Promise<void>((resolve) => { releaseLookup = resolve })
    await page.route(/\/api\/library\/media\?ids=/, async (route) => {
      if (new URL(route.request().url()).searchParams.get('ids')?.split(',').includes('9')) await lookupHeld
      await route.fallback()
    })
    mock.tracks.push({ ...mock.tracks[0], id: 9, title: 'Somebody to Love', duration_seconds: 296 })
    // Another device started a new queue holding only track 9.
    mock.playback.queue_media_item_ids = [9]
    mock.playback.queue_item_ids = [3001]
    mock.playback.queue_index = 0
    mock.playback.media_item_id = 9
    mock.playback.queue_context_count = 1
    mock.playback.position_seconds = 0
    pushPlaybackState(mock, { source_device_id: 2, source_command: 'set_queue_context' })
    await settle(page)
    await expect(bar).toContainText('Bohemian Rhapsody')

    const notSent = page.waitForEvent('console', (message) =>
      message.text().includes('track_ended_not_sent_item_not_current'))
    await endPlayingTrack(page)
    await notSent
    await settle(page)
    expect(sent(mock, 'track_ended')).toEqual([])
    // Not the next song of the old queue.
    await expect(bar).not.toContainText("Don't Stop Me Now")

    releaseLookup()
    await expect(bar).toContainText('Somebody to Love')
    const playing = page.locator('audio[data-stream-src*="/api/stream/9"]').first()
    await expect.poll(() => playing.evaluate((el) => (el as HTMLMediaElement).paused)).toBe(false)
    expect(mock.playback.media_item_id).toBe(9)
    expect(sent(mock, 'track_ended')).toEqual([])
  })

  test('a pause from a remote control carries a null position', async ({ page }) => {
    const mock = await openApp(page)
    await playBohemianRhapsody(page)
    markActive(mock, 2)
    pushPlaybackState(mock)
    await settle(page)
    const from = mock.wsMessages.length
    await player(page).getByRole('button', { name: 'Pause', exact: true }).click()
    await expect.poll(() => sent(mock, 'pause', from).length).toBe(1)
    const [pause] = sent(mock, 'pause', from)
    expect('position_seconds' in pause).toBe(true)
    expect(pause.position_seconds).toBeNull()
  })

  test('a snapshot older than the last one applied is ignored until the next reconnect', async ({ page }) => {
    const mock = await openApp(page)
    await playBohemianRhapsody(page)
    const bar = player(page)
    // Another device plays, so track changes here are only what is shown.
    markActive(mock, 2)
    pushPlaybackState(mock)
    await expect(devicePickerToggle(page)).toHaveAccessibleName(/Living room/)

    // Two broadcasts delivered out of order: the newer one first.
    mock.playback.queue_index = 1
    mock.playback.media_item_id = 2
    mock.playback.revision += 1
    const older = playbackSnapshot(mock)
    mock.playback.queue_index = 2
    mock.playback.media_item_id = 3
    pushPlaybackState(mock)
    await expect(bar).toContainText('Midnight City')
    mock.playbackSocket!.send(JSON.stringify({ type: 'state', state: older }))
    await settle(page)
    expect(await bar.textContent()).toContain('Midnight City')

    // A new socket may meet a lower revision (a server restored from a
    // backup): it is taken as it is.
    mock.playback.revision = 1
    mock.playback.queue_index = 1
    mock.playback.media_item_id = 2
    await dropPlaybackSocket(page, mock)
    await expect(bar).toContainText("Don't Stop Me Now")
  })
})

// An expired or revoked session does not leave the signed-in shell up with
// empty lists and error toasts, nor a rejected playback socket retried every
// 2 seconds, for ever, in every open tab.
test.describe('Expired sessions and reconnecting', () => {
  /** Counts the app's socket lifecycle log lines (`websocket_connecting` and
   * `websocket_closed`), which it writes the moment each happens. */
  function watchSocketLog(page: Page) {
    const counts = { connecting: 0, closed: 0 }
    page.on('console', (message) => {
      const text = message.text()
      if (text.includes('"event":"websocket_connecting"')) counts.connecting += 1
      if (text.includes('"event":"websocket_closed"')) counts.closed += 1
    })
    return counts
  }

  const registrations = (mock: AppMockState) => mock.wsMessages.filter((m) => m.type === 'register_device').length
  const expiredMessage = 'Your session has expired - please sign in again.'

  test('a 401 from any API call returns to the login page with one message', async ({ page }) => {
    const mock = await openApp(page)
    // The session ends with the library request the click makes, not before:
    // a background request that met a 401 first would show the login page
    // while the click was still waiting for its button to hold still. The
    // click is sent straight to the button for the same reason.
    let expired = false
    await page.route('**/api/library/**', (route) => {
      if (!expired) return route.fallback()
      mock.authenticated = false
      return route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Not authenticated' }),
      })
    })
    const songs = page.getByRole('button', { name: 'Songs', exact: true })
    await expect(songs).toBeVisible()
    expired = true
    await songs.dispatchEvent('click')
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
    await expect(page.getByText(expiredMessage)).toHaveCount(1)
    await settle(page)
    await expect(page.locator('.toast')).toHaveCount(0)
  })

  test('the login page tells wrong credentials, rate limiting and an unreachable server apart', async ({ page }) => {
    const mock = await installApiMock(page, false)
    await page.goto('/#/home')
    await page.getByPlaceholder('Username').fill('dork_soul')
    await page.getByPlaceholder('Password').fill('wrong password')
    const signIn = page.getByRole('button', { name: 'Sign in' })
    const error = page.locator('.login-error')

    await signIn.click()
    await expect(error).toHaveText('Incorrect username or password.')

    await page.route('**/api/auth/login', (route) => route.fulfill({
      status: 429,
      headers: { 'Retry-After': '120' },
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Too many login attempts' }),
    }))
    await signIn.click()
    await expect(error).toHaveText('Too many sign-in attempts - try again in 2 minutes.')

    await page.unroute('**/api/auth/login')
    await page.route('**/api/auth/login', (route) => route.abort())
    await signIn.click()
    await expect(error).toHaveText("Couldn't reach the server - try again in a moment.")

    await page.unroute('**/api/auth/login')
    await page.getByPlaceholder('Password').fill(mock.password)
    await signIn.click()
    await expect(page.locator('.home-view')).toBeVisible()
  })

  test('a first load that cannot reach the server offers a retry instead of the login form', async ({ page }) => {
    await installApiMock(page)
    await page.route('**/api/auth/me', (route) => route.abort())
    await page.goto('/#/home')
    await expect(page.getByText("Can't reach the server")).toBeVisible()
    await expect(page.getByRole('button', { name: 'Sign in' })).toHaveCount(0)

    // Still down: the screen stays.
    await page.getByRole('button', { name: 'Retry' }).click()
    await expect(page.getByRole('button', { name: 'Retry' })).toBeEnabled()
    await expect(page.getByText("Can't reach the server")).toBeVisible()

    // Back: the browser coming online retries by itself.
    await page.unroute('**/api/auth/me')
    await page.evaluate(() => window.dispatchEvent(new Event('online')))
    await expect(page.locator('.home-view')).toBeVisible()
  })

  test('logging out clears the user even when the request fails', async ({ page }) => {
    await openApp(page)
    await page.route('**/api/auth/logout', (route) => route.abort())
    await page.getByRole('button', { name: 'Settings' }).click()
    await page.getByRole('menuitem', { name: 'Log out' }).click()
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
  })

  test('reconnects with exponential backoff and starts over after registering', async ({ page }) => {
    await page.clock.install()
    const log = watchSocketLog(page)
    const mock = await openApp(page)
    // The start-up connection attempts (dev mode makes two) have all been
    // logged before the count below is taken: a late console message would
    // otherwise look like a reconnect that came too early.
    await expect.poll(() => registrations(mock)).toBeGreaterThan(0)
    await settle(page)
    await page.clock.pauseAt(new Date(Date.now() + 5_000))
    mock.playbackSocketMode = 'refuse'
    let connections = log.connecting
    let closed = 1
    await mock.playbackSocket!.close()
    await expect.poll(() => log.closed).toBe(closed)

    // 1 s doubling to a 30 s cap, each up to 20% early (jitter).
    for (const delay of [1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000]) {
      await page.clock.runFor(delay * 0.8 - 50)
      await page.evaluate(() => 0)
      expect(log.connecting, `nothing before ${delay * 0.8} ms`).toBe(connections)
      await page.clock.runFor(delay * 0.2 + 100)
      connections += 1
      await expect.poll(() => log.connecting, `a connection by ${delay} ms`).toBe(connections)
      // Refused at once; the next delay counts from that close.
      closed += 1
      await expect.poll(() => log.closed).toBe(closed)
    }

    // A registration starts the sequence over.
    mock.playbackSocketMode = 'accept'
    const before = registrations(mock)
    await page.clock.runFor(30_100)
    await expect.poll(() => registrations(mock)).toBe(before + 1)
    mock.playbackSocketMode = 'refuse'
    closed = log.closed
    connections = log.connecting
    await mock.playbackSocket!.close()
    await expect.poll(() => log.closed).toBe(closed + 1)
    await page.clock.runFor(1_050)
    await expect.poll(() => log.connecting).toBe(connections + 1)
  })

  test('reconnects at once when the browser comes back online', async ({ page }) => {
    await page.clock.install()
    const log = watchSocketLog(page)
    const mock = await openApp(page)
    await page.clock.pauseAt(new Date(Date.now() + 5_000))
    await mock.playbackSocket!.close()
    await expect.poll(() => log.closed).toBe(1)
    const connections = log.connecting
    const before = registrations(mock)
    await page.evaluate(() => window.dispatchEvent(new Event('online')))
    await expect.poll(() => log.connecting).toBe(connections + 1)
    await expect.poll(() => registrations(mock)).toBe(before + 1)
  })

  test('stops reconnecting and shows the login page when the socket is closed with 4401', async ({ page }) => {
    await page.clock.install()
    const log = watchSocketLog(page)
    const mock = await openApp(page)
    mock.authenticated = false
    await mock.playbackSocket!.close({ code: 4401, reason: 'session expired' })
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
    await expect(page.getByText(expiredMessage)).toBeVisible()
    const connections = log.connecting
    await page.clock.runFor(60_000)
    await page.evaluate(() => 0)
    expect(log.connecting).toBe(connections)
  })

  test('a failed handshake with an expired session shows the login page', async ({ page }) => {
    const mock = await openApp(page)
    mock.authenticated = false
    // Turned down before it opens, as the server does a socket without a
    // valid session (the browser cannot read the server's reason then).
    mock.playbackSocketMode = 'refuse'
    const meCalls = mock.calls.filter((call) => call.path === '/api/auth/me').length
    await mock.playbackSocket!.close()
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
    await expect(page.getByText(expiredMessage)).toBeVisible()
    expect(mock.calls.filter((call) => call.path === '/api/auth/me').length).toBe(meCalls + 1)
  })

  test('a reconnect refused with 4401 shows the login page without asking /api/auth/me', async ({ page }) => {
    await page.clock.install()
    const log = watchSocketLog(page)
    const mock = await openApp(page)
    mock.authenticated = false
    const meCalls = mock.calls.filter((call) => call.path === '/api/auth/me').length
    const connections = log.connecting
    // An ordinary drop; the replacement socket is then refused by the server.
    await mock.playbackSocket!.close()
    await page.clock.runFor(5_000)
    await expect.poll(() => log.connecting).toBeGreaterThan(connections)
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
    await expect(page.getByText(expiredMessage)).toBeVisible()
    expect(mock.calls.filter((call) => call.path === '/api/auth/me').length).toBe(meCalls)
    const after = log.connecting
    await page.clock.runFor(60_000)
    await page.evaluate(() => 0)
    expect(log.connecting).toBe(after)
  })

  test('a socket replaced by another tab reconnects only on focus', async ({ page }) => {
    await page.clock.install()
    const log = watchSocketLog(page)
    const mock = await openApp(page)
    await mock.playbackSocket!.close({ code: 4000, reason: 'replaced' })
    await expect.poll(() => log.closed).toBe(1)
    const connections = log.connecting
    await page.clock.runFor(60_000)
    await page.evaluate(() => 0)
    expect(log.connecting).toBe(connections)

    const before = registrations(mock)
    await page.evaluate(() => window.dispatchEvent(new Event('focus')))
    await expect.poll(() => log.connecting).toBe(connections + 1)
    await expect.poll(() => registrations(mock)).toBe(before + 1)
  })

  test('replaces a socket that misses two heartbeat answers', async ({ page }) => {
    await page.clock.install()
    const log = watchSocketLog(page)
    const mock = await openApp(page)
    // The server answers every heartbeat with a `heartbeat_ack`; this one has
    // gone quiet (a half-open connection), so nothing arrives at all.
    mock.holdPlaybackTypes.add('heartbeat')
    const connections = log.connecting
    const before = registrations(mock)
    // Heartbeats go out every 20 s: one unanswered is not enough...
    await page.clock.runFor(41_000)
    await page.evaluate(() => 0)
    expect(log.connecting).toBe(connections)
    // ...the second one, with 45 s of silence, is.
    await page.clock.runFor(20_000)
    await expect.poll(() => log.connecting).toBe(connections + 1)
    await expect.poll(() => registrations(mock)).toBe(before + 1)
  })

  // Heartbeats are answered with `heartbeat_ack` alone (no snapshot): on an
  // idle socket the acks are all that arrives.
  test('heartbeat acks alone keep a quiet socket', async ({ page }) => {
    await page.clock.install()
    const log = watchSocketLog(page)
    const mock = await openApp(page)
    const connections = log.connecting
    const states = mock.playback.revision
    await page.clock.runFor(100_000)
    await page.evaluate(() => 0)
    expect(mock.wsMessages.filter((m) => m.type === 'heartbeat').length).toBeGreaterThanOrEqual(4)
    expect(mock.playback.revision).toBe(states)
    expect(log.connecting).toBe(connections)
  })

  test('shows that it is reconnecting after three seconds without the socket', async ({ page }) => {
    // The notice lives in the player bar (under 1200px); the desktop side
    // panel leaves device status to the header's device picker.
    await page.setViewportSize({ width: 1000, height: 800 })
    await page.clock.install()
    const mock = await openApp(page)
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(30) }))
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    const bar = player(page)
    await expect(bar).toContainText('Bohemian Rhapsody')
    const status = bar.getByRole('status').filter({ hasText: 'Reconnecting' })

    mock.playbackSocketMode = 'refuse'
    await dropPlaybackSocket(page, mock)
    await page.clock.runFor(2_000)
    await expect(status).toHaveCount(0)
    await page.clock.runFor(1_500)
    await expect(status).toBeVisible()

    mock.playbackSocketMode = 'accept'
    await page.clock.runFor(31_000)
    await expect(status).toHaveCount(0)
  })
})

// A device that starts playback while the active device is gone plays it;
// a device that comes back and finds another one active goes quiet.
test.describe('Claiming output from a vanished device', () => {
  test('starting a queue while the active device is offline starts audio here', async ({ page }) => {
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(30) }))
    const mock = await installApiMock(page)
    // "Living room" was playing and vanished without pausing.
    markActive(mock, 2)
    mock.playback.devices[1].is_connected = false
    mock.playback.queue_media_item_ids = [3]
    mock.playback.queue_item_ids = [7001]
    mock.playback.queue_context_count = 1
    mock.playback.media_item_id = 3
    mock.playback.is_playing = true
    await page.goto('/#/home')
    await expect(devicePickerToggle(page)).toHaveAccessibleName(/Living room \(offline\)/)

    mock.holdPlaybackTypes.add('set_queue_context')
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    // Started inside the click, before the server has answered.
    const main = page.locator('[data-player-element="main"]')
    await expect(main).toHaveAttribute('data-stream-src', /\/api\/stream\/1(\?|$)/)
    await expect.poll(() => main.evaluate((el) => (el as HTMLMediaElement).paused)).toBe(false)

    releaseHeldPlaybackMessages(mock)
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    await expect(devicePickerToggle(page)).not.toHaveAccessibleName(/Living room/)
    expect(mock.playback.active_device_id).toBe(1)
    await settle(page)
    expect(await main.evaluate((el) => (el as HTMLMediaElement).paused)).toBe(false)
  })

  test('a device the server hands the output to although the active one still looked connected plays it', async ({ page }) => {
    // The Android suite's case: the page that was playing closed, but its
    // socket never closed on the server, so every snapshot still lists it as
    // connected. The server hands the output over anyway (nothing heard from
    // it), so this page cannot start the element inside the click (it reads
    // as a remote control here) and must start it when the state arrives.
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(30) }))
    const mock = await installApiMock(page)
    markActive(mock, 2)
    mock.silentDeviceIds.add(2)
    mock.playback.queue_media_item_ids = [3]
    mock.playback.queue_item_ids = [7001]
    mock.playback.queue_context_count = 1
    mock.playback.media_item_id = 3
    mock.playback.is_playing = true
    await page.goto('/#/home')
    await expect(devicePickerToggle(page)).toHaveAccessibleName(/Living room/)

    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect(player(page)).toContainText('Bohemian Rhapsody')
    expect(mock.playback.active_device_id).toBe(1)
    await expect(devicePickerToggle(page)).not.toHaveAccessibleName(/Living room/)
    const main = page.locator('[data-player-element="main"]')
    await expect(main).toHaveAttribute('data-stream-src', /\/api\/stream\/1(\?|$)/)
    await expect.poll(() => main.evaluate((el) => (el as HTMLMediaElement).paused)).toBe(false)
    await settle(page)
    expect(await main.evaluate((el) => (el as HTMLMediaElement).paused)).toBe(false)
  })

  test('a device that learns on reconnect that another one took over stops its audio', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(30) }))
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    const main = page.locator('audio[data-stream-src*="/api/stream/1"]').first()
    await expect.poll(() => main.evaluate((el) => (el as HTMLMediaElement).paused)).toBe(false)

    // The socket drops (a locked phone): the music goes on meanwhile...
    mock.playbackSocketMode = 'refuse'
    await dropPlaybackSocket(page, mock)
    expect(await main.evaluate((el) => (el as HTMLMediaElement).paused)).toBe(false)
    // ...while another device starts playing.
    markActive(mock, 2)
    // The mock hands output back to any device registering with the resume
    // flag; the real server does so only when the active device's row is
    // gone (register_device), so drop the flag to model that.
    await page.evaluate(() => window.sessionStorage.removeItem('atriolyric.playback-resume'))

    const registrations = mock.wsMessages.filter((m) => m.type === 'register_device').length
    mock.playbackSocketMode = 'accept'
    await page.clock.runFor(31_000)
    await expect.poll(() => mock.wsMessages.filter((m) => m.type === 'register_device').length)
      .toBeGreaterThan(registrations)
    await expect(devicePickerToggle(page)).toHaveAccessibleName(/Living room/)
    await expect.poll(() => main.evaluate((el) => (el as HTMLMediaElement).paused)).toBe(true)
  })
})

// A phone that loses its network mid-album keeps playing what it already has:
// at a track's end it moves on by itself to the next queued track (preloaded
// as an in-memory copy, see usePrefetch.ts), keeps the end to tell the server
// after the reconnect, and the server then agrees with where it is.
test.describe('Track ends while the network is gone', () => {
  const streamId = /\/api\/stream\/(\d+)(\?|$)/

  /** Serves decodable files and records which tracks finished downloading
   * as copies. Once `network.down()` is called every stream request fails,
   * and so does every one that was being held: with `only`, requests for
   * other tracks are held (not yet downloaded) until then. After
   * `network.up()` every track is served. */
  async function serveStreams(page: Page, only?: number) {
    let offline = false
    let wentDown = false
    const held: Route[] = []
    const copied = new Set<number>()
    await page.route(streamId, async (route) => {
      const id = Number(streamId.exec(route.request().url())![1])
      if (offline) return route.abort('internetdisconnected').catch(() => {})
      if (only != null && !wentDown && id !== only) return void held.push(route)
      await route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(30) }).catch(() => {})
    })
    page.on('requestfinished', (request) => {
      const id = streamId.exec(request.url())?.[1]
      if (id && request.resourceType() === 'fetch') copied.add(Number(id))
    })
    const network = {
      down() {
        offline = true
        wentDown = true
        for (const route of held.splice(0)) void route.abort('internetdisconnected').catch(() => {})
      },
      up() { offline = false },
    }
    return { network, copied }
  }

  async function goOffline(page: Page, mock: AppMockState, network: { down(): void }) {
    network.down()
    mock.playbackSocketMode = 'refuse'
    await page.evaluate(() => {
      Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => false })
      window.dispatchEvent(new Event('offline'))
    })
    await dropPlaybackSocket(page, mock)
  }

  async function comeBack(page: Page, mock: AppMockState, network: { up(): void }) {
    network.up()
    const registrations = mock.wsMessages.filter((m) => m.type === 'register_device').length
    mock.playbackSocketMode = 'accept'
    await page.evaluate(() => {
      Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => true })
      window.dispatchEvent(new Event('online'))
    })
    await page.clock.runFor(31_000)
    await expect.poll(() => mock.wsMessages.filter((m) => m.type === 'register_device').length)
      .toBeGreaterThan(registrations)
  }

  /** The id of the track an audio element is playing, or null. Only an
   * element with data and no error counts: a failed load can leave `paused`
   * false. */
  const soundingTrack = (page: Page) =>
    page.evaluate(() => {
      const audio = Array.from(document.querySelectorAll('audio'))
        .find((a) => !a.paused && !a.error && a.readyState > 0 && a.dataset.streamSrc)
      const id = audio && /\/api\/stream\/(\d+)/.exec(audio.dataset.streamSrc!)?.[1]
      return id ? Number(id) : null
    })

  async function endPlayingTrack(page: Page) {
    await expect.poll(() => soundingTrack(page)).not.toBeNull()
    await page.evaluate(() => {
      const playing = Array.from(document.querySelectorAll('audio')).find((a) => !a.paused && !a.error && a.readyState > 0)
      playing!.dispatchEvent(new Event('ended'))
    })
  }

  const ends = (mock: AppMockState, from: number) =>
    mock.wsMessages.slice(from).filter((m) => m.type === 'track_ended').map((m) => m.queue_item_id)

  test('plays on through the preloaded tracks and the server catches up on reconnect', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    const { network, copied } = await serveStreams(page)
    const bar = player(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect.poll(() => soundingTrack(page)).toBe(1)
    // The next two tracks are in memory before the network goes.
    await expect.poll(() => [...copied].sort()).toEqual(expect.arrayContaining([2, 3]))
    const [firstRow, secondRow, thirdRow] = mock.playback.queue_item_ids

    await goOffline(page, mock, network)
    const from = mock.wsMessages.length

    await endPlayingTrack(page)
    await expect(bar).toContainText("Don't Stop Me Now")
    await expect.poll(() => soundingTrack(page)).toBe(2)
    await endPlayingTrack(page)
    await expect(bar).toContainText('Midnight City')
    await expect.poll(() => soundingTrack(page)).toBe(3)
    expect(ends(mock, from)).toEqual([])

    await comeBack(page, mock, network)
    // Both ends are replayed, in order: the server moves to where the phone is.
    await expect.poll(() => mock.playback.media_item_id).toBe(3)
    await page.clock.runFor(2_000)
    expect(ends(mock, from)).toEqual([firstRow, secondRow])
    expect(playbackSnapshot(mock).current_queue_item_id).toBe(thirdRow)
    await expect(bar).toContainText('Midnight City')
    expect(await soundingTrack(page)).toBe(3)
  })

  // A locked phone's socket can stay suspended for over a minute after a track
  // ends. The end must not be dropped as too old, or the registration snapshot
  // (still the finished track, at its last saved position) is applied and the
  // phone plays that track's last ten seconds again before restarting the next
  // one from 0.
  test('an end kept longer than a minute is still replayed while the next track plays on', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    const { network, copied } = await serveStreams(page)
    const bar = player(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect.poll(() => soundingTrack(page)).toBe(1)
    await expect.poll(() => copied.has(2)).toBe(true)
    const [firstRow, secondRow] = mock.playback.queue_item_ids

    await goOffline(page, mock, network)
    const from = mock.wsMessages.length
    await endPlayingTrack(page)
    await expect(bar).toContainText("Don't Stop Me Now")
    await expect.poll(() => soundingTrack(page)).toBe(2)
    await page.clock.runFor(60_000)

    await comeBack(page, mock, network)
    await expect.poll(() => mock.playback.media_item_id).toBe(2)
    await page.clock.runFor(2_000)
    expect(ends(mock, from)).toEqual([firstRow])
    expect(playbackSnapshot(mock).current_queue_item_id).toBe(secondRow)
    await expect(bar).toContainText("Don't Stop Me Now")
    expect(await soundingTrack(page)).toBe(2)
  })

  // Chrome on Android does not buffer a paused element ahead, so the standby
  // holding the next track's copy is often not ready when the track ends; the
  // gapless hand-off then waits for the sync effect, which waits for the
  // socket.
  test('plays on offline when the standby holding the next track is not buffered yet', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    const { network, copied } = await serveStreams(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect.poll(() => soundingTrack(page)).toBe(1)
    await expect.poll(() => copied.has(2)).toBe(true)
    const standby = page.locator('[data-player-element="standby"]')
    await expect(standby).toHaveAttribute('src', /^blob:/)
    await standby.evaluate((el) => {
      Object.defineProperty(el, 'readyState', { configurable: true, get: () => HTMLMediaElement.HAVE_METADATA })
    })

    await goOffline(page, mock, network)
    await endPlayingTrack(page)
    await expect(player(page)).toContainText("Don't Stop Me Now")
    await expect.poll(() => soundingTrack(page)).toBe(2)
  })

  test('the next track, not downloaded when the network went, starts when it comes back', async ({ page }) => {
    await page.clock.install()
    const mock = await openApp(page)
    // Only the first track arrives before the network goes.
    const { network } = await serveStreams(page, 1)
    const bar = player(page)
    await page.getByRole('button', { name: 'Songs', exact: true }).click()
    await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
    await expect.poll(() => soundingTrack(page)).toBe(1)
    const [firstRow] = mock.playback.queue_item_ids

    await goOffline(page, mock, network)
    const from = mock.wsMessages.length
    await endPlayingTrack(page)
    await expect(bar).toContainText("Don't Stop Me Now")
    // It cannot play without the network, and it is not the file's fault: no
    // "can't be played", and however long it waits it does not give up.
    for (let i = 0; i < 6; i++) await page.clock.runFor(5_000)
    await expect(page.locator('.toast')).toHaveCount(0)
    expect(await soundingTrack(page)).toBeNull()

    await comeBack(page, mock, network)
    await expect.poll(() => soundingTrack(page)).toBe(2)
    await expect.poll(() => mock.playback.media_item_id).toBe(2)
    expect(mock.playback.is_playing).toBe(true)
    expect(ends(mock, from)).toEqual([firstRow])
    await expect(bar).toContainText("Don't Stop Me Now")
    await expect(page.locator('.toast')).toHaveCount(0)
  })
})

// A queue can hold 5,000 explicit rows, while one `GET /api/library/media`
// takes 20,000 characters of ids and reads 2,000 of them: asked for in one
// request, a long queue would be refused on every snapshot, so the device
// would have no queue to show, no next track to prefetch and nothing to
// advance to at a track end.
test.describe('A queue longer than one lookup', () => {
  const FIRST_ID = 100_000
  const LENGTH = 3_000
  const PLAYING = 2_500

  /** Queues 3,000 tracks this page has never seen, playing row 2,501 here. */
  function longQueue(mock: AppMockState): number[] {
    const base = mock.tracks[0]
    const ids = Array.from({ length: LENGTH }, (_, i) => FIRST_ID + i)
    for (const id of ids) mock.tracks.push({ ...base, id, title: `Long Track ${id - FIRST_ID + 1}` })
    mock.playback.queue_media_item_ids = ids
    mock.playback.queue_item_ids = ids.map(() => mock.nextQueueItemId++)
    mock.playback.queue_index = PLAYING
    mock.playback.media_item_id = ids[PLAYING]
    mock.playback.queue_context_count = ids.length
    mock.playback.position_seconds = 0
    mock.playback.is_playing = true
    return ids
  }

  /** The ids of each queue lookup made so far (a lookup of one id is the
   * current track's own). */
  const lookups = (mock: AppMockState) =>
    mock.calls
      .filter((call) => call.path === '/api/library/media')
      .map((call) => new URLSearchParams(call.url.split('?')[1]).get('ids')!.split(',').map(Number))
      .filter((ids) => ids.length > 1)

  const standbyTrack = (page: Page) =>
    page.locator('[data-player-element="standby"]').evaluate((el) => (el as HTMLElement).dataset.streamSrc ?? null)

  test('is looked up in parts, the rows around the playing one first', async ({ page }) => {
    // Below the side panel's 1200px: the queue is listed only in Now Playing.
    await page.setViewportSize({ width: 1100, height: 800 })
    const mock = await openApp(page)
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(30) }))
    const ids = longQueue(mock)
    pushPlaybackState(mock)

    await expect(player(page)).toContainText('Long Track 2501')
    // The next track is there to preload: playback has its part of the queue.
    await expect.poll(() => standbyTrack(page)).toContain(`/api/stream/${ids[PLAYING + 1]}`)
    await expect.poll(() => new Set(lookups(mock).flat()).size).toBeGreaterThanOrEqual(LENGTH - 1)
    const made = lookups(mock)
    expect(Math.max(...made.map((part) => part.length)), 'ids in one request').toBeLessThanOrEqual(500)
    // First the rows from a little before the playing one on...
    expect(made[0][0]).toBe(ids[PLAYING - 50])
    expect(made[0]).toContain(ids[PLAYING + 1])
    // ...to the end of the queue, then the rows before them.
    expect(made.at(-1)!.at(-1)).toBe(ids[PLAYING - 51])

    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    const view = page.getByRole('region', { name: 'Now playing', exact: true })
    await expect(view.locator('.queue-row-current')).toContainText('Long Track 2501')
    await expect(view.locator('.nowplaying-side-count')).toHaveText(String(LENGTH - PLAYING - 1))
    await expect(view.getByRole('button', { name: `Show ${PLAYING} previous` })).toBeVisible()
  })

  test('a part that fails leaves the others in place and is asked for again by the next snapshot', async ({ page }) => {
    await page.setViewportSize({ width: 1100, height: 800 })
    const mock = await openApp(page)
    await page.route(/\/api\/stream\/\d+(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'audio/wav', body: silentWav(30) }))
    const ids = longQueue(mock)
    // The request for the start of the queue fails for now.
    let failing = true
    let failed = 0
    await page.route(/\/api\/library\/media\?/, (route) => {
      const asked = new URL(route.request().url()).searchParams.get('ids')!.split(',')
      if (failing && asked.includes(String(ids[0]))) {
        failed += 1
        return route.fulfill({ status: 502, contentType: 'text/plain', body: 'bad gateway' })
      }
      return route.fallback()
    })
    pushPlaybackState(mock)

    await expect(player(page)).toContainText('Long Track 2501')
    await expect.poll(() => failed).toBe(1)
    // Playback has the rows that did arrive: the next track is preloaded.
    await expect.poll(() => standbyTrack(page)).toContain(`/api/stream/${ids[PLAYING + 1]}`)
    // The list is not shown with rows missing (they are matched to the
    // server's queue items by position).
    await player(page).getByRole('button', { name: 'Open now playing' }).click()
    const view = page.getByRole('region', { name: 'Now playing', exact: true })
    await expect(view).toBeVisible()
    await settle(page)
    await expect(view.locator('.queue-row')).toHaveCount(0)

    // The next snapshot asks again, for the missing rows only.
    failing = false
    const before = lookups(mock).length
    pushPlaybackState(mock)
    await expect(view.locator('.queue-row-current')).toContainText('Long Track 2501')
    await expect(view.getByRole('button', { name: `Show ${PLAYING} previous` })).toBeVisible()
    const again = lookups(mock).slice(before).flat()
    expect(again).toContain(ids[0])
    expect(again).not.toContain(ids[PLAYING + 1])
    expect(again.length).toBeLessThanOrEqual(500)
  })
})

// "Is this page the output right now?" is derived in one module that every
// part of the player reads. There is no unit-test runner in
// web/, so it is loaded into a bare page through Vite.
test.describe('Output state', () => {
  test('one state for every mix of socket, tab and active device, and what each consumer does with it', async ({ page }) => {
    await page.route('**/output-blank', (route) =>
      route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>blank</title>' }))
    await page.goto('/output-blank')
    const results = await page.evaluate(async () => {
      type Output = typeof import('../../src/player/outputState')
      const modulePath = '/src/player/outputState.ts'
      const output: Output = await import(/* @vite-ignore */ modulePath)
      const me = 1
      const other = 2
      const derive = (inputs: Partial<Parameters<Output['deriveOutputState']>[0]>) => output.deriveOutputState({
        connected: true,
        socketReplaced: false,
        deviceId: me,
        lastDeviceId: me,
        activeDeviceId: me,
        ...inputs,
      })
      const down = { connected: false, deviceId: null }
      const states = {
        active: derive({}),
        remote: derive({ activeDeviceId: other }),
        none: derive({ activeDeviceId: null }),
        noSnapshotYet: derive({ activeDeviceId: undefined }),
        dropout: derive(down),
        offlineRemote: derive({ ...down, activeDeviceId: other }),
        neverRegistered: derive({ ...down, lastDeviceId: null, activeDeviceId: null }),
        replaced: derive({ ...down, socketReplaced: true }),
      }
      const playing = { hasCurrent: true, serverPlaying: true }
      const all = ['active', 'dropout', 'offline', 'replaced', 'remote', 'none'] as const
      const where = (test: (state: (typeof all)[number]) => boolean) => all.filter(test).join(' ')
      return {
        states,
        elementsKept: where((state) => output.keepsElementsThroughSocketLoss(state, { hasCurrent: true, wantPlaying: true })),
        elementsKeptPaused: where((state) => output.keepsElementsThroughSocketLoss(state, { hasCurrent: true, wantPlaying: false })),
        sessionKeptHidden: where((state) => output.keepsMediaSessionThroughSocketLoss(state, { ...playing, hidden: true })),
        sessionKeptVisible: where((state) => output.keepsMediaSessionThroughSocketLoss(state, { ...playing, hidden: false })),
        keepAliveHidden: where((state) => output.wantsKeepAlive(state, { ...playing, hidden: true })),
        keepAliveVisible: where((state) => output.wantsKeepAlive(state, { ...playing, hidden: false })),
        keepAlivePaused: where((state) => output.wantsKeepAlive(state, { hasCurrent: true, serverPlaying: false, hidden: true })),
        backgroundVideoKept: where((state) => output.keepsBackgroundVideoAudio(state)),
        elsewhere: [output.anotherDeviceIsActive(other, me), output.anotherDeviceIsActive(me, me), output.anotherDeviceIsActive(null, me)],
      }
    })
    expect(results).toEqual({
      states: {
        active: 'active',
        remote: 'remote',
        none: 'none',
        noSnapshotYet: 'none',
        dropout: 'dropout',
        offlineRemote: 'offline',
        neverRegistered: 'offline',
        replaced: 'replaced',
      },
      elementsKept: 'dropout offline',
      elementsKeptPaused: '',
      sessionKeptHidden: 'dropout offline',
      sessionKeptVisible: '',
      keepAliveHidden: 'active dropout offline',
      keepAliveVisible: 'active',
      keepAlivePaused: '',
      backgroundVideoKept: 'active dropout offline',
      elsewhere: [true, false, false],
    })
  })
})
