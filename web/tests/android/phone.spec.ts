import { writeFileSync } from 'node:fs'
import type { Page } from 'playwright/test'
import { isSilent as desktopSilent, openDevice, playAlbum, remoteNote } from '../real/devices'
import { back, PHONE_ORIGIN, screenOff, screenOn, shell, wakefulness } from './adb'
import { setPhoneNetwork } from './net-proxy'
import {
  expect,
  isSilent,
  LAUNCH_MODE,
  musicElements,
  playerBar,
  playingTime,
  shotPath,
  signIn,
  tapPlayAlbum,
  test,
  trackTitle,
  type Phone,
  type Server,
} from './phone'

// Real Chrome on an Android phone (emulator), the real backend and the Vite
// dev server; the "desktop" device is a Chromium context on this machine.
// Nothing is mocked. The tests run one after another in this one file (one
// phone); they do not depend on each other: each starts Chrome afresh with
// no data for the app's origin and signs in.

const ARTIST = 'Phone Artist'

function note(name: string, value: unknown): void {
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  test.info().annotations.push({ type: name, description: text })
  console.log(`[android:${test.info().title.slice(0, 2).trim()}] ${name}: ${text}`)
}

function next(page: Page) {
  return playerBar(page).getByRole('button', { name: 'Next', exact: true })
}

