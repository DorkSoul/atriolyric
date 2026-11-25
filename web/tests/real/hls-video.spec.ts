import { existsSync, readdirSync, readFileSync, unlinkSync } from 'node:fs'
import { createRequire } from 'node:module'
import { join } from 'node:path'
import { expect, test, type Page } from 'playwright/test'
import { openDevice, playerBar, trackTitle, type Device } from './devices'
import { BACKEND_LOG, DATA_DIR, VIEWER_STORAGE_STATE } from './env'
import { CUT_CLAIMED_SECONDS, CUT_TITLE, VIDEO_SECONDS, VIDEO_TITLE } from './make-video'

// A video no browser plays natively (MPEG-2 + MP2 in MKV, see make-video.ts)
// through the server's on-demand HLS transcode and the real hls.js, which the
// app loads lazily. Real web app, real backend, real ffmpeg. One device, one
// viewing, by its own user: the steps run in order on the same page.
test.describe.configure({ mode: 'serial' })

const HLS_VERSION = (createRequire(import.meta.url)('hls.js/package.json') as { version: string }).version
const SEGMENT_SECONDS = 6
const SEGMENTS = Math.ceil(VIDEO_SECONDS / SEGMENT_SECONDS)

interface SegmentResponse {
  item: number
  index: number
  status: number
  bytes: number
}

interface VideoState {
  time: number
  readyState: number
  paused: boolean
  ended: boolean
  duration: number
  buffered: Array<[number, number]>
  endedEvents: number
  /** currentTime when the last `ended` fired (the app moves on right after). */
  endedAt: number | null
}

let device: Device
let page: Page
let videoId: number
let cutId: number
const segments: SegmentResponse[] = []
/** The segment responses for one video. */
const segmentsOf = (item: number) => segments.filter((s) => s.item === item)
const hlsChunks: Array<{ url: string; hasVersion: boolean }> = []

async function videoState(): Promise<VideoState> {
  return page.evaluate(() => {
    const video = document.querySelector('video[data-player-element="video"]') as HTMLVideoElement
    const buffered: Array<[number, number]> = []
    for (let i = 0; i < video.buffered.length; i++) buffered.push([video.buffered.start(i), video.buffered.end(i)])
    return {
      time: video.currentTime,
      readyState: video.readyState,
      paused: video.paused,
      ended: video.ended,
      duration: video.duration,
      buffered,
      endedEvents: (window as Window & { __videoEnded?: number }).__videoEnded ?? 0,
      endedAt: (window as Window & { __videoEndedAt?: number }).__videoEndedAt ?? null,
    }
  })
}

const videoTime = async () => (await videoState()).time

/** Every toast shown since the page loaded (they disappear on their own). */
async function toastsSeen(): Promise<string[]> {
  return page.evaluate(() => (window as Window & { __toasts?: string[] }).__toasts ?? [])
}

/** hls.js errors the app reported as fatal (it reports every hls.js error). */
function fatalErrors(): Array<Record<string, unknown>> {
  return device.diagnostics.filter((d) => d.event === 'media_error' && d.fatal === true)
}

/** Click the player bar's seek slider where `seconds` is. */
async function seekTo(seconds: number): Promise<void> {
  const slider = playerBar(page).getByRole('slider', { name: 'Seek' })
  await expect(slider).toHaveAttribute('aria-valuemax', String(VIDEO_SECONDS))
  const box = await slider.boundingBox()
  expect(box).not.toBeNull()
  await slider.click({ position: { x: (box!.width * seconds) / VIDEO_SECONDS, y: box!.height / 2 } })
}

/** After a seek: playing again near `target` (within 1.5 s) and advancing.
 * Returns how long that took, for the report. */
async function expectResumedNear(target: number, startedAt: number): Promise<number> {
  await expect.poll(async () => {
    const s = await videoState()
    return !s.paused && s.readyState >= 3 && Math.abs(s.time - target) <= 1.5
  }, { timeout: 15_000 }).toBe(true)
  const first = await videoTime()
  await expect.poll(videoTime, { timeout: 10_000 }).toBeGreaterThan(first + 0.5)
  const elapsed = Date.now() - startedAt
  // Played on from the target, not from somewhere else.
  expect(Math.abs((await videoTime()) - target)).toBeLessThan(4)
  return elapsed
}

function noErrorShown(toasts: string[]): void {
  expect(toasts.filter((t) => t.includes('toast-error') || t.includes("Couldn't play"))).toEqual([])
  expect(fatalErrors()).toEqual([])
}

/** The session directory the server encodes this viewing's segments into
 * (settings.hls_dir / "<item>_<user>", the audio-only one has a suffix). */
