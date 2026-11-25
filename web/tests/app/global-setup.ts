import { existsSync } from 'node:fs'
import { request } from 'playwright/test'
import realGlobalSetup from '../real/global-setup'
import { STORAGE_STATE, WEB_URL } from '../real/env'
import { adb, PHONE_PORT, preparePhone, shell } from '../android/adb'
import {
  APK,
  APP_VIDEO_DIR,
  APP_VIDEO_TITLE,
  makeAppVideo,
  makeRadioClip,
  PACKAGE,
  RADIO_PORT,
  RADIO_STATION,
  setAeroplaneMode,
  startRadio,
} from './app'

/** The real suite's set-up (4-track album, two small videos, login), a third
 * root with a 720p clip, then the app itself: installed fresh, with the
 * emulator's 127.0.0.1:3000 forwarded to the dev server on this machine. */
export default async function globalSetup(): Promise<() => Promise<void>> {
  if (!existsSync(APK)) throw new Error(`No APK at ${APK}: build it first with mobile/build-apk.sh`)
  await realGlobalSetup()

  makeAppVideo()
  const api = await request.newContext({ baseURL: WEB_URL, storageState: STORAGE_STATE })
  const created = await api.post('/api/admin/roots', { data: { path: APP_VIDEO_DIR, display_name: 'App videos', is_shared: true } })
  if (!created.ok()) throw new Error(`add root failed: ${created.status()} ${await created.text()}`)
  const root = (await created.json()) as { id: number }
  const scan = await api.post(`/api/admin/roots/${root.id}/scan`)
  if (scan.status() !== 202) throw new Error(`scan failed: ${scan.status()} ${await scan.text()}`)
  const deadline = Date.now() + 90_000
  for (;;) {
    const videos = (await (await api.get('/api/library/videos?dir=')).json()) as Array<{ title: string }>
    if (videos.some((video) => video.title === APP_VIDEO_TITLE)) break
    if (Date.now() > deadline) throw new Error(`"${APP_VIDEO_TITLE}" was not scanned in time`)
    await new Promise((r) => setTimeout(r, 500))
  }
  // A radio station fed by a stream on this machine.
  const stopRadio = await startRadio(makeRadioClip())
  const station = await api.post('/api/radio/stations', {
    data: { name: RADIO_STATION, stream_url: `http://127.0.0.1:${RADIO_PORT}/live.mp3`, is_shared: true },
  })
  if (!station.ok()) throw new Error(`add station failed: ${station.status()} ${await station.text()}`)
  await api.dispose()

  setAeroplaneMode(false)
  preparePhone(PHONE_PORT)
  shell(`am force-stop ${PACKAGE} || true`)
  adb('install', '-r', APK)
  // A first run: no server set, no login, no downloads.
  shell(`pm clear ${PACKAGE}`)
  shell(`pm grant ${PACKAGE} android.permission.POST_NOTIFICATIONS`)
  return stopRadio
}