/** Chrome's activity lifecycle state (RESUMED, STOPPED, ...) as Android sees it. */
function chromeActivityState(): string {
  const dump = shell('dumpsys activity activities')
  const at = dump.search(/Task\{[^\n]*A=\d+:com\.android\.chrome/)
  return at < 0 ? 'no task' : (/state=(\w+)/.exec(dump.slice(at))?.[1] ?? 'unknown')
}

async function activeDevice(server: Server): Promise<number | null> {
  return (await server.state()).active_device_id
}

async function phoneConnected(server: Server, phone: Phone): Promise<boolean | undefined> {
  return (await server.state()).devices.find((d) => d.id === phone.deviceId)?.is_connected
}

test.beforeAll(() => {
  const chrome = shell('dumpsys package com.android.chrome').match(/versionName=(\S+)/)?.[1] ?? 'unknown'
  console.log(`[android] launch mode: ${LAUNCH_MODE}; Chrome ${chrome}`)
})

test('1 sign in and start an album with a real tap', async ({ phone, server }) => {
  const { page } = phone
  await signIn(phone)
  note('device id', phone.deviceId)
  await tapPlayAlbum(phone, 'Real Artist', 'Real Album', 'One')
  // The clock really moves (decoded and advancing, not just "not paused").
  const t0 = (await playingTime(page)) ?? 0
  await expect.poll(() => playingTime(page)).toBeGreaterThan(t0 + 1)
  await expect(trackTitle(page)).toContainText('One')
  await expect.poll(() => activeDevice(server)).toBe(phone.deviceId)
  const state = await server.state()
  expect(state.is_playing).toBe(true)
  note('evidence', {
    playingTime: await playingTime(page),
    activeDeviceId: state.active_device_id,
    deviceName: state.devices.find((d) => d.id === phone.deviceId)?.name,
  })
})

test('2 next-track advance with the screen off, and plays recorded', async ({ phone, server }) => {
  const page0 = phone.page
  const ids = await server.trackIds()
  await signIn(phone)
  // A log of every media event and visibility change, kept in the page.
  await page0.evaluate(() => {
    const log: Array<[number, string, string, number, string]> = []
    ;(window as unknown as { __mediaLog: typeof log }).__mediaLog = log
    for (const type of ['timeupdate', 'ended', 'play', 'pause', 'playing', 'waiting', 'stalled']) {
      document.addEventListener(type, (e) => {
        const el = e.target as HTMLMediaElement
        if (!el.dataset?.playerElement) return
        log.push([Date.now(), type, el.dataset.playerElement, el.currentTime, document.visibilityState])
      }, true)
    }
    document.addEventListener('visibilitychange', () => log.push([Date.now(), 'visibility', '', 0, document.visibilityState]))
  })
  const postedAt: Array<{ id: number; t: number }> = []
  page0.on('request', (req) => {
    const m = /\/api\/stream\/(\d+)\/played$/.exec(new URL(req.url()).pathname)
    if (m && req.method() === 'POST') postedAt.push({ id: Number(m[1]), t: Date.now() })
  })

  await tapPlayAlbum(phone, ARTIST, 'Gapless Album', 'Gap One')
  await expect.poll(() => playingTime(page0)).toBeGreaterThan(1)
  await expect.poll(() => activeDevice(server)).toBe(phone.deviceId)

  // A real phone locks when its screen goes off; that lock screen is what
  // stops Chrome's activity and hides the page. The emulator image has the
  // lock screen disabled, so it is turned on (swipe, no PIN) for this test.
  const lockWasDisabled = shell('locksettings get-disabled').trim() === 'true'
  // Playwright's DevTools session makes Chrome emulate a focused, visible
  // page; with it attached the page never goes hidden. Detached, the phone is
  // on its own while the screen is off, like a real one in a pocket.
  if (phone.canDetach) await phone.detach()
  note('DevTools detached while the screen is off', phone.canDetach)
  if (lockWasDisabled) shell('locksettings set-disabled false')
  const tSleep = Date.now()
  screenOff()
  await expect.poll(() => wakefulness(), { timeout: 10_000 }).not.toBe('Awake')
  note('wakefulness after KEYCODE_SLEEP', wakefulness())
  await expect.poll(() => /isKeyguardShowing=true/.test(shell('dumpsys window')), { timeout: 10_000 }).toBe(true).catch(() => undefined)
  note('while off', {
    keyguardShowing: /isKeyguardShowing=true/.test(shell('dumpsys window')),
    chromeActivity: chromeActivityState(),
  })

  // While the screen is off, only the server is watched (touching the page
  // over DevTools could keep it awake). Two boundaries: Gap Three current.
  const samples: Array<{ s: number; index: number; playing: boolean; pos: number }> = []
  let reachedThird = false
  const deadline = Date.now() + 45_000
  while (Date.now() < deadline) {
    const s = await server.state()
    samples.push({ s: Math.round((Date.now() - tSleep) / 100) / 10, index: s.queue_index, playing: s.is_playing, pos: Math.round(s.position_seconds * 10) / 10 })
    if (s.queue_index >= 2) {
      reachedThird = true
      break
    }
    await new Promise((r) => setTimeout(r, 1000))
  }
  // Some listening of Gap Three with the screen still off (elapsed time is
  // the thing under test here).
  if (reachedThird) await new Promise((r) => setTimeout(r, 3000))
  const tWake = Date.now()
  const asleepAtEnd = wakefulness()
  screenOn()
  await expect.poll(() => wakefulness(), { timeout: 10_000 }).toBe('Awake')
  if (lockWasDisabled) shell('locksettings set-disabled true')
  shell('wm dismiss-keyguard')
  if (phone.canDetach) await phone.reattach()
  const page = phone.page
  await expect.poll(() => page.evaluate(() => document.visibilityState), { timeout: 15_000 }).toBe('visible')
  note('screen off for (s)', (tWake - tSleep) / 1000)
  note('wakefulness just before waking', asleepAtEnd)
  note('server samples while off (s since sleep, queue index, playing, position)', samples.map((x) => `${x.s}:${x.index}:${x.playing ? 'P' : '-'}:${x.pos}`).join(' '))

  const log = await page.evaluate(() => (window as unknown as { __mediaLog: Array<[number, string, string, number, string]> }).__mediaLog)
  writeFileSync(test.info().outputPath('media-log.json'), JSON.stringify(log))
  const off = log.filter(([t]) => t >= tSleep && t <= tWake)
  const updates = off.filter(([, type]) => type === 'timeupdate')
  const gaps: number[] = []
  for (let i = 1; i < updates.length; i += 1) gaps.push(updates[i][0] - updates[i - 1][0])
  const sorted = [...gaps].sort((a, b) => a - b)
  const maxGapMs = sorted.at(-1) ?? null
  // The largest currentTime step between two timeupdates of the same element
  // (what noteListening compares with LISTEN_MAX_STEP_SECONDS).
  let maxStep = 0
  const lastByRole = new Map<string, number>()
  for (const [, type, role, time] of off) {
    if (type !== 'timeupdate') continue
    const prev = lastByRole.get(role)
    if (prev != null && time > prev) maxStep = Math.max(maxStep, time - prev)
    lastByRole.set(role, time)
  }
  note('timeupdate while screen off', {
    count: updates.length,
    maxGapMs,
    medianGapMs: sorted.length ? sorted[Math.floor(sorted.length / 2)] : null,
    gapsOver2s: gaps.filter((g) => g > 2000).length,
    maxCurrentTimeStepS: Math.round(maxStep * 100) / 100,
    visibilityStates: [...new Set(off.map((e) => e[4]))],
    otherEvents: off.filter(([, type]) => type !== 'timeupdate').map(([t, type, role, time]) => `${((t - tSleep) / 1000).toFixed(1)}s ${type} ${role} ${time.toFixed(1)}`),
  })

  // Playback advanced (server) and the phone is on (at least) Gap Three.
  const after = await server.state()
  note('server after wake', { queue_index: after.queue_index, media_item_id: after.media_item_id, is_playing: after.is_playing })
  expect(reachedThird, 'two track boundaries passed while the screen was off').toBe(true)
  expect(after.queue_index).toBeGreaterThanOrEqual(2)

  // Each fully played track recorded exactly once.
  const full = [ids['Gap One'], ids['Gap Two']]
  const reported = () => server.playCounts(full)
  await expect.poll(() => Object.values(reported()).every((c) => c >= 1), { timeout: 20_000 }).toBe(true).catch(() => undefined)
  const counts = reported()
  // POSTs made while DevTools was detached are not seen here; the database is the record.
  note('play reports (POSTs, s since sleep)', postedAt.map((p) => `${p.id}@${((p.t - tSleep) / 1000).toFixed(1)}`).join(' ') || 'none')
  note('play_history rows', { 'Gap One': counts[ids['Gap One']], 'Gap Two': counts[ids['Gap Two']] })
  note('recently played', await server.recentlyPlayedTitles())
  expect.soft(counts[ids['Gap One']], 'Gap One recorded once').toBe(1)
  expect.soft(counts[ids['Gap Two']], 'Gap Two recorded once').toBe(1)
})

test('3 a play past half is recorded once, an early skip is not', async ({ phone, server }) => {
  const { page } = phone
  const ids = await server.trackIds()
  await signIn(phone)
  await tapPlayAlbum(phone, ARTIST, 'Threshold Album', 'Half One')
  const one = ids['Half One']
  const two = ids['Half Two']
  await expect.poll(() => phone.played.includes(one), { timeout: 30_000 }).toBe(true)
  const atReport = await playingTime(page)
  note('Half One position when its play was reported (s; threshold 12)', atReport)
  expect(atReport ?? 0).toBeGreaterThanOrEqual(11.5)

  await phone.tap(next(page))
  await expect(trackTitle(page)).toContainText('Half Two')
  await expect.poll(() => playingTime(page)).toBeGreaterThan(2)
  const skippedAt = await playingTime(page)
  await phone.tap(next(page))
  await expect(trackTitle(page)).toContainText('Half Three')
  await expect.poll(() => playingTime(page)).toBeGreaterThan(1)
  note('Half Two skipped at (s)', skippedAt)
  // Absence over time is what is checked: a late report would arrive here.
  await page.waitForTimeout(4000)
  const counts = server.playCounts([one, two])
  note('play_history rows', { 'Half One': counts[one], 'Half Two': counts[two] })
  note('POSTed plays', phone.played)
  expect(counts[one]).toBe(1)
  expect(counts[two]).toBe(0)
  expect(phone.played.filter((id) => id === one)).toHaveLength(1)
  expect(phone.played).not.toContain(two)
})

test('4 the phone claims playback from a desktop that went away', async ({ phone, server, browser }) => {
  const { page } = phone
  await signIn(phone)
  const desk = await openDevice(browser, 'Chrome')
  await playAlbum(desk)
  await expect.poll(() => activeDevice(server)).toBe(desk.id)
  await desk.context.close()
  await expect.poll(async () => (await server.state()).devices.find((d) => d.id === desk.id)?.is_connected).toBe(false)
  note('desktop', { id: desk.id, name: desk.name, closedWhilePlaying: true })

  await tapPlayAlbum(phone, 'Real Artist', 'Real Album', 'One')
  const t0 = (await playingTime(page)) ?? 0
  await expect.poll(() => playingTime(page)).toBeGreaterThan(t0 + 0.5)
  await expect.poll(() => activeDevice(server)).toBe(phone.deviceId)
  await expect(page.locator('.player-remote-note')).toHaveCount(0)
  note('evidence', { phonePlayingTime: await playingTime(page), active: await activeDevice(server), phone: phone.deviceId })
})

test('5 a phone that played on offline goes quiet when it reconnects after another device took over', async ({ phone, server, browser }) => {
  const { page } = phone
  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) note('phone page navigated', frame.url())
  })
  page.on('console', (msg) => {
    if (/vite|socket|offline|online/i.test(msg.text())) note('phone console', msg.text().slice(0, 200))
  })
  await signIn(phone)
  await tapPlayAlbum(phone, 'Real Artist', 'Real Album', 'One')
  await expect.poll(() => activeDevice(server)).toBe(phone.deviceId)
  await expect.poll(() => phoneConnected(server, phone)).toBe(true)

  // Off the network: radios off first (what a user does; navigator.onLine
  // goes false), but the page comes through `adb reverse` over the adb link,
  // which the radios do not touch - so the relay in net-proxy.ts then cuts
  // every connection, like a network that went away. The server telling the
  // device is disconnected is the proof the socket really dropped.
  const dropped = async (ms: number) => {
    try {
      await expect.poll(() => phoneConnected(server, phone), { timeout: ms }).toBe(false)
      return true
    } catch {
      return false
    }
  }
  shell('svc wifi disable')
  shell('svc data disable')
  const byRadios = await dropped(10_000)
  note('navigator.onLine after radios off', await page.evaluate(() => navigator.onLine))
  note('socket dropped by radios off alone (10 s)', byRadios)
  const cut = await setPhoneNetwork(false)
  note('relay cut', cut)
  const gone = byRadios || (await dropped(30_000))
  const restore = async () => {
    shell('svc wifi enable')
    shell('svc data enable')
    await setPhoneNetwork(true)
  }
  note('offline method', `svc wifi/data disable + relay cut; socket dropped: ${gone}`)
  try {
    expect(gone, 'the phone\'s playback socket dropped').toBe(true)
    // Still playing, offline - on track One, or already on Two: a track that
    // ends offline moves on to the next one's in-memory copy (where One ends
    // depends on how long the drop took). Polled, as the moment between two
    // tracks has no element playing.
    await expect.poll(() => playingTime(page)).not.toBeNull()
    note('phone playing while offline at (s)', { time: await playingTime(page), track: await trackTitle(page).textContent() })

    const desk = await openDevice(browser, 'Edge')
    try {
      await playAlbum(desk)
      await expect.poll(() => activeDevice(server)).toBe(desk.id)
      await expect.poll(() => playingTime(page)).not.toBeNull() // still offline, still playing
      const tOnline = Date.now()
      await restore()
      await expect.poll(() => phoneConnected(server, phone), { timeout: 60_000 }).toBe(true)
      await expect.poll(() => isSilent(page), { timeout: 30_000 }).toBe(true)
      note('phone silent after reconnect (s)', (Date.now() - tOnline) / 1000)
      await expect(page.locator('.player-remote-note')).toContainText(`playing on ${desk.name}`)
      expect(await desktopSilent(desk.page)).toBe(false)
      await expect(remoteNote(desk.page)).toHaveCount(0)
      note('evidence', { active: await activeDevice(server), desktop: desk.id, phoneElements: await musicElements(page) })
    } finally {
      await desk.context.close()
    }
  } finally {
    await restore().catch(() => undefined)
    // Let Android finish bringing Wi-Fi back before the next test starts
    // Chrome (a network change mid-load leaves a dev-server page blank).
    await expect.poll(() => /ni\{WIFI CONNECTED/.test(shell('dumpsys connectivity')), { timeout: 30_000 }).toBe(true).catch(() => undefined)
  }
})

