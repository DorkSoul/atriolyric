import { execFileSync } from 'node:child_process'
import { mkdirSync, readFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { join } from 'node:path'
import { _android as android, type AndroidDevice, type Page } from 'playwright/test'
import { DATA_DIR } from '../real/env'
import { SERIAL, shell } from '../android/adb'

/** The Android app (mobile/): its package, and the APK the suite installs. */
export const PACKAGE = 'io.github.dorksoul.atriolyric'
export const APK = process.env.ATRIOLYRIC_APK ?? join(process.cwd(), '..', 'mobile', 'android', 'app', 'build', 'outputs', 'apk', 'debug', 'app-debug.apk')
/** The page bundled in the APK (set-up and offline view). */
export const SHELL_ORIGIN = 'https://localhost'

/** A third library root with one 720p clip, so a 480p download rendition exists. */
export const APP_VIDEO_DIR = join(DATA_DIR, 'app-videos')
export const APP_VIDEO_TITLE = 'App Clip'
export const APP_VIDEO_SECONDS = 12

export function makeAppVideo(): void {
  mkdirSync(APP_VIDEO_DIR, { recursive: true })
  execFileSync('ffmpeg', [
    '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
    '-f', 'lavfi', '-i', `testsrc=size=1280x720:rate=25:duration=${APP_VIDEO_SECONDS}`,
    '-f', 'lavfi', '-i', `sine=frequency=440:sample_rate=48000:duration=${APP_VIDEO_SECONDS}`,
    '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-g', '25',
    '-c:a', 'aac', '-b:a', '96k', '-ac', '2',
    join(APP_VIDEO_DIR, `${APP_VIDEO_TITLE}.mp4`),
  ], { stdio: 'inherit' })
}

/** A local endless MP3 stream for a test radio station (the backend relays it). */
export const RADIO_PORT = 8399
export const RADIO_STATION = 'App Test Radio'

export function makeRadioClip(): Buffer {
  const path = join(DATA_DIR, 'radio-clip.mp3')
  execFileSync('ffmpeg', [
    '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
    '-f', 'lavfi', '-i', 'sine=frequency=330:sample_rate=44100:duration=4',
    '-c:a', 'libmp3lame', '-b:a', '64k', '-ac', '1', '-write_xing', '0', '-id3v2_version', '0',
    path,
  ], { stdio: 'inherit' })
  return readFileSync(path)
}

/** Serves `clip` over and over at about its own bitrate, like a live station. */
export function startRadio(clip: Buffer): Promise<() => Promise<void>> {
  const server = createServer((_request, response) => {
    response.writeHead(200, { 'Content-Type': 'audio/mpeg', 'Cache-Control': 'no-store' })
    let offset = 0
    const chunk = 4000 // half a second at 64 kbps
    const send = () => {
      const end = Math.min(clip.length, offset + chunk)
      response.write(clip.subarray(offset, end))
      offset = end >= clip.length ? 0 : end
    }
    // A few seconds at once so playback starts promptly, then real time.
    for (let i = 0; i < 8; i += 1) send()
    const timer = setInterval(send, 500)
    response.on('close', () => clearInterval(timer))
  })
  return new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(RADIO_PORT, '127.0.0.1', () => {
      resolve(() => new Promise<void>((done) => {
        server.closeAllConnections()
        server.close(() => done())
      }))
    })
  })
}

/** Swipes the app away from Recents: its task is removed, as the system does it. */
export function swipeAway(): void {
  const tasks = shell('am stack list')
  const id = new RegExp(`taskId=(\\d+): ${PACKAGE.replace(/\./g, '\\.')}/`).exec(tasks)?.[1]
  if (!id) throw new Error(`no task for ${PACKAGE} in:\n${tasks}`)
  shell(`am stack remove ${id}`)
}

/** Where the test backend looks for an Android app to offer (see playwright.app.config.ts). */
export const OFFERED_APK = join(DATA_DIR, 'offered', 'atriolyric.apk')

