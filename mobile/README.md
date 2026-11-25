# Atriolyric for Android

The Android app is the web app in a Capacitor 8 shell with one native plugin
of our own, and plays music with native players (Media3). What it adds over
the PWA is downloads:

- Songs and videos can be downloaded to the device. A downloaded copy is the
  one that plays, with or without a connection.
- With no connection it is still the same app: it shows what it loaded before
  and plays the downloads. There is no separate offline mode to switch to.

## Build

Needs Node 22+, JDK 21 and an Android SDK with platform 36 and build-tools 36.
The Android project uses Gradle 9.9 (wrapper, checksum pinned), the Android
Gradle plugin 9.5 (which compiles the Kotlin itself, so there is no separate
Kotlin plugin; Kotlin 2.4) and Media3 1.11.

```bash
mobile/build-apk.sh
```

It builds the bundled page from `web/`, copies it into the Android project and
runs Gradle. The APK is written to
`mobile/android/app/build/outputs/apk/debug/app-debug.apk`. Set `JAVA_HOME` and
`ANDROID_HOME` if the script does not find them. Install it with:

```bash
adb install -r mobile/android/app/build/outputs/apk/debug/app-debug.apk
```

That is the debug build, for development and the tests. The release build is
shrunk and optimised by R8 (`app/proguard-rules.pro`; Capacitor's own rules
keep the plugin, which it calls by reflection). To check that R8 broke nothing,
build the debug APK through R8 too and run the emulator suite on it:

```bash
(cd mobile/android && ./gradlew assembleDebug -Patrio.minifyDebug)
```

## Release build, download and updates

```bash
mobile/release-apk.sh            # mobile/dist/atriolyric-<version>.apk, signed
mobile/release-apk.sh --release  # and published as the GitHub release v<version>
```

The release build is signed with the key named in
`~/.config/atriolyric/android-signing.env` and takes its version from
`web/package.json` (version code `major*1000000 + minor*1000 + patch`). The
script refuses a working tree with uncommitted changes (`--allow-dirty` for a
local test build, never with `--release`), and the signing passwords reach
Gradle only, never npm or Vite. `--release` attaches the APK to the GitHub
release as `atriolyric.apk`; the image build then puts it in the image. If a
release has no APK, the image build says so with a warning on its run. A server whose image carries
the APK offers it under Settings > About, and an installed app that is older
shows "App update available": it downloads the APK from its own server and
opens Android's installer. The app only accepts an update from the address of
the server it is set to, and only an APK of this app (same package name),
signed with the same key and with a higher version code; anything else is
refused before the installer opens. The first install from a browser needs "install unknown apps"
allowed for that browser; a debug build cannot be updated to a release build
(different keys), so uninstall it first. See
[docs/releasing.md](../docs/releasing.md).

## How it works

- **One app, connection or not.** With a server set, the app loads the web
  app from that server, same-origin with its API (`MainActivity.kt`). Every
  request for the page, its files and its artwork passes through the app's own
  copy (`WebCache.kt`): the page is asked from the server each time and comes
  from the copy when the server does not answer; built files and artwork come
  from the copy once they are there. The page copy is app data (an offline
  start needs it); the artwork copy is in the cache folder, which "Clear
  cache" or the system may empty, as it can always be fetched again. The page also has the app fetch the views
  that were never opened (`/asset-list.json`, written by the web build), so
  each of them opens offline too. The app has no service worker; this copy
  does that job.
- **What the pages show offline.** The answer to every read request is kept on
  the device (`web/src/native/offlineCache.ts`). When the server does not
  answer, a page gets the answer it got last time, so the library, playlists
  and Home look as they did. `web/src/native/offline.ts` decides whether the
  server is reachable and asks again every 15 seconds while it is not; a small
  "Offline" notice shows meanwhile. So that screens never opened still have
  something to show, the app also fetches the whole library in the background
  once a day while connected (`web/src/native/offlineLibrary.ts`): every
  album, artist and song, which songs carry which genre and tag
  (`GET /api/library/labels/all`), the playlists with their contents, the
  video folders and the radio stations. Offline, the song, album and artist lists
  are answered from that copy, with their filters (genre and tag included),
  order and paging, and so is the Genres/tags view of a song, album or
  artist, whenever the exact answer was never kept. A downloaded song's
  lyrics are kept with it. Changes (likes, playlist edits) need the
  server and say so.
- **What plays offline.** The play queue lives on the server. With no server,
  the playback socket is replaced by a stand-in on the device
  (`web/src/native/localPlayback.ts`) that takes the same messages and answers
  with the same snapshots, for downloaded songs only. The player does not know
  the difference, so the queue, shuffle, repeat and the notification work as
  usual. Rows that are not downloaded are greyed and skipped. When the server
  is back, a queue that was changed offline is handed to it in one
  `set_queue_context` (with the position, see `docs/playback-protocol.md`),
  unless another device is playing by then. Plays made offline are reported
  when the server is back, as after any lost connection.
- **The bundled page** (`web/shell/`, `web/src/shell/`) only asks for the
  server's address, and says so when a server that was never loaded cannot be
  reached. Settings > Downloads > Change server opens it. Only this page may
  set the server (`setServerUrl`, `connect`, `openApp`): the server's own page
  is refused, so a script on it cannot point the app somewhere else.
