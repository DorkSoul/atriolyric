import { defineConfig } from 'playwright/test'
import realConfig from './playwright.real.config'
import { OFFERED_APK } from './tests/app/app'

// The Android app suite: the APK from mobile/ on an Android emulator or
// device (over adb), against the real backend and the Vite dev server - the
// real two-device suite's servers and library, plus one 720p clip. The app is
// told to use http://127.0.0.1:3000, which `adb reverse` carries to the dev
// server on this machine. Build the APK first (mobile/build-apk.sh), then
// `npm run test:app`. See mobile/README.md.
const [backend, vite] = Array.isArray(realConfig.webServer) ? realConfig.webServer : []

export default defineConfig({
  ...realConfig,
  testDir: './tests/app',
  globalSetup: './tests/app/global-setup.ts',
  globalTeardown: './tests/app/global-teardown.ts',
  // One device, one app: the tests run in order and build on each other.
  fullyParallel: false,
  workers: 1,
  timeout: 240_000,
  expect: { timeout: 20_000 },
  webServer: [
    // The test radio station's stream is served from this machine.
    // It offers the Android app whenever a test puts an APK at OFFERED_APK.
    {
      ...backend,
      env: {
        ...backend.env,
        ATRIOLYRIC_ALLOW_PRIVATE_RADIO_HOSTS: 'true',
        ATRIOLYRIC_ANDROID_APK_PATH: OFFERED_APK,
      },
    },
    vite,
  ],
})
