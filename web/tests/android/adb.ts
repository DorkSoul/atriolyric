import { execFileSync } from 'node:child_process'
import { homedir } from 'node:os'
import { join } from 'node:path'

// The SDK the emulator runs from (Android Studio's default location unless
// ANDROID_HOME says otherwise). Playwright and these helpers must talk to
// the same adb server (port 5037), so its platform-tools go first on PATH.
export const ANDROID_HOME = process.env.ANDROID_HOME ?? join(homedir(), 'Android', 'Sdk')
process.env.ANDROID_HOME = ANDROID_HOME
const platformTools = join(ANDROID_HOME, 'platform-tools')
if (!(process.env.PATH ?? '').split(':').includes(platformTools)) {
  process.env.PATH = `${platformTools}:${process.env.PATH ?? ''}`
}

/** The emulator (or phone) the suite drives. */
export const SERIAL = process.env.ANDROID_SERIAL ?? 'emulator-5554'

/** The Vite port the phone reaches through `adb reverse`: the page is then
 * served from http://localhost:<port> on the phone, a secure context. */
export const PHONE_PORT = 3000
export const PHONE_ORIGIN = `http://localhost:${PHONE_PORT}`
/** Host port forwarded to Chrome's DevTools socket on the phone. */
export const CDP_PORT = 9333

/** Where screenshots go: ANDROID_SHOTS_DIR, else test-results/android-shots. */
export const SHOTS_DIR = process.env.ANDROID_SHOTS_DIR ?? join(process.cwd(), 'test-results', 'android-shots')

export function adb(...args: string[]): string {
  // The emulator's adb link can drop for a moment when the host is busy
  // ("device offline"): wait for it to come back and try again.
  for (let attempt = 1; ; attempt += 1) {
    try {
      return execFileSync('adb', ['-s', SERIAL, ...args], { encoding: 'utf8', timeout: 30_000, stdio: ['ignore', 'pipe', 'pipe'] })
    } catch (error) {
      const text = String((error as { stderr?: unknown }).stderr ?? error)
      // Transport errors only (the command never reached the device), so a
      // non-idempotent command (a tap, Back) is never sent twice.
      if (attempt >= 4 || !/offline|not found|closed|protocol fault|failed to (connect|read|get)|no devices|transport/i.test(text)) {
        throw new Error(`adb ${args.join(' ')} failed: ${text.trim()}`)
      }
      console.warn(`[android] adb ${args.join(' ')}: ${text.trim()} - waiting for the device`)
      execFileSync('adb', ['-s', SERIAL, 'wait-for-device'], { timeout: 60_000 })
    }
  }
}

export function shell(command: string): string {
  return adb('shell', command)
}

export function wakefulness(): string {
  return /mWakefulness=(\w+)/.exec(shell('dumpsys power'))?.[1] ?? 'unknown'
}

export function screenOff(): void {
  shell('input keyevent KEYCODE_SLEEP')
}

export function screenOn(): void {
  shell('input keyevent KEYCODE_WAKEUP')
  shell('wm dismiss-keyguard')
}

export function back(): void {
  shell('input keyevent KEYCODE_BACK')
}

/** Phone set-up for a run: awake, unlocked, kept awake while plugged in (so
 * the screen does not time out mid-test), and the phone's localhost:3000
 * forwarded to `hostPort` on this machine. */
export function preparePhone(hostPort: number): void {
  const booted = shell('getprop sys.boot_completed').trim()
  if (booted !== '1') throw new Error(`${SERIAL} is not booted (sys.boot_completed=${booted || 'empty'})`)
  screenOn()
  shell('svc power stayon true')
  // Without it Chrome opens its own "Chrome notifications make things
  // easier" sheet over the page on start (Android 13+). A real phone that
  // plays music has it granted (the media notification needs it).
  shell('pm grant com.android.chrome android.permission.POST_NOTIFICATIONS')
  shell('svc wifi enable')
  shell('svc data enable')
  adb('reverse', `tcp:${PHONE_PORT}`, `tcp:${hostPort}`)
}

export function releasePhone(): void {
  try {
    adb('reverse', '--remove-all')
    if (adb('forward', '--list').includes(`tcp:${CDP_PORT}`)) adb('forward', '--remove', `tcp:${CDP_PORT}`)
    shell('svc wifi enable')
    shell('svc data enable')
    shell('svc power stayon false')
  } catch (error) {
    console.warn('android teardown:', error)
  }
}