test('6 Android Back closes a dialog and Now Playing; the bottom tab bar has 44 px targets and More closes on Back', async ({ phone }) => {
  const { page } = phone
  await signIn(phone)
  await page.goto(`${PHONE_ORIGIN}/#/songs`)
  const more = page.getByRole('button', { name: 'More actions for One', exact: true })
  await expect(more).toBeVisible()
  await phone.tap(more)
  const info = page.getByRole('menuitem', { name: 'Info', exact: true })
  await expect(info).toBeVisible()
  await phone.tap(info)
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  const songsUrl = `${PHONE_ORIGIN}/#/songs`
  back()
  await expect(dialog).toHaveCount(0)
  await expect(page.getByRole('navigation').first()).toBeVisible()
  expect(page.url()).toBe(songsUrl)
  note('after Back on the Info dialog', page.url())

  await tapPlayAlbum(phone, 'Real Artist', 'Real Album', 'One')
  const albumUrl = page.url()
  await phone.tap(playerBar(page).getByRole('button', { name: 'Open now playing' }))
  const close = page.locator('.nowplaying-close')
  await expect(close).toBeVisible()
  back()
  await expect(close).toHaveCount(0)
  await expect(playerBar(page)).toBeVisible()
  expect(page.url()).toBe(albumUrl)
  note('after Back on Now Playing', page.url())

  // The bottom tab bar: Home, three tabs and More, all on screen with a
  // 44 px target each (no sideways scrolling); the rest open from More.
  const bar = page.getByRole('navigation', { name: 'Quick tab navigation' })
  await expect(bar).toBeVisible()
  const boxes = await bar.locator('button').evaluateAll((els) => els.map((el) => {
    const r = el.getBoundingClientRect()
    return { label: el.textContent, w: Math.round(r.width * 10) / 10, h: Math.round(r.height * 10) / 10 }
  }))
  note('tab targets (css px)', boxes)
  expect(boxes).toHaveLength(5)
  for (const b of boxes) {
    expect(b.w, `${b.label} width`).toBeGreaterThanOrEqual(44)
    expect(b.h, `${b.label} height`).toBeGreaterThanOrEqual(44)
  }
  expect(await bar.evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBe(true)
  await phone.tap(bar.getByRole('button', { name: 'More' }))
  const sheet = page.getByRole('dialog', { name: 'More' })
  await expect(sheet).toBeVisible()
  back()
  await expect(sheet).toHaveCount(0)
  expect(page.url()).toBe(albumUrl)
  note('after Back on the More sheet', page.url())
})

test('7 pull to refresh on the Songs list reloads it', async ({ phone }) => {
  const { page } = phone
  await signIn(phone)
  await page.goto(`${PHONE_ORIGIN}/#/songs`)
  await expect(page.getByRole('button', { name: 'More actions for One', exact: true })).toBeVisible()
  let listRequests = 0
  let documentRequests = 0
  page.on('request', (req) => {
    const url = new URL(req.url())
    if (url.pathname === '/api/library/tracks') listRequests += 1
    if (req.resourceType() === 'document') documentRequests += 1
  })
  const main = page.locator('main.app-main')
  expect(await main.evaluate((el) => el.scrollTop)).toBe(0)
  const box = (await main.boundingBox())!
  const x = box.x + box.width / 2
  const y = box.y + 40
  await phone.swipe({ x, y }, { x, y: y + 320 }, 800)
  await expect.poll(() => documentRequests, { timeout: 15_000 }).toBeGreaterThan(0)
  await expect.poll(() => listRequests, { timeout: 20_000 }).toBeGreaterThan(0)
  await expect(page.getByRole('button', { name: 'More actions for One', exact: true })).toBeVisible({ timeout: 30_000 })
  note('requests after the pull', { document: documentRequests, '/api/library/tracks': listRequests })
})

test('8 layout at the phone viewport: no sideways scroll, player bar inside', async ({ phone }) => {
  const { page } = phone
  await signIn(phone)
  await tapPlayAlbum(phone, 'Real Artist', 'Real Album', 'One')
  const probe = () => page.evaluate(() => {
    const doc = document.documentElement
    const vw = window.innerWidth
    const offenders: string[] = []
    for (const el of document.querySelectorAll('body *')) {
      const r = el.getBoundingClientRect()
      if (r.width === 0 || r.right <= vw + 1) continue
      // Inside something that scrolls sideways on purpose (or clips): fine.
      let clipped = false
      for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
        const ox = getComputedStyle(p).overflowX
        if (ox !== 'visible') { clipped = true; break }
      }
      if (!clipped) offenders.push(`${el.tagName.toLowerCase()}.${String(el.className).split(' ')[0]} right=${Math.round(r.right)}`)
      if (offenders.length >= 5) break
    }
    const safe = document.createElement('div')
    safe.style.cssText = 'position:fixed;padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)'
    document.body.appendChild(safe)
    const cs = getComputedStyle(safe)
    const insets = [cs.paddingTop, cs.paddingRight, cs.paddingBottom, cs.paddingLeft].join(' ')
    safe.remove()
    const bar = document.querySelector('.player-bar')?.getBoundingClientRect()
    return {
      innerWidth: vw,
      innerHeight: window.innerHeight,
      docScrollWidth: doc.scrollWidth,
      bodyScrollWidth: document.body.scrollWidth,
      scrollX: window.scrollX,
      offenders,
      safeAreaInsets: insets,
      playerBar: bar ? { left: bar.left, top: Math.round(bar.top), right: bar.right, bottom: Math.round(bar.bottom) } : null,
    }
  })
  const views: Array<[string, string]> = [['home', '#/home'], ['songs', '#/songs'], ['settings', '#/settings']]
  const results: Record<string, Awaited<ReturnType<typeof probe>>> = {}
  for (const [name, hash] of views) {
    await page.goto(`${PHONE_ORIGIN}/${hash}`)
    await expect(page.getByRole('navigation').first()).toBeVisible()
    await page.waitForLoadState('networkidle').catch(() => undefined)
    results[name] = await probe()
    await page.screenshot({ path: shotPath(`layout-${name}`) })
  }
  await phone.tap(playerBar(page).getByRole('button', { name: 'Open now playing' }))
  await expect(page.locator('.nowplaying-close')).toBeVisible()
  results['now-playing'] = await probe()
  await page.screenshot({ path: shotPath('layout-now-playing') })
  note('layout', results)
  note('screenshots', ['home', 'songs', 'settings', 'now-playing'].map((n) => shotPath(`layout-${n}`)))
  for (const [name, r] of Object.entries(results)) {
    expect.soft(r.docScrollWidth, `${name}: page scroll width`).toBeLessThanOrEqual(r.innerWidth)
    expect.soft(r.bodyScrollWidth, `${name}: body scroll width`).toBeLessThanOrEqual(r.innerWidth)
    expect.soft(r.offenders, `${name}: elements past the right edge`).toEqual([])
    if (name !== 'now-playing') {
      expect.soft(r.playerBar, `${name}: player bar`).not.toBeNull()
      if (r.playerBar) {
        expect.soft(r.playerBar.left, `${name}: player bar left`).toBeGreaterThanOrEqual(0)
        // innerWidth is rounded (412); the real layout width is 1080 / 2.625 = 411.43.
        expect.soft(r.playerBar.right, `${name}: player bar right`).toBeLessThanOrEqual(r.innerWidth + 1)
        expect.soft(r.playerBar.bottom, `${name}: player bar bottom`).toBeLessThanOrEqual(r.innerHeight + 1)
      }
    }
  }
})

