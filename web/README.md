# Atriolyric web app

React 19, TypeScript and Vite. In production the build is served by the backend; in development Vite serves it and proxies `/api` and `/ws` to the backend on `127.0.0.1:8321`. See the top-level [README](../README.md) for setup.

## Scripts

Run from this folder. Requires Node 22 or newer.

| Command | What it does |
|---------|--------------|
| `npm run dev` | Vite dev server on `http://127.0.0.1:3000` (`ATRIOLYRIC_DEV_HOST` changes the bind address) |
| `npm run build` | `tsc -b` (type-checks the app, the config and the e2e tests), then `vite build` into `dist/`, which also writes `dist/version.json` |
| `npm run lint` | `oxlint` |
| `npm run preview` | Serves the built `dist/` |
| `npm run test:e2e` | The mocked Playwright suite |
| `npm run test:real` | The real two-device Playwright suite |
| `npm run test:android` | The real suite's servers, driven from Chrome on an Android device or emulator |

The app version is `version` in `package.json` (see [docs/releasing.md](../docs/releasing.md)).

## Tests

There are two Playwright suites, plus an Android variant of the real one.

**Mocked** (`npm run test:e2e`, `tests/e2e/`, config `playwright.config.ts`). The API is faked in the browser (`tests/e2e/mockApi.ts`, one module per resource in `tests/e2e/mock/`), and Playwright starts the Vite dev server itself. Fast; covers UI flows, accessibility (axe) and layout. CI runs it in Chromium, Firefox and WebKit.

**Real** (`npm run test:real`, `tests/real/`, config `playwright.real.config.ts`). The real web app against the real backend, nothing mocked, with two browser contexts acting as two devices of one user. It is the only suite that exercises the playback protocol end to end. It needs `uv sync --frozen` done in `../server`, starts its own backend on port 8321 with a temporary database and a small generated WAV library. Stop any dev backend first, since it uses the same port. It runs in Chromium only.

**Android** (`npm run test:android`, `tests/android/`, config `playwright.android.config.ts`). The same servers and library as the real suite (plus a second root with short generated albums), with the phone checks the desktop suites cannot make: real touch input, the screen turned off mid-queue, Android Back, pull to refresh, the phone viewport, installability, and claiming or losing playback against a desktop Chromium context. It needs one booted Android device or emulator visible to `adb` (`ANDROID_SERIAL`, default `emulator-5554`; `ANDROID_HOME` for the SDK) with Chrome on a debuggable build, since Chrome is started with a command-line file to skip its first-run screens and is then driven over its DevTools socket. The phone reaches the dev server through `adb reverse tcp:3000`, so it loads the app from `http://localhost:3000`, which the config adds to `ATRIOLYRIC_ALLOWED_ORIGINS`. Taps and swipes are real OS input (`adb shell input`). `ANDROID_LAUNCH=playwright` uses Playwright's `launchBrowser()` instead, which also turns off Chrome's background throttling, so the screen-off numbers are not representative in that mode. Screenshots (one per view, and the whole phone screen when a test fails) go to `ANDROID_SHOTS_DIR` (default `test-results/android-shots`, kept after the run). The tests share one phone and run in order in a single file.

### Pinned dependencies

`package.json` cannot carry comments, so the exact pins are explained here.

- `hls.js` is pinned exactly. It plays every video the server converts to HLS, and the app's recovery code (`src/player/useHlsVideo.ts`) depends on how it retries and reports errors, which has changed between minor releases. Update it on purpose: read the release notes and `MIGRATING.md` for every minor version crossed, check the recovery code and the hls.js stand-in in `tests/e2e/app-helpers.ts` against them, and play a converted video on a phone.
- `openapi-typescript` is pinned exactly because its output is checked in (`src/api-types.ts`, see `npm run gen:api-types`). Its TypeScript peer range does not include TypeScript 6 yet, hence the `overrides` entry that hands it the project's own TypeScript.

### Playwright version

CI runs the mocked suite inside the `mcr.microsoft.com/playwright` container, whose image tag must match the `playwright` version installed from `package-lock.json`: the browsers in the image are the ones that exact version expects. `.github/workflows/docker-publish.yml` names the image (currently `v1.62.1-noble`) and has a step that fails the job when the installed version differs. When you update `playwright` in `package.json`, change the image tag and the version in that check in the same commit. The real suite does not use the container; it installs Chromium with `npx playwright install --with-deps chromium`.
