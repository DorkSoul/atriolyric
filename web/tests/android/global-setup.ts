import { execFileSync } from 'node:child_process'
import { join } from 'node:path'
import { request } from 'playwright/test'
import realGlobalSetup from '../real/global-setup'
import { DATA_DIR, STORAGE_STATE, WEB_URL } from '../real/env'
import { PHONE_PORT, preparePhone } from './adb'
import { RELAY_PORT, startRelay } from './net-proxy'

export const PHONE_LIBRARY_DIR = join(DATA_DIR, 'phone-library')

/** The real suite's set-up (its 4-track album, login, scan), then a second
 * root with the phone suite's albums, then the phone itself (through the
 * relay in net-proxy.ts). Returns the relay's teardown. */
export default async function globalSetup(): Promise<() => Promise<void>> {
  await realGlobalSetup()

  execFileSync('uv', ['run', '--frozen', 'python', '../web/tests/android/make-phone-albums.py', PHONE_LIBRARY_DIR], {
    cwd: '../server',
    stdio: 'inherit',
  })
  const api = await request.newContext({ baseURL: WEB_URL, storageState: STORAGE_STATE })
  const created = await api.post('/api/admin/roots', {
    data: { path: PHONE_LIBRARY_DIR, display_name: 'Phone library', is_shared: true },
  })
  if (!created.ok()) throw new Error(`add root failed: ${created.status()} ${await created.text()}`)
  const root = (await created.json()) as { id: number }
  const scan = await api.post(`/api/admin/roots/${root.id}/scan`)
  if (scan.status() !== 202) throw new Error(`scan failed: ${scan.status()} ${await scan.text()}`)
  const deadline = Date.now() + 90_000
  for (;;) {
    const roots = (await (await api.get('/api/admin/roots')).json()) as Array<{
      id: number
      active_task: unknown
      last_task: { status: string } | null
    }>
    const mine = roots.find((r) => r.id === root.id)
    if (mine && !mine.active_task && mine.last_task) {
      if (mine.last_task.status !== 'completed') throw new Error(`scan ended as ${JSON.stringify(mine.last_task)}`)
      break
    }
    if (Date.now() > deadline) throw new Error('phone library scan did not finish in time')
    await new Promise((r) => setTimeout(r, 500))
  }
  const added = (await (await api.get('/api/library/recently-added?limit=50')).json()) as unknown[]
  if (added.length !== 11) throw new Error(`expected 11 scanned tracks, got ${added.length}`)
  await api.dispose()

  const stopRelay = await startRelay(PHONE_PORT)
  preparePhone(RELAY_PORT)
  return stopRelay
}