test('9 installable: manifest, icons and an activated service worker', async ({ phone }) => {
  const { page, context } = phone
  await signIn(phone)
  const assets = await page.evaluate(async () => {
    const link = document.querySelector<HTMLLinkElement>('link[rel="manifest"]')?.href ?? null
    const res = await fetch('/manifest.json', { cache: 'no-store' })
    const manifest = (await res.json()) as { icons: Array<{ src: string; sizes: string; purpose?: string }>; display?: string; start_url?: string }
    const icons = []
    for (const icon of manifest.icons) {
      const r = await fetch(icon.src, { cache: 'no-store' })
      let size = ''
      if (r.ok && icon.src.endsWith('.png')) {
        const bitmap = await createImageBitmap(await r.blob())
        size = `${bitmap.width}x${bitmap.height}`
      }
      icons.push({ src: icon.src, purpose: icon.purpose ?? 'any', declared: icon.sizes, status: r.status, type: r.headers.get('content-type'), size })
    }
    return { link, manifestStatus: res.status, manifestType: res.headers.get('content-type'), display: manifest.display, start_url: manifest.start_url, icons }
  })
  note('manifest', assets)
  expect(assets.link).toBe(`${PHONE_ORIGIN}/manifest.json`)
  expect(assets.manifestStatus).toBe(200)
  for (const icon of assets.icons) {
    expect.soft(icon.status, icon.src).toBe(200)
    if (icon.src.endsWith('.png')) expect.soft(icon.size, icon.src).toBe(icon.declared)
  }
  const want = ['192x192:any', '512x512:any', '192x192:maskable', '512x512:maskable']
  expect(assets.icons.map((i) => `${i.declared}:${i.purpose}`)).toEqual(expect.arrayContaining(want))

  const sw = await page.evaluate(async () => {
    const reg = await Promise.race([
      navigator.serviceWorker.ready,
      new Promise<null>((r) => setTimeout(() => r(null), 30_000)),
    ])
    if (!reg) return null
    const worker = reg.active
    if (worker && worker.state !== 'activated') {
      await new Promise<void>((r) => worker.addEventListener('statechange', () => worker.state === 'activated' && r()))
    }
    return { scope: reg.scope, script: reg.active?.scriptURL ?? null, state: reg.active?.state ?? null, controlled: !!navigator.serviceWorker.controller }
  })
  note('service worker', sw)
  expect(sw?.state).toBe('activated')

  // Chrome's own verdict (DevTools); informational on Chrome 124.
  const cdp = await context.newCDPSession(page)
  try {
    const result = (await cdp.send('Page.getInstallabilityErrors')) as { installabilityErrors: Array<{ errorId: string }> }
    note('Chrome installability errors', result.installabilityErrors.map((e) => e.errorId))
  } catch (error) {
    note('Chrome installability errors', `not available: ${String(error).slice(0, 120)}`)
  } finally {
    await cdp.detach()
  }
  // Leave no worker behind for the next run's pages.
  await page.evaluate(async () => {
    for (const reg of await navigator.serviceWorker.getRegistrations()) await reg.unregister()
  })
})
