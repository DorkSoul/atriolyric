import realGlobalTeardown from '../real/global-teardown'
import { releasePhone } from './adb'

export default function globalTeardown(): void {
  releasePhone()
  realGlobalTeardown()
}
