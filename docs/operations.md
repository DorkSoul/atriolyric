# Operations and recovery

## Production deployment

Copy `compose.example.yaml` to `compose.yaml`, set its volume paths, put
Atriolyric behind an HTTPS reverse proxy and start the production overlay
(every setting is described in [configuration.md](configuration.md)):

```sh
ATRIOLYRIC_UID=1000 ATRIOLYRIC_GID=1000 \
  docker compose -f compose.yaml -f compose.production.yaml up -d
```

Replace the IDs with a dedicated unprivileged host account. It needs write access to `/data`. Give it write access to media only when you want the in-app tag editor to write tags into the files; otherwise add `:ro` to that volume. Nothing else writes to media: removing a track in the app only drops it from the index and never deletes the file.

Without the overlay the image starts as root, gives `/data` to `PUID:PGID` (default `1000:1000`) and drops to that user before the server starts (`docker/entrypoint.sh`, with `setpriv`; `tini` is PID 1). The overlay runs the container as `ATRIOLYRIC_UID:ATRIOLYRIC_GID` from the start (`user:`), so `/data` must already belong to that account. It also makes the container root filesystem read-only, drops all Linux capabilities, sets `no-new-privileges`, and binds the port to `127.0.0.1:8030` only, so it is reachable through a proxy on the same machine. The session cookie is marked Secure on its own when requests arrive over HTTPS (`ATRIOLYRIC_COOKIE_SECURE=auto`, the default; see [configuration.md](configuration.md)).

Test login, streaming, scanning, and intended edits through the public HTTPS URL after changing proxy or identity settings. Do not expose the backend port directly to an untrusted network.

## Behind a reverse proxy

Set `FORWARDED_ALLOW_IPS` on the app container to the proxy's address or its Docker network's subnet (for example `172.18.0.0/16`; pin the subnet in your stack so it cannot change). Uvicorn believes `X-Forwarded-For` only from the peers in that list, default `127.0.0.1`. Without it every request comes from the proxy: the login limiter shares one bucket across all visitors and the logs cannot attribute anything.

Do not use `*` unless the app's port is reachable only through the proxy. The proxy must set `X-Forwarded-For` itself and not pass on a client's value.

When the header arrives from a peer that is not trusted, the app logs one warning ("A request carried X-Forwarded-For but arrived from ..."), naming the peer. Check the first lines after a deploy: the warning means the variable is missing or the wrong address.

### Login rate limits

Failed password checks are counted in memory (a restart clears them):

| Limit | Value |
|-------|-------|
| Per client address | 20 failures in 5 minutes |
| Per username | 10 failures in 5 minutes |

A refused attempt does not count, so a block ends when the oldest failure is five minutes old and cannot be prolonged by retrying. A successful sign-in clears only that username's failures. The address is the one uvicorn reports, so the limits are only meaningful once `FORWARDED_ALLOW_IPS` is right.

## Versions and updates

The app version is `version` in `web/package.json`. The web build bakes it into the bundle and writes `version.json`, which the server reports at `/api/version`. An open app checks that endpoint on start and whenever it returns to the foreground; when the versions differ it shows an "Update available" prompt with a Restart button. A deploy without a version bump will not prompt open apps to update. The release procedure is in [releasing.md](releasing.md).

Which image a stack pulls depends on its tag: `latest` moves only on a release, `edge` follows every commit to `master`, `X.Y` and `X.Y.Z` are releases. See the tag table in the README's "Updating" section.

## Video transcoding (HLS)

Videos whose codecs browsers cannot decode (HEVC, AV1, 10-bit H.264, AC-3/DTS audio, ...) are transcoded on the fly to H.264/AAC and streamed as HLS. Videos that already play are streamed directly from the original file. Nothing is kept as a permanent second copy.

Defaults, from `server/app/hls.py` and `server/app/config.py`:

| What | Value | Setting |
|------|-------|---------|
| Segment length | 6 seconds, fixed | no |
| Concurrent sessions | 4 | `ATRIOLYRIC_HLS_MAX_SESSIONS` |
| Cache size cap | 4 GiB | `ATRIOLYRIC_HLS_CACHE_MAX_BYTES` |
| Segments kept behind the player | 100 (10 minutes) | no |
| Idle session, files deleted after | 10 minutes | no |
| Idle session, encoder stopped after | 60 seconds | no |
| Encoder paused when this far ahead of the player | 8 segments | no |
| Wait for one segment before giving up | 120 seconds | no |

