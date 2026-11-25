import { execFileSync } from 'node:child_process'
import { request, type APIRequestContext } from 'playwright/test'
import {
  LIBRARY_DIR,
  PASSWORD,
  STORAGE_STATE,
  USERNAME,
  VIDEO_LIBRARY_DIR,
  VIEWER_STORAGE_STATE,
  VIEWER_USERNAME,
  WEB_URL,
} from './env'
import { CUT_TITLE, makeCutVideo, makeVideo, VIDEO_TITLE } from './make-video'

/** Add a library root and wait for its first scan to finish. */
async function addRootAndScan(api: APIRequestContext, path: string, displayName: string): Promise<void> {
  const created = await api.post('/api/admin/roots', {
    data: { path, display_name: displayName, is_shared: true },
  })
  if (!created.ok()) throw new Error(`add root failed: ${created.status()} ${await created.text()}`)
  const root = (await created.json()) as { id: number }

  const scan = await api.post(`/api/admin/roots/${root.id}/scan`)
  if (scan.status() !== 202) throw new Error(`scan failed: ${scan.status()} ${await scan.text()}`)

  // Done when the root has no active task and its last task finished.
  const deadline = Date.now() + 90_000
  for (;;) {
    const roots = (await (await api.get('/api/admin/roots')).json()) as Array<{
      id: number
      active_task: unknown
      last_task: { status: string } | null
    }>
    const mine = roots.find((r) => r.id === root.id)
    if (mine && !mine.active_task && mine.last_task) {
      if (mine.last_task.status !== 'completed') {
        throw new Error(`scan of ${displayName} ended as ${JSON.stringify(mine.last_task)}`)
      }
      return
    }
    if (Date.now() > deadline) throw new Error(`scan of ${displayName} did not finish in time`)
    await new Promise((r) => setTimeout(r, 500))
  }
}

/** Build the tiny library and the video library, log in, add both as
 * roots and scan them. */
export default async function globalSetup(): Promise<void> {
  // Reuses mutagen from the server environment, so the tags are exactly what
  // the scanner reads in the backend's own tests.
  execFileSync('uv', ['run', '--frozen', 'python', '../web/tests/real/make-library.py', LIBRARY_DIR], {
    cwd: '../server',
    stdio: 'inherit',
  })
  makeVideo(VIDEO_LIBRARY_DIR)
  makeCutVideo(VIDEO_LIBRARY_DIR)

  // Through the Vite proxy, as the browsers will: same origin, same Host.
  const api = await request.newContext({ baseURL: WEB_URL })
  const login = await api.post('/api/auth/login', { data: { username: USERNAME, password: PASSWORD } })
  if (!login.ok()) throw new Error(`login failed: ${login.status()} ${await login.text()}`)

  await addRootAndScan(api, LIBRARY_DIR, 'Real library')
  await addRootAndScan(api, VIDEO_LIBRARY_DIR, 'Real videos')

  // Tracks only: the video is listed under Videos, not here.
  const added = (await (await api.get('/api/library/recently-added?limit=50')).json()) as unknown[]
  if (added.length !== 4) throw new Error(`expected 4 scanned tracks, got ${JSON.stringify(added).slice(0, 300)}`)
  const videos = (await (await api.get('/api/library/videos?dir=')).json()) as Array<{ id: number; title: string }>
  for (const title of [VIDEO_TITLE, CUT_TITLE]) {
    const video = videos.find((v) => v.title === title)
    if (!video) throw new Error(`expected the video "${title}", got ${JSON.stringify(videos).slice(0, 300)}`)
    // The point of these videos: they must take the HLS transcode path.
    const playback = (await (await api.get(`/api/stream/${video.id}/playback`)).json()) as { mode: string }
    if (playback.mode !== 'hls') throw new Error(`expected "${title}" to play through HLS, got ${JSON.stringify(playback)}`)
  }

  const viewerCreated = await api.post('/api/admin/users', {
    data: { username: VIEWER_USERNAME, password: PASSWORD, is_admin: false },
  })
  if (!viewerCreated.ok()) throw new Error(`add viewer failed: ${viewerCreated.status()} ${await viewerCreated.text()}`)

  await api.storageState({ path: STORAGE_STATE })
  await api.dispose()

  const viewer = await request.newContext({ baseURL: WEB_URL })
  const viewerLogin = await viewer.post('/api/auth/login', { data: { username: VIEWER_USERNAME, password: PASSWORD } })
  if (!viewerLogin.ok()) throw new Error(`viewer login failed: ${viewerLogin.status()} ${await viewerLogin.text()}`)
  await viewer.storageState({ path: VIEWER_STORAGE_STATE })
  await viewer.dispose()
}