/** The activity on top of the screen, as "package/class". */
export function topActivity(): string {
  return /topResumedActivity=ActivityRecord\{\S+ \S+ (\S+)/.exec(shell('dumpsys activity activities'))?.[1] ?? ''
}

export function startApp(): void {
  shell(`am start -n ${PACKAGE}/.MainActivity`)
}

export function stopApp(): void {
  shell(`am force-stop ${PACKAGE}`)
}

let device: AndroidDevice | null = null

/** Drop the DevTools connection; the app carries on. While Playwright is
 * attached the page is kept "focused", which is not what a locked phone is. */
export async function detach(): Promise<void> {
  await device?.close().catch(() => undefined)
  device = null
}

/** The app's WebView page whose address passes `match`, over DevTools. The
 * app swaps its page (bundled shell <-> the server's), so this is called
 * again after each swap. */
export async function attach(match: (url: string) => boolean, timeoutMs = 45_000): Promise<Page> {
  const deadline = Date.now() + timeoutMs
  let last = 'the app is not running'
  for (;;) {
    await detach()
    try {
      const found = (await android.devices({ omitDriverInstall: true })).find((d) => d.serial() === SERIAL)
      if (!found) throw new Error(`Playwright does not see ${SERIAL}`)
      device = found
      const webview = await found.webView({ pkg: PACKAGE }, { timeout: 5_000 })
      const first = await webview.page()
      const pages = first.context().pages()
      const page = pages.find((p) => match(p.url()))
      if (page) {
        page.setDefaultTimeout(20_000)
        return page
      }
      last = `pages: ${pages.map((p) => p.url()).join(', ') || 'none'}`
    } catch (error) {
      last = String(error).split('\n')[0]
    }
    if (Date.now() > deadline) throw new Error(`no app page matched within ${timeoutMs} ms (${last})`)
    await new Promise((r) => setTimeout(r, 750))
  }
}

/** Calls the app's native plugin from its page. */
export function native<T>(page: Page, method: string, options: Record<string, unknown> = {}): Promise<T> {
  return page.evaluate(
    ([name, opts]) => (window as unknown as { Capacitor: { nativePromise: (p: string, m: string, o: unknown) => Promise<unknown> } })
      .Capacitor.nativePromise('AtrioNative', name as string, opts),
    [method, options] as const,
  ) as Promise<T>
}

export interface Slot {
  slot: string
  /** Which load of the page this state is for: a new one is a reload. */
  loadId: number
  mediaId: number
  playbackState: string
  playing: boolean
  playWhenReady: boolean
  position: number
  duration: number | null
  error: string | null
  active: boolean
  eq: number[] | null
  gainDb: number | null
  limiter: boolean | null
  /** Whether it plays the copy on the device, not a stream. */
  downloaded: boolean
}

export async function slots(page: Page): Promise<Slot[]> {
  return (await native<{ slots: Slot[] }>(page, 'playerState')).slots
}

export async function activeSlot(page: Page): Promise<Slot | null> {
  const state = await native<{ slots: Slot[] }>(page, 'playerState')
  return state.slots.find((slot) => slot.active) ?? null
}

export interface DeviceDownload {
  mediaId: number
  kind: string
  title: string
  quality: string
  qualityLabel: string
  state: string
  bytes: number
}

export async function deviceDownloads(page: Page): Promise<DeviceDownload[]> {
  return (await native<{ items: DeviceDownload[] }>(page, 'downloadList')).items
}

export interface SessionInfo {
  /** PLAYING, PAUSED, BUFFERING, ... as the system reports it; null when the app has no session. */
  state: string | null
  /** "title, artist, album" as the lock screen and notification are given it. */
  description: string
}

/** The app's media session as Android sees it: what the notification, the
 * lock screen and headset buttons control. */
export function mediaSession(): SessionInfo {
  const dump = shell('dumpsys media_session')
  const blocks = dump.split(/\n(?=\s+\S.*\n\s+ownerPid=)/)
  const mine = blocks.find((block) => block.includes(`package=${PACKAGE}`))
  if (!mine) return { state: null, description: '' }
  const names: Record<string, string> = { '0': 'NONE', '1': 'STOPPED', '2': 'PAUSED', '3': 'PLAYING', '6': 'BUFFERING', '7': 'ERROR' }
  const raw = /state=PlaybackState \{state=(\w+)(?:\((\d+)\))?/.exec(mine)
  const state = raw ? (raw[2] ? raw[1] : names[raw[1]] ?? raw[1]) : null
  const description = /metadata:\s*size=\d+,\s*description=(.*)/.exec(mine)?.[1]?.trim() ?? ''
  return { state, description }
}

/** A media button as a headset or the lock screen sends it. */
export function mediaKey(command: 'play' | 'pause' | 'next' | 'previous'): void {
  shell(`cmd media_session dispatch ${command}`)
}

/** Whether the app has a media notification posted. */
export function hasMediaNotification(): boolean {
  const dump = shell('dumpsys notification --noredact')
  return dump.split(/\n(?=\s*NotificationRecord\()/).some((record) => record.includes(`pkg=${PACKAGE}`) && /MediaStyle|category=transport/.test(record))
}

export function setAeroplaneMode(on: boolean): void {
  shell(`cmd connectivity airplane-mode ${on ? 'enable' : 'disable'}`)
}