Segments are encoded as the player reaches them and cached under `/data/hls`. The encoder pauses when it is well ahead, so a paused or idle viewer costs no CPU. Seeking far from the encoder restarts it at the new position. The `hls` folder is emptied at startup and at shutdown: nothing survives a restart.

A video and its audio-only background rendition (used for background playback on phones) each take a session. Audio-only sessions never use the GPU.

### Session cap

When every session slot is taken, a new viewer may take over the session that has been idle longest, but only if nobody has asked it for a segment in the last 30 seconds. If all sessions are that busy, the request is refused with `503` and a `Retry-After` header (seconds until the least busy session would become available). The response message is "Too many videos are being converted right now - try again in a moment". Nobody else's encoder is killed. Raise `ATRIOLYRIC_HLS_MAX_SESSIONS` if the machine can encode more at once.

### Hardware encoding (`ATRIOLYRIC_HWACCEL`)

Video is always re-encoded, never copied, so segments land on the fixed keyframe grid.

- `none`: always software (libx264).
- `auto` and `vaapi`: on first use the server runs a half-second test encode with `h264_vaapi` on `/dev/dri/renderD128` (timeout 30 s) and remembers the result until restart. If the device does not exist or the test fails, it uses software. The log line `HLS video encoder:` says which was picked.
- `vaapi` differs from `auto` only in logging: when the test fails it logs an error (`ATRIOLYRIC_HWACCEL=vaapi but VAAPI is not usable`) instead of the routine info line. It does not refuse to run: it still falls back to software, so playback keeps working.
- If a VAAPI encode fails later, mid-playback, that session restarts once in software (log warning `VAAPI encode failed, falling back to software`).

To enable VAAPI map `/dev/dri` into the container and add the host's `render` group to `group_add`. The image includes the Intel and Mesa VAAPI drivers.

## Video download renditions

An app can download a video as one ordinary MP4 at a lower quality than the original: 1080p, 720p or 480p. Only levels below the source's height are offered (a 720p video offers 480p only); the original file is always available as it is. Nothing is made until somebody asks for it.

| What | Value | Setting |
|------|-------|---------|
| Encodes at once | 1, the rest wait in the order they were asked for | no |
| Cache size cap | 20 GiB | `ATRIOLYRIC_RENDITION_CACHE_MAX_BYTES` |
| Video | H.264, 8-bit, at most 6 / 3.5 / 1.5 Mbit/s for 1080p / 720p / 480p | no |
| Audio | AAC stereo, 192 kbit/s (first audio track; no subtitles) | no |
| Encode timeout | 3 times the video's length, between 10 minutes and 6 hours | no |
| A failed encode is reported as failed for | 1 hour, or until it is asked for again | no |

Files are kept under `/data/renditions` as `<item id>_<height>.mp4` and survive a restart. A rendition older than its source file is made again the next time it is asked for. When the folder grows past the cap, the least recently downloaded files are deleted first; a file that is still being written (a hidden `.tmp` name) is never deleted, and neither is the one that has just finished. The folder is disposable: it can be emptied at any time.

Only one encode runs at a time, so a batch of downloads does not take the machine away from live playback. The encoder is chosen as for HLS (`ATRIOLYRIC_HWACCEL`, see Hardware encoding above): VAAPI when it works, otherwise software; an encode that fails on VAAPI is tried once more in software. At shutdown the running encode is stopped and the queue is dropped; the app asks again.

The endpoints are listed at the top of `server/app/routers/download.py`.

## Internet radio

Each listener holds a worker thread and an upstream connection while listening, so streams are capped: 2 per user and 8 in total. Beyond that the stream request is refused with `429` and a message ("You are already listening to 2 radio streams", or "The server is relaying as many radio streams as it can"). Radio hosts that resolve to private addresses are refused unless `ATRIOLYRIC_ALLOW_PRIVATE_RADIO_HOSTS` is `true`.

## Library scans and removal safety

