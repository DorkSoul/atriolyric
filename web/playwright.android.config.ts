import { defineConfig } from 'playwright/test'
import realConfig from './playwright.real.config'
import { WEB_URL } from './tests/real/env'
import { PHONE_ORIGIN } from './tests/android/adb'

// The Android phone suite: real Chrome on an Android device or emulator
// (driven over adb), against the real backend and the Vite dev server - the
// same servers, data and library as the real two-device suite, plus a second
// root with the phone albums. Run with `npm run test:android` (see README).
//
// The phone loads the app from http://localhost:3000 through `adb reverse`,
// so its requests carry that origin; the desktop "second device" keeps
// http://127.0.0.1:3000. Both are allowed for the cross-origin write check.
const [backend, vite] = Array.isArray(realConfig.webServer) ? realConfig.webServer : []

export default defineConfig({
  ...realConfig,
  testDir: './tests/android',
  globalSetup: './tests/android/global-setup.ts',
  globalTeardown: './tests/android/global-teardown.ts',
  // One phone: everything runs in order, in one worker. All tests live in
  // one file, so the --workers=3 that pw-local.sh passes cannot split them.
  fullyParallel: false,
  workers: 1,
  timeout: 120_000,
  // `use` (serviceWorkers: 'block', ...) applies to the desktop contexts only;
  // Chrome on the phone is not launched by the runner and keeps service
  // workers (the installability check needs them).
  webServer: [
    {
      ...backend,
      env: { ...backend.env, ATRIOLYRIC_ALLOWED_ORIGINS: JSON.stringify([WEB_URL, PHONE_ORIGIN]) },
    },
    vite,
  ],
})
