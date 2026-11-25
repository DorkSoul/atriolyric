<p align="center">
  <img src="web/public/favicon.svg" width="96" height="96" alt="Atriolyric logo">
</p>

<h1 align="center">Atriolyric</h1>

<p align="center">
  A self-hosted music and video server for your own library. Fast browsing,
  playback that follows you between devices, mixes made from what you play,
  and an Android app that works offline. Your files stay on your server.
</p>

---

- [Features](#features)
- [Setup](#setup)
- [Using it from outside your home](#using-it-from-outside-your-home)
- [Updating](#updating)
- [How it works](#how-it-works)
- [Developer setup](#developer-setup)
- [Licence](#licence)

## Features

- **Your library, browsed like a streaming app.** Artists, albums, songs,
  folders and playlists, with search, sorting, genre filters and an A–Z jump
  bar. Stays fast with well over a hundred thousand tracks.
- **Playback that follows you.** The queue and position live on the server,
  so every open tab, phone and computer shows the same thing. Pause on your
  phone and it pauses on your desktop; move playback from one device to
  another with the device picker.
- **Mixes and autoplay.** Daily mixes from the artists you play together and
  from your genres and tags, Discovery, song, artist and album mixes, and
  autoplay when the queue ends. They learn from what you finish and what you
  skip. Similar-sounding tracks are worked out on your server from the audio
  itself. See [docs/mixes.md](docs/mixes.md).
- **Videos too.** Video files are browsed like music (a folder per show or
  film works without tagging) and transcoded on the fly when a browser
  cannot play them, with optional hardware encoding.
- **Now Playing.** Full-screen player with the queue, synced lyrics, genres
  and tags, and the audio quality ("FLAC 16/44").
- **Sound.** A quality cap with transcoding, adaptive quality on slow
  connections, volume levelling between tracks and an equaliser.
- **Likes, playlists and radio.** Like or dislike songs, albums and artists,
  build and reorder playlists, and listen to internet radio.
- **Metadata from free services.** Optional lookups against MusicBrainz,
  ListenBrainz, Cover Art Archive, Wikipedia and lrclib for missing artwork,
  artist photos, release types, credits and lyrics. None needs an account or
  key, and the app works fully offline without them. ListenBrainz scrobbling
  is available per user. See [docs/providers.md](docs/providers.md).
- **Want list.** Search MusicBrainz for music you don't own, see what is
  missing from an artist's discography, and export the list.
- **Several users.** Accounts with their own queue, likes and history, and
  per-library access.
- **Admin in the browser.** Add and scan library folders, edit tags, manage
  users and read what the server has been doing, without a shell. Removing a
  track only drops it from the library; Atriolyric never deletes your files.
- **Android app.** Native playback with lock-screen controls, downloads for
  offline listening, and the same app with or without a connection.
- **Works everywhere.** Desktop and mobile layouts, dark and light themes,
  and installable to a phone's home screen from the browser.

## Setup

You need a computer that stays on (a NAS, a home server or a spare PC) with
[Docker](https://docs.docker.com/get-docker/) installed. On a Synology,
QNAP, Unraid or TrueNAS box, Docker comes from the system's app store, and
the steps below work in its "Compose" or "Project" screen (or Portainer's
"Stacks").

**1. Create the compose file.** Make a folder for Atriolyric and save this as
`compose.yaml` inside it (the same file is in this repository as
[compose.example.yaml](compose.example.yaml)):

```yaml
services:
  atriolyric:
    image: ghcr.io/dorksoul/atriolyric:latest
    container_name: atriolyric
    restart: unless-stopped
    ports:
      - "8030:8000"
    volumes:
      - ./data:/data
      - /path/to/your/music:/media/music
```

Change `/path/to/your/music` to the folder that holds your music, for example
`/volume1/music` on a Synology or `/home/you/Music` on Linux. To add more
folders (videos, a second drive), add more lines, each with its own name
after `/media/`:

```yaml
      - /path/to/your/videos:/media/videos
```

`./data` is where Atriolyric keeps its database and caches. It is created
next to `compose.yaml`.

**2. Start it.** In that folder:

```bash
docker compose up -d
```

**3. Create your account.** Open `http://<server address>:8030` in a browser
(for example `http://192.168.1.20:8030`). The first visit shows a setup screen
that asks for a one-time **setup code**. The code is printed in the
container's log: run `docker logs atriolyric`, or open the container's log in
your NAS's Docker app or Portainer. Enter it and choose the admin username
and password.

**4. Add your music.** Go to **Admin → Libraries → Add root**, enter
`/media/music` (the name after the colon in your compose file) and press
**Scan**. Large libraries take a while on the first scan; you can browse and
play while it runs. Artwork, volume levelling and sound analysis carry on in
the background afterwards.

**5. Get the Android app (optional).** Download it from **Settings → About**
in the web app, or from the [releases page](https://github.com/DorkSoul/atriolyric/releases).
Enter the address you open in the browser when the app asks for a server.

### If something doesn't work

- **The page doesn't load.** Check the container is running
  (`docker ps`) and that nothing else uses port 8030. To use another port,
  change the first number: `"8040:8000"`.
- **The library is empty after a scan, or a scan reports an unreadable
  folder.** The app runs as user and group `1000`. If your music belongs to
  another user (common on a NAS), tell Atriolyric to run as that user. Find
  their ids with `id <username>` on the server, then add to the compose file:

  ```yaml
      environment:
        PUID: "1026"
        PGID: "100"
  ```

- **Hardware video transcoding** (Intel or AMD graphics): see the commented
  lines at the end of [compose.example.yaml](compose.example.yaml).

Everything else that can be changed is listed in
[docs/configuration.md](docs/configuration.md). None of it is needed for a
normal install.

## Using it from outside your home

The setup above works on your home network. To listen from anywhere, pick one
of these:

- **A VPN such as [Tailscale](https://tailscale.com/) or WireGuard.** The
  easiest and safest: install it on the server and on your devices, and use
  the server's VPN address. Nothing is opened to the internet.
- **A Cloudflare Tunnel.** No port forwarding and no public IP needed, and
  you get an HTTPS address on your own domain. You need a free Cloudflare
  account and a domain managed by Cloudflare.
  1. In the Cloudflare dashboard go to **Zero Trust → Networks → Tunnels**,
     create a tunnel and copy its token.
  2. Add this service to the same `compose.yaml`, below `atriolyric:`:

     ```yaml
       cloudflared:
         image: cloudflare/cloudflared:latest
         restart: unless-stopped
         command: tunnel run
         environment:
           TUNNEL_TOKEN: paste-your-token-here
     ```

  3. In the tunnel's **Public Hostname** settings, add a hostname (for
     example `music.yourdomain.com`) with service type `HTTP` and URL
     `atriolyric:8000`.
  4. Run `docker compose up -d` again.

  Cloudflare can see the traffic that passes through the tunnel, and its
  terms limit streaming large amounts of video through it; for a big video
  library a VPN or your own proxy is the better choice.
- **Port forwarding with a reverse proxy.** Forward ports 80 and 443 on your
  router to a reverse proxy such as [Caddy](https://caddyserver.com/), Nginx
  Proxy Manager or Traefik, which gets an HTTPS certificate and passes
  requests to `<server>:8030`. Always use HTTPS for this: never forward port
  8030 itself to the internet. See
  [docs/configuration.md](docs/configuration.md#behind-a-reverse-proxy) for
  the one setting a proxy needs (`FORWARDED_ALLOW_IPS`), and
  [compose.production.yaml](compose.production.yaml) for extra hardening.

Logins work over plain HTTP at home and over HTTPS from outside without
changing anything: the login cookie is HTTPS-only whenever the page was
opened over HTTPS.

## Updating

```bash
docker compose pull
docker compose up -d
```

The `latest` tag moves only on a release. Your data is upgraded on start, and
a copy of the database is saved to `data/backups/` first. Installed Android
apps offer the new version once the server has it. Backups and recovery are
covered in [docs/operations.md](docs/operations.md).

Image tags on `ghcr.io/dorksoul/atriolyric`:

| Tag | What it is |
|-----|------------|
| `latest` | The newest release |
| `X.Y.Z`, `X.Y` | A specific release, or the newest patch of `X.Y` |
| `edge` | The newest commit on `master`, untested between releases |

## How it works

| Part | Built with |
|------|------------|
| Server | Python, [FastAPI](https://fastapi.tiangolo.com/), SQLAlchemy, SQLite (WAL mode) |
| Web app | React, TypeScript, Vite |
| Media | ffmpeg (transcoding, artwork, sound analysis), mutagen (tags) |
| Live sync | A WebSocket per device carrying the shared playback state |
| Android | Capacitor shell around the web app, with native Media3 playback and downloads |
| Packaging | One Docker image holding the server and the built web app |

The server scans your folders into a SQLite database, serves the web app and
streams the files, transcoding when a device needs a smaller or different
format. Slow figures (counts, library statistics) are worked out in the
background, not while you wait. Nothing about your library leaves the server
except the lookups listed in [docs/providers.md](docs/providers.md), and each
of those can be turned off.

More reading:

- [docs/configuration.md](docs/configuration.md): every setting, reverse
  proxies, logs
- [docs/operations.md](docs/operations.md): transcoding, scans, background
  work, backups and restore
- [docs/mixes.md](docs/mixes.md): how mixes and autoplay pick tracks
- [docs/providers.md](docs/providers.md): the external services and what is
  sent to them
- [docs/playback-protocol.md](docs/playback-protocol.md): the playback
  WebSocket
- [mobile/README.md](mobile/README.md): the Android app

## Developer setup

Contributions are welcome. Requirements: Python 3.12, Node 22,
[uv](https://docs.astral.sh/uv/) and ffmpeg. For the Android app also a JDK
21 and the Android SDK (see [mobile/README.md](mobile/README.md)).

```bash
git clone https://github.com/DorkSoul/atriolyric.git
cd atriolyric

# Server, on port 8321 (the web dev server proxies to it)
cd server
uv sync
uv run uvicorn app.main:app --reload --port 8321

# Web app, in a second terminal
cd web
npm install
npm run dev
```

Open `http://localhost:3000`. The dev server proxies `/api` and `/ws` to the
server on 8321 and reloads on every change. The server keeps its database in
`server/data/`. To test against real files, add a folder on your machine as a
library root in the app.

### Running the tests

Run the tests on your own machine before opening a pull request; it is much
faster than waiting for CI.

```bash
# Server: lint, type-check, tests
cd server
uv run ruff check .
uv run pyright
uv run pytest -m "not network"

# Web app
cd web
npm run lint
npm run build              # also type-checks
npx playwright install chromium
npm run test:e2e -- --project=chromium   # browser tests against a mocked API
npm run test:real          # two browser "devices" against a real server
```

`scripts/local-gate.sh` runs all of these in the same containers CI uses
(needs Podman or Docker): `./scripts/local-gate.sh --plan` shows what it
would run. See [web/README.md](web/README.md) for what each browser suite
covers. Tests marked `network` call MusicBrainz and lrclib for real and are
left out above.

After changing an API response model in `server/app/schemas.py`, run
`npm run gen:api-types` in `web/` and commit `web/src/api-types.ts`.

Database changes go through ordered steps in `server/app/migrations.py`; see
[docs/migrations.md](docs/migrations.md).

### Contributing

1. Fork the repository and create a branch from `master`.
2. Make your change, with tests, and run the tests above.
3. Open a pull request into `master`. The test workflow runs on pull
   requests only, so it is the check your change has to pass.

Releases and the Android build are described in
[docs/releasing.md](docs/releasing.md).

### Project layout

```
server/app/          FastAPI app: routers/ (API), discovery/ (mixes),
                     metadata_providers/ (external services), scanner,
                     playback, transcoding, migrations
server/tests/        pytest suite
web/src/             React app: components/, player/, native/ (Android bridge)
web/tests/           Playwright suites: e2e/ (mocked), real/ (two devices)
mobile/              Android app (Capacitor + Kotlin)
docker/              Container entrypoint
docs/                Configuration, operations, mixes, providers, releasing
scripts/local-gate.sh  Runs the whole test gate locally
```

## Licence

Atriolyric is free software, licensed under the
[GNU Affero General Public License v3.0 or later](LICENSE). If you run a
modified version for other people over a network, the AGPL requires you to
offer them its source code.