A scan compares the files on disk with the index. Tracks whose files are missing are removed from the index, but an unmounted share looks like an empty folder, so removal is held back when the evidence is weak. A scan holds the missing tracks (it removes nothing, logs `not removing N missing track(s)`, and records the count as `missing_held` on the task) when:

- any folder or file could not be read, or
- no media files were found at all (is the folder mounted?), or
- more than 20 tracks are missing and they are more than 20% of that root's tracks.

Everything else the scan did (new and changed files) is kept. To remove the held tracks once you are sure the files are really gone, run a scan with mode `confirm_removals` (`POST /api/admin/roots/{id}/scan?mode=confirm_removals`; the Admin panel offers it as "Remove missing tracks"). It is a normal scan that skips the size checks. Removal also deletes every user's favourites, playlist entries and play history for those tracks, and a later rescan does not bring them back. An unreadable folder still holds removal, even when confirmed. A stopped scan never runs its removal step.

### Moved and renamed files

A track that is missing from its old path and turns up at a new one in the same library root keeps its row, and with it every user's favourites, playlist entries, play history and dislikes. The scan recognises it as the same file (same size and modification time, or same size and file name), or, for a file that was renamed and rewritten at once by a tagger, as the same recording (same MusicBrainz recording id, or same title, artist, album and track number at the same length). Each old row is matched at most once and only on a clear match; a copy whose original is still in place is a new track. So renaming an album folder, or moving the whole library into a subfolder of the root, changes paths and nothing else, and does not trip the removal hold. A move from one library root to another is not recognised: it is a removal in one root and an addition in the other.

## Listening data

Besides counted plays, the server keeps how each listen ended, in the `listen_events` table. Each row is one track or music video a device started and then left: the user, the item, when it started and ended, seconds heard, the item's length, the position, the outcome (`finished`, `skipped`, `stopped`, `transferred`, `error` or `unknown`), what started it (`auto` or `user`) and the kind of list it came from (album, artist, mix, playlist or library). It is stored whether or not the listen counted as a play. The mixes learn from it: see [mixes.md](mixes.md) and [playback-protocol.md](playback-protocol.md).

- It is never sent to ListenBrainz. Scrobbles come only from counted plays.
- It is deleted with the user, and with a track when the track is removed from the index.
- It follows `ATRIOLYRIC_PLAY_HISTORY_RETENTION_DAYS`: rows that started longer ago than that are deleted by the maintenance run, in batches. With `0` (keep forever) age never deletes them.
- Whatever the retention, each user keeps at most their newest 50,000 listens; older ones are deleted by the same maintenance run.

Like play history, it is a per-user listening history. It is not written to the log (see the "Logs" section of [configuration.md](configuration.md#logs)).

## Activity log

Admins see a stored activity log (Admin → Server activity; `GET /api/admin/activity`, with `kind`, `level`, `day`, `before_id` and `limit` filters; `GET /api/admin/activity/download` returns the same as text). It holds one short line per event in the `activity_events` table: sign-ins and failed sign-ins, every named action the app log calls `app.activity` (library folders, users, tag edits, task stops...), each library task's start and finish with its added/updated/removed counts, and every request that failed with a server error (method and path only, with the exception's type).

- It also holds the application log's own lines (`app.log_mirror`), so the page shows what `docker logs` would without the per-request noise: every warning and error from the app's loggers as `log.<area>` (`log.scanner`, `log.loudness`, `log.metadata_providers`...), the background work's INFO lines (a scan's progress, a mix built, the cache warm-up finishing, maintenance), and requests that failed with their reason (`request.failed`) or were slow (`request.slow`). An exception is stored as its type and message, not a traceback.
- Left out on purpose: the one-line-per-request, per-socket-frame and per-stream INFO lines (routine, and with verbose logging on they carry client addresses and what each user played), and anything already stored as a named event. An INFO message repeated over and over (a progress line) is kept 15 times in a row and then once every 10 minutes; warnings and errors are never limited.
- Filters: `kind` is an exact kind or a group's prefix with its dot (`log.`, `task.`); `level` is `info`, `warning`, `error` or `problems` (warnings and errors together).
- It never stores a password, token, API key, cookie, query string or request body. A failed sign-in for an unknown name stores a short hash of that name, as the log does.
- Events are written by one background thread, a batch per short transaction, so recording one never slows or fails the request it describes; if the database is busy the event is logged as dropped.
- It follows `ATRIOLYRIC_ACTIVITY_LOG_RETENTION_DAYS` (default 30, `0` keeps everything) and is capped at `ATRIOLYRIC_ACTIVITY_LOG_MAX_ROWS` rows (default 50,000); the maintenance run deletes the rest in batches.

