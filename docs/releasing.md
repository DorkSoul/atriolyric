# Releasing

The app version lives in one place: `version` in `web/package.json`. The web build bakes it into the bundle and into `version.json`, which the server reports at `/api/version`. Open apps compare that with their own and prompt for a restart when it changes, so every release needs a bump.

## Steps

1. Pick the number: patch for fixes, minor for features (semantic versioning).
2. Set `version` in `web/package.json`, and the same version in `web/package-lock.json`: the top-level `"version"` and the `"version"` under `packages[""]` (two entries).
3. Commit on `master` and run the tests locally (`scripts/local-gate.sh`, see below).
4. Tag and push:

   ```bash
   git tag -a vX.Y.Z -m "Atriolyric X.Y.Z"
   git push origin master vX.Y.Z
   ```

5. Build the signed Android app and publish the GitHub release with it attached as `atriolyric.apk`: `mobile/release-apk.sh --release` (see "The Android app"). Publishing the release starts the image build, which puts the APK inside the image.

The tag must be `v` plus the version, for example `v1.0.0`.

## The Android app

The signed APK is built on a workstation, because the signing key lives there and nowhere else (`~/.config/atriolyric/android-signing.env` names the keystore; see [mobile/README.md](../mobile/README.md)). Keep a backup of that keystore: Android only updates an app signed with the same key, so losing it means everyone uninstalls and reinstalls.

`mobile/release-apk.sh` builds `mobile/dist/atriolyric-<version>.apk`; with `--release` it also publishes the GitHub release `v<version>` with the APK attached (the `gh` CLI, signed in). The image carries the APK at `/app/android/atriolyric.apk`; the server offers it in Settings > About ("Get the Android app") and installed apps see "App update available". An image built without one offers no app, and installed apps stay as they are. The app only notices an update when the server's APK is newer than itself.

## What gets published

`.github/workflows/docker-publish.yml` builds the image, scans it with Trivy (HIGH or CRITICAL findings that have a fix stop the push) and publishes it to `ghcr.io/dorksoul/atriolyric`:

| Trigger | Tags |
|---------|------|
| A push to `master` | `edge`, `sha-<commit>` |
| A published GitHub release `vX.Y.Z` | `X.Y.Z`, `X.Y`, `latest`, with the release's `atriolyric.apk` inside |

`.github/workflows/tests.yml` runs the backend, web, mocked browser (Chromium) and real two-device suites on pull requests into `master` only. Nothing runs the tests on a push, so run them locally before a release.

### What a deployed stack pulls

| Stack uses | Updates on |
|------------|------------|
| `edge` | every push to `master` |
| `latest` | each release |
| `X.Y` | each patch release of that minor |
| `X.Y.Z` | never; you change it by hand |

After a release, `docker compose pull && docker compose up -d` (or a redeploy with "pull image") picks it up.

## The local test gate

`scripts/local-gate.sh` runs the whole test gate for the checked-out commit on a workstation (Podman or Docker needed). It is much faster than CI, and nothing runs the tests on a push to `master`, so run it before every release.

```bash
git checkout master          # the commit to release, committed and clean
scripts/local-gate.sh        # runs what the changes need
```

The parts of the gate, in containers that match what ships:

- the backend lint, type-check, dependency audit and tests, as root in the Dockerfile's runtime base image with that image's ffmpeg (not the workstation's Python or ffmpeg). The tests run in six processes at once (`pytest-xdist`);
- the web lint, build and dependency audit, and the mocked browser suite in Chromium, in the Playwright image CI uses. Firefox and WebKit are optional: `--all-browsers` adds them;
- the API-types staleness check and the real two-device suite, on the workstation.

Only the parts a change can affect are run. The script compares the commit with the last one it passed (the local ref `refs/gate/verified`):

| Changed | Runs |
|---|---|
| `server/` | the backend part |
| `web/` code, tests or configuration | the web part |
| only files in `web/public/` that are not scripts (icons, the manifest) | the web part |
| only the version number in `web/package.json` and `web/package-lock.json` | no web part |
| `docs/`, `*.md` | nothing |
| anything else (the Dockerfile, workflows, scripts) | every part |

The API-types check and the real two-device suite run every time, so a change on one side that breaks the other is still caught. When the last verified commit is not an ancestor of the one being checked (after an amend or a rebase), or there is none, every part runs. `--full` runs every part regardless, and `--plan` prints which parts would run and stops.

A dependency audit only runs with its own part, so a release that touches only the server does not re-audit the npm packages. Run `--full` now and then, for example before a release that follows a long gap.

Within each part the cheap checks come first (lint and types before the tests), and the steps that download something are tried three times.

When all of it passes, the script points `refs/gate/verified` at the commit, so the next run only checks what changed since.

Each container step leaves a marker as its last command, and the gate fails if the marker is missing afterwards: an exit status of 0 is not proof that a step ran (a `GATE_WRAP` that does not pass standard input on leaves the container with an empty script, which "passes" at once). Before passing, the gate also checks that the checkout is still the commit it started with. After any gate run, the log should show the pytest summary line and the Playwright totals.

The web dependency audit is `npm run audit:deps` (`web/scripts/audit.mjs`): `npm audit` at the high level, except that an advisory listed in `web/audit-exceptions.json` is accepted for development-only packages until its `expires` date. Packages that ship get no exceptions, and an expired or unused entry fails the audit. Add an entry only for an advisory with no fix available, with the reason and a date about a month ahead.

Options are environment variables: `GATE_WRAP` (a command prefix for the heavy steps, for example a wrapper that limits CPU and memory), `GATE_ENGINE` (`podman` or `docker`), `GATE_CACHE` (caches and test output; default `~/.cache/atriolyric-gate`), `GATE_BROWSERS` (default `chromium`; `--all-browsers` makes it all three), `GATE_PYTEST_WORKERS` (default 6).
