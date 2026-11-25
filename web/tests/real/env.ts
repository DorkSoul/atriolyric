import { mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

export const PASSWORD = 'real-e2e-password-1'
export const USERNAME = 'admin'
export const BACKEND_PORT = 8321
export const WEB_URL = 'http://127.0.0.1:3000'

// One data directory per run. The config is loaded first, in the main
// process; the env var carries the path to the workers and global hooks.
process.env.REAL_E2E_DIR ??= join(tmpdir(), `atriolyric-real-e2e-${Date.now()}`)
export const DATA_DIR = process.env.REAL_E2E_DIR
mkdirSync(DATA_DIR, { recursive: true })
export const LIBRARY_DIR = join(DATA_DIR, 'library')
/** A second root holding the videos the HLS spec plays (see make-video.ts). */
export const VIDEO_LIBRARY_DIR = join(DATA_DIR, 'videos')
export const STORAGE_STATE = join(DATA_DIR, 'storage-state.json')
/** A second account for the HLS spec: playback state is per user, and the
 * spec files can run at the same time (pw-local.sh passes --workers=3), so
 * the video viewing must not share a queue with the two-device scenarios. */
export const VIEWER_USERNAME = 'viewer'
export const VIEWER_STORAGE_STATE = join(DATA_DIR, 'viewer-storage-state.json')
export const BACKEND_LOG = join(DATA_DIR, 'backend.log')
