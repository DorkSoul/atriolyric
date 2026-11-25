# Configuration

Everything here is optional: the image works with no environment variables
set. Add the ones you need under `environment:` in your `compose.yaml`.

Settings are environment variables. Those in `server/app/config.py` are
prefixed `ATRIOLYRIC_`; rows that say "Not an `ATRIOLYRIC_` setting" are the
exceptions. Booleans take
`true`/`false` (also `1`/`0`, `yes`/`no`). A value that does not parse stops the
server at startup. Unset variables take the default shown.

| Variable | Format | Default | Description |
|----------|--------|---------|-------------|
| `ATRIOLYRIC_DATABASE_PATH` | path | `data/atriolyric.db` (the image sets `/data/atriolyric.db`) | SQLite database file. Thumbnails, art, caches, backups and the credential key live in the same folder |
| `ATRIOLYRIC_WEB_DIST_PATH` | path | unset (the image sets `/app/web`) | Built frontend to serve. Leave unset in development, where Vite serves it |
| `ATRIOLYRIC_DEFAULT_USERNAME` | text | `admin` | Username of the admin created at first boot when a password is set |
| `ATRIOLYRIC_DEFAULT_PASSWORD` | text | unset | Creates the first admin at boot, for headless installs. Unset: the setup screen asks for the one-time setup code from the log |
| `ATRIOLYRIC_COOKIE_SECURE` | `auto`, `true`, `false` | `auto` | Whether the session cookie is sent over HTTPS only. `auto` marks it HTTPS-only (and sends `Strict-Transport-Security`) when the request arrived over HTTPS, directly or through a reverse proxy that sets `X-Forwarded-Proto: https`, so both a plain-HTTP home install and an HTTPS one work without setting anything. `true` always marks it, `false` never does |
| `ATRIOLYRIC_ALLOWED_ORIGINS` | JSON list of origins | `[]` | Extra origins, beyond the page's own host, allowed to call the API and open the playback WebSocket, e.g. `["https://music.example.com"]`. A plain comma-separated string fails to parse |
| `ATRIOLYRIC_ALLOW_PRIVATE_RADIO_HOSTS` | boolean | `false` | Allow radio streams that resolve to private or internal addresses |
| `ATRIOLYRIC_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` (any case) | `INFO` | Verbosity of the app's own log lines |
| `ATRIOLYRIC_VERBOSE_LOGGING` | boolean | `false` | Debug tracing: every API request with user, address and user agent, every playback WebSocket frame, and the web app's media events. Very chatty and privacy-sensitive. See Logs below |
| `ATRIOLYRIC_HWACCEL` | `auto`, `vaapi`, `none` (any case) | `auto` | Video encoder for HLS transcoding. See [docs/operations.md](docs/operations.md) |
| `ATRIOLYRIC_HLS_MAX_SESSIONS` | integer | `4` | Concurrent video transcodes. A video and its audio-only background rendition each take one. Values below 1 act as 1 |
| `ATRIOLYRIC_HLS_CACHE_MAX_BYTES` | integer (bytes) | `4294967296` (4 GiB) | Cap on cached HLS segments |
| `ATRIOLYRIC_CACHE_MAX_BYTES` | integer (bytes) | `21474836480` (20 GiB) | Budget for the audio transcode cache (`transcodes/`); least recently used files are evicted first |
| `ATRIOLYRIC_RENDITION_CACHE_MAX_BYTES` | integer (bytes) | `21474836480` (20 GiB) | Budget for the video download renditions (`renditions/`: a video as one MP4 at 1080p, 720p or 480p, made when an app asks to download it); least recently used files are evicted first. See [docs/operations.md](docs/operations.md) |
| `ATRIOLYRIC_ANDROID_APK_PATH` | file path | unset (the image sets `/app/android/atriolyric.apk`) | The Android app's signed APK. When the file exists the server offers it for download in Settings > About and installed apps can update from it. See [docs/releasing.md](docs/releasing.md) |
| `ATRIOLYRIC_PLAY_HISTORY_RETENTION_DAYS` | integer (days, 0 or more) | `0` (keep forever) | Play history older than this is deleted by the maintenance run every 10 minutes, in batches. Play counts, Most played, Recently played and Discovery all read it, so they lose older plays. Negative values fail at startup |
| `ATRIOLYRIC_ACTIVITY_LOG_RETENTION_DAYS` | integer (days, 0 or more) | `30` | The admin's stored activity log (Admin → Server activity: sign-ins, scans, edits, failed and slow requests, the server log's warnings, errors and background work) keeps this many days; `0` keeps it forever. Pruned by the maintenance run every 10 minutes |
| `ATRIOLYRIC_ACTIVITY_LOG_MAX_ROWS` | integer (100 or more) | `50000` | The activity log never holds more rows than this, whatever the retention; the oldest go first |
| `ATRIOLYRIC_MIX_MIN_TRACK_SECONDS` | number (seconds, 0 or more) | `45` | Mixes, Discovery, Song Mix, artist/album mixes and autoplay leave out shorter tracks (intros, skits, interludes). A track of unknown length is kept; a song you pick yourself (a Song Mix's seed, an album, a playlist) is never filtered |
| `ATRIOLYRIC_MIX_MAX_TRACK_SECONDS` | number (seconds, above 0) | `1200` (20 minutes) | The same, for longer tracks (DJ mixes, whole albums or concerts in one file) |
| `ATRIOLYRIC_MIX_ALLOW_LIVE_ALBUMS` | boolean | `true` | Whether those automatic picks may use tracks from albums MusicBrainz types as Live. Spoken word, interview, audiobook and audio drama albums are always left out |
| `ATRIOLYRIC_AUTOPLAY_RECENT_HOURS` | number (hours, 0 or more) | `24` | Autoplay (what plays after the queue runs out) skips tracks played this recently, besides those already in the queue; it only lets them back when nothing else in the library qualifies. `0` turns the play-history check off |
| `ATRIOLYRIC_MIX_BLOCKED_LABELS` | JSON list of strings | `[]` | Extra genre and tag names that never become an automatic mix or count as two artists being alike, on top of the built-in list of non-genre tags ("seen live", "favourites", ...). Compared lower case, e.g. `["my uploads", "car"]`. Specify mix still offers them. See [docs/mixes.md](docs/mixes.md) |
| `ATRIOLYRIC_AUDIO_ANALYSIS` | boolean | `true` | Background pass that decodes up to 90 seconds of each track with ffmpeg and stores a "sounds like" vector, used for similar tracks and track order in mixes. Runs once the loudness pass is done. `false` turns it off; vectors already stored are still used. See [docs/operations.md](docs/operations.md) |
| `ATRIOLYRIC_AUDIO_ANALYSIS_CONCURRENCY` | integer (1 to 16) | `1` | Tracks analysed at once, each one niced ffmpeg plus numpy work on one core. With `1`, analysis waits while the loudness pass is measuring; each step above 1 adds a worker that runs alongside it. Values outside 1 to 16 fail at startup |
| `ATRIOLYRIC_AUDIO_EXTENSIONS` | JSON list of strings | `[".mp3", ".flac", ".ogg", ".m4a", ".wav", ".opus", ".wma", ".aac"]` | File extensions a scan treats as audio. Lower case, with the dot |
| `ATRIOLYRIC_VIDEO_EXTENSIONS` | JSON list of strings | `[".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"]` | File extensions a scan treats as video |
| `ATRIOLYRIC_USER_AGENT` | text | `atriolyric/<version> (+https://github.com/DorkSoul/atriolyric)`, with the running release's version | `User-Agent` sent to MusicBrainz, ListenBrainz, Deezer, Cover Art Archive, Wikipedia, Wikidata, Wikimedia Commons, lrclib and radio streams. MusicBrainz asks for a contact address; set one here if you use it heavily |
| `ATRIOLYRIC_ALLOW_NEWER_SCHEMA` | `1`, `true` or `yes` | unset | Start anyway when the database was upgraded by a newer release. Not a `Settings` field: `migrations.py` reads it directly. See [docs/operations.md](docs/operations.md) |
| `SQLITE_TMPDIR` | path | `/data/tmp` in the Docker image; unset elsewhere | Not an `ATRIOLYRIC_` setting. Where SQLite writes temporary files for large sorts. The app also creates `tmp/` beside the database at startup |
| `PUID`, `PGID` | numbers | `1000` | Not an `ATRIOLYRIC_` setting. The user and group the server runs as. When the container starts as root (the default) it gives `/data` to this user first. Pick a host user that can read your media folders |
| `TZ` | time zone name | `UTC` | Not an `ATRIOLYRIC_` setting. Time zone for the times in the container log, e.g. `Europe/London` |
| `FORWARDED_ALLOW_IPS` | comma-separated addresses or CIDR ranges | `127.0.0.1` | Not an `ATRIOLYRIC_` setting. Uvicorn's list of peers whose `X-Forwarded-For` is believed. Set it behind a reverse proxy (see below) |
| `ATRIOLYRIC_DEV_HOST` | address | `127.0.0.1` | Development only, read by `web/vite.config.ts`. Address the Vite dev server binds to. Use `0.0.0.0` only when a published dev port has to reach it |

Unknown `ATRIOLYRIC_*` variables are ignored: a name that is not in the table
does nothing (for example `ATRIOLYRIC_COOKIE_SAMESITE` is not read).

## Behind a reverse proxy

Uvicorn replaces the client address with the one in `X-Forwarded-For` only for
peers listed in `FORWARDED_ALLOW_IPS` (default: `127.0.0.1`). When the proxy
is another container, every request arrives from the proxy's address, so
unless you set the variable the login limiter counts the whole internet as one
client (twenty wrong passwords from anywhere lock everyone out) and the logs
cannot say who did what. Set it to the proxy's address, or the Docker
network's subnet, for example `FORWARDED_ALLOW_IPS: "172.18.0.0/16"`
(pin the subnet in your stack so it cannot change). Do not use `*` unless
this container's port is reachable only through the proxy: any client that can
connect directly could then claim any address. Your proxy must also set
`X-Forwarded-For` itself and not pass on a client's value untouched.

If a request arrives with `X-Forwarded-For` from a peer uvicorn does not trust,
the server logs one warning (naming the peer) so a missing setting is visible.
Login attempts are limited per address (20 failures in 5 minutes) and per
username (10 failures in 5 minutes). Only failed password checks count; a
refused attempt does not, so a block simply ends when the oldest failure is
5 minutes old and cannot be prolonged by retrying. A successful sign-in clears
only that username's failures.

## Logs

Everything goes to the container log (`docker logs atriolyric`). At the
default setting you get:

- one line per API write, failure or slow request, with the signed-in user, a
  request id (also sent back as the `X-Request-ID` header) and, for failures,
  the reason: `POST /api/admin/roots -> 409 (4ms) user=admin#1 req=ab12cd34 detail="Library root already exists"`
- named events for important actions: sign-ins and failed sign-ins, account
  and library-path changes, access grants, scan/metadata task start and
  finish (with a traceback if one fails)
- uncaught browser errors, reported by the web app as `client.error`
- playback connections by user id and device id: a device's socket opening
  and closing (with the reason, such as a silent or unreachable socket), a
  state update that could not be sent, and a playback error the web app
  reports (its kind and codes, not which item was playing)
- uvicorn's own start-up, shutdown and error lines

Successful reads (pages, streams, artwork) are not logged at this setting:
uvicorn's access line for every request, which carries the client address,
and its line for every WebSocket handshake are left out.

Verbose logging (`ATRIOLYRIC_VERBOSE_LOGGING=true`) is **off by default**.
Turn it on only while debugging, because it adds client addresses and a
per-user listening history to the log:

- every API request, reads and streams included, with the user, the client
  address, the `Range` header, the response size and the browser's user agent
- uvicorn's access line for every request (files of the web app included,
  with the client address and the query string) and its WebSocket handshake
  line
- every playback WebSocket connection with its address and user agent, every
  frame, and what the server did with it: device registrations, track ends,
  transfers and each state update, with the id of the item playing
- the web app's own debug events (audio element, socket and page lifecycle
  events, posted every few seconds); the web app only starts sending them
  when the server reports that verbose logging is on

At the default setting the log holds only the first list: no user agent and
no client address except on sign-in events, and no per-user record of what
was played (video transcoding and a failed audio transcode log the id of the
item, without the user). Passwords and API keys are not written to
the log, and a login name that does not belong to an account (people type
passwords there) is logged only as a short hash. The one-time setup code is
logged on purpose, as first-run setup needs it. `compose.production.yaml` rotates the
container log (`max-size: 20m`, `max-file: 5`); without a `logging:` setting
Docker keeps it forever.

If you hit a bug, include the log lines around it and the request id.

Metadata/lyrics lookups (MusicBrainz, ListenBrainz, Deezer, Cover Art
Archive, Wikipedia, Wikimedia Commons, lrclib)
work out of the box with no API key — they're all free,
unauthenticated public APIs — and every one of them fails gracefully if
unreachable, so the app works fine fully offline too.
