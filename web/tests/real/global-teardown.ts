import { rmSync } from 'node:fs'
import { DATA_DIR } from './env'

export default function globalTeardown(): void {
  // Keep the backend log when asked to (REAL_E2E_KEEP=1) or when CI wants it.
  if (process.env.REAL_E2E_KEEP) {
    console.log(`real e2e data kept in ${DATA_DIR}`)
    return
  }
  rmSync(DATA_DIR, { recursive: true, force: true })
}
