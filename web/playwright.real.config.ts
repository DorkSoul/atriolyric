import { defineConfig, devices } from 'playwright/test'
import { BACKEND_LOG, BACKEND_PORT, DATA_DIR, PASSWORD, USERNAME, WEB_URL } from './tests/real/env'

// The real two-device suite: the real web app against the real backend, no
// mocks. Run with `npm run test:real` (needs `uv sync --frozen` in ../server).
// The fast mocked suite stays `npm run test:e2e` (playwright.config.ts).
export default defineConfig({
  testDir: './tests/real',
  testMatch: '**/*.spec.ts',
  globalSetup: './tests/real/global-setup.ts',
  globalTeardown: './tests/real/global-teardown.ts',
  // The scenarios share one server's playback state.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: WEB_URL,
    serviceWorkers: 'block',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        launchOptions: {
          args: ['--autoplay-policy=no-user-gesture-required', '--mute-audio'],
        },
      },
    },
  ],
  webServer: [
    {
      // Backend stdout/stderr (playback_diagnostic events) go to the data dir.
      command: `uv run --frozen uvicorn app.main:app --host 127.0.0.1 --port ${BACKEND_PORT} > "${BACKEND_LOG}" 2>&1`,
      cwd: '../server',
      url: `http://127.0.0.1:${BACKEND_PORT}/api/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: {
        ATRIOLYRIC_DATABASE_PATH: `${DATA_DIR}/atriolyric.db`,
        ATRIOLYRIC_DEFAULT_USERNAME: USERNAME,
        ATRIOLYRIC_DEFAULT_PASSWORD: PASSWORD,
        ATRIOLYRIC_COOKIE_SECURE: 'false',
        // The HLS spec's transcodes use libx264 on every machine (a CI runner
        // may have no GPU), not a VAAPI device when the machine has one.
        ATRIOLYRIC_HWACCEL: 'none',
        // The dev server proxies /api, and the backend's cross-origin write
        // check sees the page's origin (port 3000) on a request to 8321: without
        // this every POST (play reports, play sources) is refused with 403.
        // Production serves the app from the backend itself, so it is
        // same-origin there.
        ATRIOLYRIC_ALLOWED_ORIGINS: JSON.stringify([WEB_URL]),
      },
    },
    {
      command: 'npm run dev -- --host 127.0.0.1',
      url: WEB_URL,
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
})