## Audio quality

A scan stores each track's codec, bitrate, sample rate and bit depth (bit depth only for lossless formats), read from the same stream header as the duration. The app shows them as a short label ("FLAC 16/44", "MP3 320") and Library info sums them up. A normal scan also reads the header of any track that has none yet (no tags, no artwork); "Rescan metadata" refreshes them for every file.

## Volume levelling (loudness pass)

Volume levelling needs a ReplayGain track gain for each track. A scan stores the gain tags a file carries. A background pass covers the rest: it starts with the server and after each scan, reads each unchecked track's tags and measures a file with no track gain tag with ffmpeg's EBU R128 filter (under `nice -n 10`, one file at a time). Every track it visits is marked checked, even when ffmpeg cannot measure it, so a file that fails is not tried again until it changes on disk. A first pass over a large untagged library can take days.

**Where to see progress.**

- Admin → Background work → "Volume levelling": how many tracks have a value out of all tracks (with a progress bar), how many could not be measured, how many are left, and the state: "Measuring now", "Up to date", or "Not running right now" when tracks are left between runs (the next scan or a restart starts it). While it runs, a rough time left appears once the run has gone on for 5 minutes and checked 200 tracks. The figures come from `GET /api/admin/background-status` (admins only); the counts are at most 15 seconds old.
- The log (INFO), once as a pass starts, then every 100 tracks, then at the end:

  ```
  Loudness: 14,210 of 135,402 tracks have a value, 318 could not be measured, 120,874 to go
  Loudness backfill: 3000 / 120298 tracks checked, 2987 values stored
  Loudness backfill complete: 2987 values stored
  ```

  "Checked" counts the tracks this run visited and "values stored" the ones that gained a gain, peak or album gain in this run. The first line counts the whole library.

## Audio analysis

A background pass computes a "sounds like" fingerprint for each track, used by mixes and autoplay for similar tracks and for the order of a mix (see [mixes.md](mixes.md)). It runs inside the server and sends nothing out.

**Cost.**

- It decodes at most 90 seconds of a track (three 30-second windows for a normal song) with one ffmpeg at a time, run under `nice -n 10`, and a little numpy work on one core at a lowered priority.
- By default it waits while the loudness pass is measuring (`Loudness backfill: N / M tracks checked, K values stored` in the log; Admin → Background work says "Waiting for volume levelling to finish"), so only one heavy ffmpeg runs at a time. It starts when the loudness pass has nothing left. With `ATRIOLYRIC_AUDIO_ANALYSIS_CONCURRENCY` set to N, N - 1 workers run alongside the loudness pass while it measures (none at 1) and N once it is done.
- Tracks you play go first: most recently played, then most played, then the rest. "Sounds like" therefore works for the music in rotation long before a big library is through.
- How long: on a desktop PC about 0.14 s per track; a NAS a few times slower. A 100,000-track library is a few hours on a PC and may be most of a day on a NAS, spread over days if the loudness pass is also running. It resumes after a restart. After it is through, it looks again every 10 minutes for tracks a scan added.
- A file that cannot be analysed (missing, unreadable, too short or silent) is recorded as failed and not tried again until the file changes.

**Where to see progress.**

- Admin → Background work → "Sound analysis": how many tracks are analysed out of all tracks (with a progress bar), how many could not be analysed, how many are left, and the state: "Measuring now", "Waiting for volume levelling to finish", "Up to date", "Not running right now" (tracks are left and the pass is between runs; it looks again within 10 minutes) or "Turned off" (`ATRIOLYRIC_AUDIO_ANALYSIS=false`; vectors already stored still count). While it runs, a rough time left appears once the run has gone on for 5 minutes and handled 200 tracks.
- The log (INFO): when a pass starts with tracks to do, then one line per 100 tracks, then at the end:

  ```
  Audio analysis: 14,210 of 135,402 tracks analysed, 318 could not be analysed, 120,874 to go
  Audio analysis: 400 / 120874 tracks, 396 analysed
  Audio analysis complete: 120500 tracks analysed, 374 failed
  ```

  A track counts as analysed when its row comes from the current analyser version and the file has not changed since.