- **A sign-in gateway.** A server behind a reverse proxy with its own login
  (an identity-aware proxy, for example) answers the app's first check with a redirect. The
  app treats that as reachable and shows the gateway's login inside the app;
  its session cookie then travels with the native players' requests too.
  Native requests take their cookies from the WebView's store per request
  (`WebViewCookies` in `Net.kt`), so a redirect to another host never carries
  the server's cookie. A song or video download that comes back as a web page
  (the gateway's login, once its session ran out) fails instead of being
  saved, and a download is never taken from another host than the one asked.
- **The plugin** (`AtrioNativePlugin.kt`, called from `web/src/native/`). The
  page finds it through `window.Capacitor`; the web bundle does not depend on
  Capacitor, so the PWA is unchanged.
- **Playback** (`PlayerHub.kt`). The web player works with two `<audio>`
  elements, the playing one and a standby with the next track. In the app each
  is backed by a native player (`web/src/native/nativeAudio.ts`), so the queue,
  play reports and multi-device logic are the web player's own. One step is
  native: the page marks the standby as the queue's next track
  (`playerLoad { next }` / `playerArm`), and when the playing track ends the
  app starts that standby at once, whether or not the page is awake (Chromium
  freezes a hidden page). The page hears both changes when it runs again and
  goes on as after its own gapless hand-over. The page is still kept awake
  while music plays (`MainActivity.applyPageVisibility`) to queue the track
  after that. One
  `MediaSessionService` holds the session. Only the system and what it trusts
  (notification, lock screen, Bluetooth headsets), this app, Android Auto or
  a car, and the Wear OS companion may connect to it; other installed apps
  are refused. Radio and a playing video's
  background sound are backed the same way (slots `r` and `v`). The
  equaliser and volume levelling run natively on the music players
  (`Equaliser.kt`, the same ten bands and filters as `web/src/audioEffects.ts`).
- **The phone's bars.** The page reports its colours (`syncSystemBars` in
  `web/src/native/bridge.ts`, on every theme change), and the app paints the
  area behind the status bar in the page background and the area behind the
  navigation bar in the bottom bar's colour (`MainActivity.applySystemBars`).
- **Closing the app.** Swiping it away stops the music. The next start shows
  what was playing, paused: online that is the server's queue, as in the PWA;
  offline it is the stand-in's, which keeps the last queue and position.
- **Downloads** (`Downloads.kt`). Media3's download manager stores each item in
  a cache under the app's files; the players read that cache first, so a
  downloaded copy always wins. A song comes from `/api/stream/{id}` at the
  music download quality; a video is the original file or a rendition the
  server prepares (`/api/download/video/{id}/renditions/...`). The download
  qualities are their own settings (Settings > Downloads), apart from the
  streaming quality. A downloaded video plays in a native player
  (`VideoActivity.kt`), online too, with audio focus like the music.
  The list event (`downloads`) is sent when the list changes; progress comes
  separately once a second (`downloadProgress`, only what is being fetched).
  A download's artwork is an image address on the server's origin,
  `/__atrio/download-art/<id>`, answered by the app from the saved file.
  "Download over Wi-Fi only" (Settings > Downloads) sets the download
  manager's requirement to an unmetered network; downloads waiting for it are
  restarted by a job (`PlatformScheduler`), and on Android 15 they pause when
  the daily limit for background data syncs runs out and carry on when the
  app is next opened. A change of quality replaces each item in one step;
  the replacement is kept on disk, so it still happens after a crash. Song and video rows carry a mark when they
  are on the device and a ring while they download; their menus then offer
  "Remove download" instead of a second download, and the app refuses one too
  (`Downloads.start`). An album, artist or playlist downloaded as a whole is
  remembered with its items (`downloadActions.ts`), so its menu knows.

## Tests

```bash
mobile/build-apk.sh
cd web && npm run test:app
```

The suite (`web/tests/app/`) installs the APK on a running emulator or device
(`ANDROID_SERIAL`, default `emulator-5554`) and runs against the real backend
and the Vite dev server, like the real two-device suite. It covers the first
run, a queue advancing with the screen off, media-session controls, downloads
at the chosen qualities, the marks and menus of downloaded items, the
downloads list, an aeroplane-mode cold start into the same app (greyed rows, a
downloaded song and video playing, a list never opened before filled from
the background fetch, the queue handed to the server when the connection
returns), the equaliser, radio and a video's sound with the screen off, and what is shown
after the app was swiped away. The equaliser's filters, the page-copy rules (`WebCache`) and the network
rules (`Net`: same-origin, media and image types) have JVM unit tests:
`cd mobile/android && ./gradlew testDebugUnitTest`. Media3 is 1.11.1.

## Not done yet

- A video that is streamed (not downloaded) shows its picture in the page;
  only its sound moves to a native player while the app is in the background.
- The equaliser and volume levelling apply to music, not to radio or videos
  (as in the web app). The native limiter has no make-up gain, so a boosted
  preset can sound slightly quieter than in a browser.
- Offline, album, artist and playlist tiles are not marked or greyed (only
  song and video rows are).
- Offline, the lists worked out from the app's copy of the library know the
  default, title, album and year orders, and an artist's "appears on" albums
  are not shown. Info pages (MusicBrainz, ListenBrainz) and search show only what
  was opened before.
- No store listing; updates come from your own server.
