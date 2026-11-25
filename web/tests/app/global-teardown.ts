import realGlobalTeardown from '../real/global-teardown'
import { releasePhone, shell } from '../android/adb'
import { detach, PACKAGE, setAeroplaneMode } from './app'

export default async function globalTeardown(): Promise<void> {
  await detach()
  try {
    setAeroplaneMode(false)
    shell(`am force-stop ${PACKAGE}`)
  } catch (error) {
    console.warn('app teardown:', error)
  }
  releasePhone()
  realGlobalTeardown()
}