**Settings.** `ATRIOLYRIC_AUDIO_ANALYSIS=false` turns it off. Vectors already stored stay and are still used. `ATRIOLYRIC_AUDIO_ANALYSIS_CONCURRENCY` (1 to 16, default 1) sets how many tracks are analysed at once. On a NAS leave it at 1.

**Storage.** One row per analysed track in `audio_features`: a 180-byte vector (45 float32 values) plus a few small columns, a few hundred bytes in all. Failures get a row too. Rows are deleted with the track.

**Dependencies and memory.**

- It needs numpy (a dependency in `server/pyproject.toml`, wheels for x86_64 and aarch64; the image includes it). The app sets `OPENBLAS_NUM_THREADS=1` before numpy loads, so numpy does not start a thread per core for each matrix product. Set that variable yourself in the container if you want a different value; the app does not overwrite it.
- The similarity index holds every vector in memory as two float32 matrices of tracks x 45: about 48 MB at 135,000 tracks (24 MB each), plus a list of ids and a lookup table. It loads on the first mix that needs it, then picks up new rows at most every 30 seconds. With no analysed tracks nothing is loaded.

## Background cache warm-up

Some answers need a query over the whole library, which takes many seconds on a large one: the sidebar's tab counts, the Albums and Artists lists, the genre and tag counts (Specify mix, the genre filter) and Library info's totals. They are kept in memory (`app.library_cache`), and every library change (a scan that changed something, a tag edit, an enrichment run, the loudness pass, custom art, a deletion, a folder ignored) empties that cache. One background thread (`app.cache_warmup`) refills it, so a request does not have to:

- at startup, after every library change (once the library has been quiet for 5 seconds, so a burst of changes costs one pass) and just after each UTC midnight, since Library info's entry is kept per date;
- for every user's set of libraries, most-used first: tab counts, the counts for Admin → Background work, genre and tag counts, the Albums and Artists tabs as they first open (A–Z: first page and full list), then Library info. Other sorts and filters are computed when first asked for, and the Songs tab is read a page at a time and needs none of this;
- one step at a time on one core, without holding a read transaction across steps. A pass that is overtaken by another library change stops, and the next one starts over. The log has one line per finished pass: `Library caches warmed in 142.3s`.

A request for something the pass has not reached yet waits for that one computation (it is never run twice at once). Library info's library-wide numbers therefore change only after a library change or at the daily pass; a count that background work moves without a library change (lyrics fetched) shows from the next day. Your own listening figures and liked songs are read on every visit.

Admin → Background work is given the counts already in hand at once; when they are older than 15 seconds a background query fetches new ones for the page's next poll.

## Application logs

Everything goes to the container's stderr from INFO upwards, so it appears in `docker logs` (and your Docker UI's log view) next to uvicorn's own lines. Nothing is written to a log file inside the container: the read-only production root filesystem has nowhere to put one.

Defaults (`ATRIOLYRIC_LOG_LEVEL=INFO`, `ATRIOLYRIC_VERBOSE_LOGGING=false`): one line per API write, failure or slow request with user and request id; named events for sign-ins, account and library changes, scans and metadata tasks; uncaught browser errors; playback sockets opening and closing by user id and device id, state updates that could not be sent and playback errors the web app reports (kind and codes only); uvicorn's start-up, shutdown and error lines. No user agent or client address is logged except on sign-in events, and there is no per-user record of what was played (video transcoding and a failed audio transcode log the id of the item, without the user). Passwords and API keys are never logged; the one-time setup code is.

Uvicorn's access log is switched on, but the app filters it: its line for every request (which carries the client address) and its line for every WebSocket handshake are dropped unless verbose logging is on. Do not add `--no-access-log` to get the default behaviour; it is not needed, and it would take those lines out of verbose mode too. Successful reads, requests for the web app's own files and `/api/health` therefore leave no line at the default setting.