function sessionDir(): string | null {
  const root = join(DATA_DIR, 'hls')
  if (!existsSync(root)) return null
  const dir = readdirSync(root).find((name) => /^\d+_\d+$/.test(name) && name.startsWith(`${videoId}_`))
  return dir ? join(root, dir) : null
}

function encoderStarts(): number[] {
  const log = readFileSync(BACKEND_LOG, 'utf8')
  return [...log.matchAll(new RegExp(`HLS encoder start: item=${videoId} index=(\\d+) `, 'g'))].map((m) => Number(m[1]))
}

test.beforeAll(async ({ browser }) => {
  // Its own account: the two-device scenarios may run at the same time.
  device = await openDevice(browser, 'Chrome', VIEWER_STORAGE_STATE)
  page = device.page
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (request.resourceType() === 'script' && /hls/i.test(url.pathname) && !url.pathname.startsWith('/api/')) {
      // The lazy chunk (Vite's pre-bundled dependency in dev, an assets/
      // chunk in a build): read it back to see which hls.js it is.
      void request.response().then(async (response) => {
        const text = (await response?.text().catch(() => '')) ?? ''
        hlsChunks.push({ url: url.pathname, hasVersion: text.includes(`"${HLS_VERSION}"`) })
      })
    }
  })
  page.on('response', (response) => {
    const match = /\/api\/stream\/(\d+)\/hls\/(\d+)\.ts$/.exec(new URL(response.url()).pathname)
    if (!match) return
    const [item, index] = [Number(match[1]), Number(match[2])]
    void response.body().then(
      (body) => segments.push({ item, index, status: response.status(), bytes: body.length }),
      () => segments.push({ item, index, status: response.status(), bytes: -1 }),
    )
  })
  // Toasts and `ended` events, kept from now on (both are gone by the time a
  // later check would look).
  await page.evaluate(() => {
    const w = window as Window & { __toasts?: string[]; __videoEnded?: number; __videoEndedAt?: number }
    w.__toasts = []
    w.__videoEnded = 0
    const record = () => {
      for (const toast of document.querySelectorAll('.toast')) {
        const text = `${toast.className}|${toast.textContent ?? ''}`
        if (!w.__toasts!.includes(text)) w.__toasts!.push(text)
      }
    }
    new MutationObserver(record).observe(document.body, { subtree: true, childList: true, characterData: true })
    const video = document.querySelector('video[data-player-element="video"]') as HTMLVideoElement
    video.addEventListener('ended', () => {
      w.__videoEnded = (w.__videoEnded ?? 0) + 1
      w.__videoEndedAt = video.currentTime
    })
  })
  const videos = (await (await device.context.request.get('/api/library/videos?dir=')).json()) as Array<{ id: number; title: string }>
  videoId = videos.find((v) => v.title === VIDEO_TITLE)!.id
  cutId = videos.find((v) => v.title === CUT_TITLE)!.id
})

test.afterAll(async () => {
  await device?.context.close()
})

test('a video browsers cannot play starts through the server transcode and the real hls.js', async () => {
  await page.goto('/#/videos')
  await page.getByRole('button', { name: `Play ${VIDEO_TITLE}` }).click()

  await expect.poll(videoTime, { timeout: 20_000 }).toBeGreaterThan(2)
  const state = await videoState()
  expect(state.readyState).toBeGreaterThanOrEqual(3)
  expect(state.paused).toBe(false)
  // The playlist's length (12 x 6 s), not a guess from the first segment.
  expect(state.duration).toBeCloseTo(VIDEO_SECONDS, 0)

  // The lazily loaded chunk was requested, and it is this hls.js.
  await expect.poll(() => hlsChunks.length).toBeGreaterThan(0)
  expect(hlsChunks.some((c) => c.hasVersion), JSON.stringify(hlsChunks)).toBe(true)
  // Segments came from the transcoder, not the file itself.
  await expect.poll(() => segmentsOf(videoId).filter((s) => s.status === 200).length).toBeGreaterThan(0)
  expect(segmentsOf(videoId).every((s) => s.status !== 200 || s.bytes > 0), JSON.stringify(segmentsOf(videoId))).toBe(true)
  expect(encoderStarts()[0]).toBe(0)
  noErrorShown(await toastsSeen())
})