`ATRIOLYRIC_VERBOSE_LOGGING=true` adds every API request (reads and streams too) with address and user agent, uvicorn's access and WebSocket handshake lines, every playback WebSocket connection (address, user agent) and frame, the playback trail (device registrations, track ends, transfers and state updates with the id of the item playing) and the web app's media events. It builds a per-user listening history in the log, so turn it on only while debugging a problem and off again afterwards. The "Logs" section of [configuration.md](configuration.md#logs) lists exactly what each setting records.

`compose.production.yaml` caps `docker logs` at five 20 MB files. With the default json-file driver and no options the log is never rotated.

## Database backup and restore

SQLite runs in WAL mode, so a running database is three files: `atriolyric.db`, `atriolyric.db-wal` and `atriolyric.db-shm`. Committed changes can sit in the `-wal` file for a while. Copying `atriolyric.db` alone, or copying the three files while the app is running, can give a backup that is missing recent changes or is inconsistent.

For a filesystem-level backup, stop the application, then back up the complete `/data` directory (all three database files, and `.credential-key`). Without `.credential-key`, the encrypted ListenBrainz user tokens cannot be recovered and each user must paste theirs again. Do not restore a `.db` file together with a `-wal` or `-shm` file from a different moment.

To back up without stopping, use SQLite's online backup (`sqlite3 atriolyric.db ".backup 'copy.db'"`), which includes the WAL contents, and copy `.credential-key` separately.

The app opens the database with `synchronous=NORMAL`: a commit is written to the `-wal` file without waiting for the disk, and the disk is synced when the WAL is checkpointed. A crash of the app loses nothing. A power failure or a crash of the host can lose the last few commits (a saved playback position, a file the next scan finds again) but cannot corrupt the database. Every ten minutes the app also runs `PRAGMA optimize`, which keeps SQLite's table statistics current; nothing needs to be scheduled for that.

### Automatic pre-migration backups

When the server starts and an existing database has schema steps to run (see [migrations.md](migrations.md)), it first copies the database to `data/backups/atriolyric-v<N>-<UTC timestamp>.db`, where `<N>` is the schema version it is upgrading from. The copy is made with SQLite's online backup, so it includes the WAL contents. The newest three copies are kept; older ones are deleted. No copy is made for a fresh database or when the schema is already up to date (data backfills alone do not trigger one). Each copy is a single `.db` file. These copies are inside `/data`, so they are also covered by a `/data` backup and are not a substitute for an off-machine one.

To roll back an upgrade: stop the container, move the current `atriolyric.db`, `atriolyric.db-wal` and `atriolyric.db-shm` aside, copy the chosen file from `data/backups/` to `atriolyric.db`, and start the previous release's image.

### Refusing a newer schema

If the database's schema version is higher than the running release knows, the server refuses to start with a message saying it was upgraded by a newer Atriolyric. This protects against starting an old image on a database already changed by a newer one. Run the newer release again, or restore a backup. `ATRIOLYRIC_ALLOW_NEWER_SCHEMA=true` makes the server start anyway, leaving the database untouched, at your own risk: the old code may misread or break newer data. Use it for one start, then unset it.

### Before an upgrade

1. Stop the container and copy `/data` to a dated, access-restricted backup.
2. Restore that backup into a temporary directory and start the new image against the copy.
3. Check login, library counts, playlists, favorites, streaming, and an explicit scan.
4. Upgrade the real instance only after the restored copy passes.

To restore, stop the application, move the failed `/data` directory aside, restore the complete backup with the configured UID/GID ownership, and start the previous known-good image. Treat backups containing integration tokens as secrets.

### Caches and temporary files

Artwork, transcode, thumbnail, HLS and download-rendition caches are disposable and can be removed while stopped; the application regenerates them.

SQLite writes temporary files for large sorts and indexes. The image sets `SQLITE_TMPDIR=/data/tmp` so they go to the data volume, not `/tmp`, which the production overlay makes a 256 MB tmpfs. The app creates `tmp/` beside the database at startup. Keep `SQLITE_TMPDIR` inside the data folder and on a volume with free space; if it points at a small tmpfs, large sorts can run it out of space.