test('a seek beyond what is buffered restarts the encoder there and plays on from the target', async () => {
  // This tiny video is encoded faster than it plays, so on any machine the
  // first run has already finished every segment: drop the ones from 48 s on
  // (as the cache's size cap would) so the seek has to restart the encoder at
  // its segment, which is the path a real long video takes.
  const dir = sessionDir()
  expect(dir).not.toBeNull()
  await expect.poll(() => existsSync(join(dir!, `${SEGMENTS - 1}.ts`)), { timeout: 20_000 }).toBe(true)
  const target = 50
  const targetSegment = Math.floor(target / SEGMENT_SECONDS)
  for (let index = targetSegment; index < SEGMENTS; index++) unlinkSync(join(dir!, `${index}.ts`))
  // Nothing at or past the target has been fetched or buffered yet.
  expect(segmentsOf(videoId).filter((s) => s.index >= targetSegment)).toEqual([])
  const before = await videoState()
  expect(before.buffered.every(([, end]) => end < target - 1), JSON.stringify(before.buffered)).toBe(true)
  const startsBefore = encoderStarts().length

  const startedAt = Date.now()
  await seekTo(target)
  const elapsed = await expectResumedNear(target, startedAt)
  console.log(`hls-video: playing on from 50 s ${elapsed} ms after the seek`)
  expect(elapsed).toBeLessThan(10_000)

  expect(encoderStarts().slice(startsBefore)).toContain(targetSegment)
  const fetched = segmentsOf(videoId).find((s) => s.index === targetSegment)
  expect(fetched?.status).toBe(200)
  expect(fetched?.bytes).toBeGreaterThan(0)
  noErrorShown(await toastsSeen())
})

test('a seek back to the start plays on from there', async () => {
  const target = 5
  const startedAt = Date.now()
  await seekTo(target)
  const elapsed = await expectResumedNear(target, startedAt)
  console.log(`hls-video: playing on from 5 s ${elapsed} ms after the seek`)
  expect(elapsed).toBeLessThan(10_000)
  noErrorShown(await toastsSeen())
})

test('the video plays to its end and ends cleanly', async () => {
  const target = VIDEO_SECONDS - 2.5
  await seekTo(target)
  await expect.poll(async () => (await videoState()).endedEvents, { timeout: 20_000 }).toBe(1)
  // It ended at its end, not by giving up somewhere before it.
  expect((await videoState()).endedAt).toBeGreaterThan(VIDEO_SECONDS - 1)

  // The app's end of an item: the server is told and moves on (autoplay
  // continues the one-video queue with music here), the player follows -
  // and nothing restarts the video.
  await expect.poll(async () => {
    const state = (await (await device.context.request.get('/api/playback/state')).json()) as {
      media_item_id: number | null
      is_playing: boolean
    }
    return state.media_item_id !== videoId || !state.is_playing
  }).toBe(true)
  await expect(trackTitle(page)).not.toContainText(VIDEO_TITLE)
  const after = await videoState()
  expect(after.paused).toBe(true)
  expect(after.endedEvents).toBe(1)

  // No segment past the end was asked for, and nothing came back empty. A
  // 404 (the server's answer for a segment past the real end) would not have
  // failed playback either: no fatal error was reported.
  expect(segmentsOf(videoId).every((s) => s.index < SEGMENTS), JSON.stringify(segmentsOf(videoId))).toBe(true)
  expect(segmentsOf(videoId).filter((s) => s.status === 200 && s.bytes <= 0)).toEqual([])
  expect(segmentsOf(videoId).filter((s) => s.status !== 200 && s.status !== 404), JSON.stringify(segmentsOf(videoId))).toEqual([])
  noErrorShown(await toastsSeen())
})

// A 404 for a segment past the real end (the playlist follows the duration
// the header claims) is retried by hls.js until it gives up; left at that,
// the video would sit frozen on its last frame under "Couldn't play" and never
// end, so nothing would play next. The app ends the item where the media ends.
test('a cut-off video plays what it has and ends, though its header promised more', async () => {
  const endedBefore = (await videoState()).endedEvents
  await page.goto('/#/videos')
  await page.getByRole('button', { name: `Play ${CUT_TITLE}` }).click()
  await expect(trackTitle(page)).toContainText(CUT_TITLE)
  await expect.poll(videoTime, { timeout: 20_000 }).toBeGreaterThan(2)
  expect((await videoState()).duration).toBeCloseTo(CUT_CLAIMED_SECONDS, 0)

  // The real picture stops at about 13 s; the playlist goes on to 24 s.
  await expect.poll(async () => (await videoState()).endedEvents, { timeout: 30_000 }).toBe(endedBefore + 1)
  const endedAt = (await videoState()).endedAt!
  expect(endedAt).toBeGreaterThan(10)
  expect(endedAt).toBeLessThan(CUT_CLAIMED_SECONDS - 6)
  await expect(trackTitle(page)).not.toContainText(CUT_TITLE)

  const cut = segmentsOf(cutId)
  expect(cut.filter((s) => s.status === 200 && s.bytes <= 0)).toEqual([])
  // The segment past the end was asked for and refused - once, not retried
  // until hls.js gave up.
  const refused = cut.filter((s) => s.status === 404)
  expect(refused.length, JSON.stringify(cut)).toBeGreaterThanOrEqual(1)
  expect(refused.length, JSON.stringify(cut)).toBeLessThanOrEqual(2)
  noErrorShown(await toastsSeen())
})
