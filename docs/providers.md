# External services

This page lists every outside service the Atriolyric server talks to: what it sends, when, how often, what it keeps, and how to turn it off. Every number comes from the code under `server/app`; each section ends with a short "Source" note so you can check it. Paths below are relative to `server/app`.

## What leaves the server

The server sends requests to music metadata services (MusicBrainz, Cover Art Archive, ListenBrainz, Deezer, lrclib, Wikipedia, Wikidata and Wikimedia Commons) and to internet radio stations. What it sends is mostly names and IDs from your library: artist, album and track titles, MusicBrainz IDs (MBIDs), and track lengths. Scrobbling also sends each linked user's ListenBrainz user token with what they played. Every request carries the app's User-Agent (see below). The services see the server's IP address, not the listeners'.

The browser talks to the server only, with two exceptions that the Content Security Policy allows:

- The art picker shows Cover Art Archive and Wikimedia Commons thumbnails, and Explore shows Deezer covers. The browser loads those images straight from `coverartarchive.org`, `*.archive.org`, `upload.wikimedia.org`, `thumb.wikimedia.org` and `*.dzcdn.net`.
- Explore's 30-second Deezer previews play straight from `*.dzcdn.net`.

Those requests come from the listener's browser and carry the listener's IP address. Every other image is fetched by the server and served from the app's own origin.

Source: `security_headers.py:36-57`.

## Summary

| Service | Used for | Key needed | Rate in code | Cache |
|---------|----------|------------|--------------|-------|
| MusicBrainz | Track matching, genres and tags, artist/album/track info, want list search and catalogues | No | 1 request per second (`Throttle(1.0)`) | 30 days (searches 1 day); recording data kept on the track |
| Cover Art Archive | Album cover URLs, art picker, want list covers | No | No spacing (`Throttle(0.0)`); pauses on 429/503 | Front image URL for the process life; image lists 30 days; downloaded files kept |
| ListenBrainz | Similar artists and songs, top tracks, listener and play counts, artist tags; scrobbling | Reads: no, except top tracks (ListenBrainz answers 401 without a token; the requesting user's linked token is sent, and without one only cached top tracks show). Scrobbling: the user's own user token | At most one request every 0.4 s (`Throttle(0.4)`), shared by reads, token checks and scrobbles; honours its rate-limit headers | 14 to 30 days; scrobble queue up to 14 days |
| Deezer | Explore previews (live only) | No | No spacing (`Throttle(0.0)`); an Explore refresh waits 0.2 s between calls; pauses on quota | Nothing stored; answers kept in process memory for 6 hours, a failure for 60 s |
| lrclib | Lyrics when the file has none | No | At most one request every 0.3 s (`Throttle(0.3)`); pauses on 429/503 | Lyrics kept on the track; a miss for 6 hours |
| Wikipedia / Wikidata / Wikimedia Commons | Artist bios; artist photos | No | 5 per second (`Throttle(0.2)`), shared | 30 days |
| Internet radio (stations added by URL, plus a few defaults) | Radio playback, relayed by the server | No | One connection per listener; at most 2 per user, 8 in total | "Now playing" in memory only |

## How the limits work

All throttled providers use one `Throttle` per provider, shared by the whole process (`metadata_providers/_throttle.py`):

- A throttle starts requests on a fixed cadence: never closer together than its `min_interval`, counted from the last scheduled slot (`_throttle.py:26-39`). An interval of `0.0` means no spacing at all; the throttle then only carries pauses.
- A page request (interactive) gives up instead of queueing when its slot is more than `INTERACTIVE_MAX_WAIT` = 15 seconds away (`_throttle.py:12`, `_throttle.py:71-77`). The page then shows "busy - try again shortly".
- Background work (inside `bulk()`) waits while any page request is waiting, so pages go first (`_throttle.py:35-38`, `_throttle.py:60-69`). The provider queue's background worker runs one job at a time inside `bulk()` (`provider_queue.py`).
- The provider queue's background worker keeps two lists. Jobs a user's request caused (a catalogue cover, a wanted entry's data, an artist re-check, a wanted artist's tracklists) run in the order they were queued. Scheduled jobs (the want list catalogue re-browse and data backfill) run only when no user job is waiting. A scheduled job that is already running hands the worker to waiting user jobs between its units of work (each wanted artist, each want list entry, each release group of a wanted artist), so a user job waits at most for the one request the scheduled job is in the middle of. Look-ups a page is waiting to show (info dialogs, the first look at a catalogue page) run on a second worker, outside `bulk()`.
- Both workers' queues are bounded (`provider_queue.py`). The background worker holds at most `MAX_PENDING` = 1000 waiting user jobs and `MAX_SCHEDULED_PENDING` = 20 scheduled ones; the second worker at most `MAX_INTERACTIVE_PENDING` = 200. Where the user who caused a job is known, that user may have at most `MAX_PENDING_PER_USER` = 200 background jobs and `MAX_INTERACTIVE_PER_USER` = 50 page look-ups queued or running. A job over a limit is refused and nothing is kept. The page then shows what is stored, as it does when nothing is cached: a catalogue cover answers 204 with the 30-second cache life, so the tile asks again; an info or catalogue request answers from stored data with `pending` false, so the page stops polling (a catalogue page with nothing stored answers 502 "try again shortly", as in a provider outage); a re-check of a saved catalogue is skipped and `refreshing` is false. The next request tries to queue again. Jobs with the same key are still queued only once.
- When a provider answers with a rate limit, the throttle stops all calls to it for the `Retry-After` time (`_throttle.py:47-52`). A `Retry-After` longer than `MAX_RETRY_AFTER` = 120 seconds is cut to 120 (`metadata_providers/result.py:38`, `result.py:91`, `result.py:111`). Where a provider gives no `Retry-After`, some modules use `DEFAULT_RATE_LIMIT_PAUSE` = 10 seconds (`result.py:40`); see each section.
- Admin library tasks ride out provider outages: each failure in a row pauses the task a little longer (2, 4, 8 ... s, at most `FAILURE_PAUSE_CAP` = 300 seconds). A task stops only when a streak of at least `PROVIDER_FAILURE_LIMIT` = 5 failures has paused for `OUTAGE_PAUSE_BUDGET` = 900 seconds, which is the 11th failure in a row after about 18 minutes (`library_tasks.py`, `_FailureStreak`). Items that failed in a streak are not cached, so the next run or opening them fetches them again.
- MusicBrainz redirects are followed: a merged entity's old MBID answers 301 to the entity it was merged into.

## The shared cache

Most provider answers are stored in the `external_cache` table (`external_cache.py`):

- A fresh answer is served without asking again. The default lifetime is `DEFAULT_TTL` = 30 days (`external_cache.py:29`). Each caller may set its own; they are listed per service below.
- An empty answer ("the provider had nothing") is kept for `DEFAULT_MISS_TTL` = 1 day (`external_cache.py:30`). No caller changes this.
- A failed request is never cached, so it is tried again next time. A stale answer is still served when the refresh fails (`external_cache.py:129-132`).
- The "refresh" buttons in info views pass `refresh=True`, which asks the provider again (`external_cache.py:113`, `external_cache.py:125`).
- Scheduled maintenance deletes rows older than `MAX_AGE` = 90 days, and empty answers older than `MAX_MISS_AGE` = 7 days (`external_cache.py:141-163`). It runs on the scheduler tick, every `DISCOVERY_SCHEDULER_INTERVAL_SECONDS` = 600 seconds (`main.py:272`, `main.py:342-358`, `main.py:375-382`).

## User-Agent

Every outbound request sends the same User-Agent. By default it is:

```
atriolyric/<version> (+https://github.com/DorkSoul/atriolyric)
```

`<version>` comes from the built frontend's `version.json`, or `0.0.0+dev` without a build. Set `ATRIOLYRIC_USER_AGENT` to replace it, for example to add your own contact address.

Requests that send it: MusicBrainz (`metadata_providers/musicbrainz.py:75`), Cover Art Archive (`coverart.py:34`), ListenBrainz reads, token checks and scrobbles (`listenbrainz.py:90-94`), Deezer (`deezer.py:40`), lrclib (`lyrics.py:105`, `lyrics.py:121`), Wikipedia, Wikidata and Wikimedia Commons (`wikipedia.py:35`), art downloads (`art.py:190`) and radio streams (`radio.py:197`).

Source: `version.py:13-14`, `version.py:35-36`; `config.py:11` (prefix `ATRIOLYRIC_`), `config.py:56`, `config.py:93-97`.

## MusicBrainz

**What is sent.** GET requests to `https://musicbrainz.org/ws/2`:

- Recording search by artist and title, ISRC lookup, and recording lookup by MBID (with credits, genres, tags, releases and relations).
- Artist, release group and release lookups by MBID; artist, release group and recording searches by name; release group browsing by artist MBID.

**When.**

- Admin library tasks started from the admin page: "MusicBrainz IDs", "MusicBrainz metadata", "Full setup" and "Artist info". A library scan reads only local tags; it never calls out.
- Page views: artist, album and track info, missing releases (catalogue), the art picker and want list search. The artist lookup also gives the Wikipedia and Wikidata links used for bios and photos.
- Artist top tracks and the artist info dialog: when a library artist has no MBID yet, a name search finds one (`metadata_info.resolve_artist_mbid`) and saves it on the artist. Mixes never do this; they ask ListenBrainz only by an MBID already stored.
- Scheduled: on each 600-second tick the server queues a re-browse of wanted artists whose catalogue is older than `WANTED_REFRESH` = 7 days, and a backfill of missing want list data. Both wait for user-triggered jobs on the provider queue (see "How the limits work").

**How often.** At most one request per second across the process (`Throttle(min_interval=1.0)`). A 429 or 503 pauses all MusicBrainz calls for the `Retry-After` time, or 10 seconds without one. Timeouts: 10 s for recording search and ISRC lookup, 15 s for name searches, 20 s for lookups and browse pages. Browsing an artist's release groups fetches pages of 100, at most 30 pages. A want list search makes three requests (artists, release groups, recordings).

**What is kept.**

- `external_cache`: artist, release, release group and artist search lookups for `MB_TTL` = 30 days; release group catalogues for `CATALOG_TTL` = 30 days; want list recording lookups for 30 days; want list searches for `SEARCH_TTL` = 1 day.
- On each track: the full recording response (`musicbrainz_metadata_json`), the time it was fetched (`musicbrainz_fetched_at`), the matched MBIDs, genres and tags. The "MusicBrainz metadata" task only fetches tracks without stored data, unless run in its "all" mode.

**Key.** None.

**Terms.** [Rate limiting](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting). The provider asks for about 1 request per second on average per IP address, and a User-Agent with the application name, version and contact details.

Source: `metadata_providers/musicbrainz.py:16-17`, `:75-76`, `:87-90`, `:152`, `:168`, `:185`, `:207`, `:209`, `:216`, `:236`, `:246`, `:256`, `:277`, `:297`, `:305`; `result.py:40`; `metadata_info.py:40-52`, `:117-144`; `listenbrainz_data.py:50-79`; `catalog.py:33-35`, `:38-60`; `want_metadata.py:186-197`; `want_list.py:70`, `:699-715`; `enrichment.py:194-195`; `library_tasks.py:278`, `:344`, `:596-631`, `:719-740`; `main.py:272`, `:396-410`; `models.py:302-307`.

## Cover Art Archive

**What is sent.**

- GET `https://coverartarchive.org/release/<release MBID>`, to find a release's front cover or list all its images. When that edition has no front cover archived, GET `https://coverartarchive.org/release-group/<release group MBID>` for the cover of another edition of the same album.
- Image downloads from Cover Art Archive (which redirects to `archive.org`) for track art and want list covers, including `https://coverartarchive.org/release-group/<MBID>/front-500`.

Album covers are looked for in this order: the picture embedded in the file, a cover image in the track's own folder (`cover`, `folder`, `front`, `album`, `albumart`… with `.jpg`, `.jpeg`, `.png`, `.webp` or `.gif`; never a symlink; each folder is listed once until it changes), the release's front cover, then the release group's. An album's or artist's tile takes its picture from the lowest-numbered track not already known to have no art (the startup art pass marks such tracks until their file changes), so one folder without covers does not leave the whole album blank.

**When.** The admin "Cover art" task (also part of "Full setup"); the art picker; want list cover art (page views and the scheduled backfill); and the first time a track's art is shown when its cover was found remotely and no copy is stored yet.

**How often.** No spacing of its own (`Throttle(0.0)`). A 429 or 503 pauses calls for the `Retry-After` time, if one is given. Timeout 10 s. Art downloads: timeout 10 s, up to 4 redirect hops, at most `MAX_ART_BYTES` = 10 MiB, HTTPS only, public addresses only, and only from `coverartarchive.org`, `archive.org`, `upload.wikimedia.org` and `thumb.wikimedia.org` or their subdomains (`ART_HOSTS`). Deezer images are never downloaded. A failed download (not a 404) is not retried for that track for `REMOTE_RETRY_SECONDS` = 3600 seconds; up to 4096 such failures are remembered in memory.

**What is kept.**

- The front image URL per release (or release group), in memory for the life of the process, and on each track as `cover_art_url`.
- Release image lists in `external_cache` for `ART_TTL` = 30 days.
- Downloaded art as files in the `art` folder next to the database, one per track, and a 500-pixel thumbnail. Want list covers, and covers chosen in the art picker, go to the custom art store (`custom_art`), which records where each image came from in `source`: `upload`, `local` (a picture from the files or the album folder), `coverartarchive` or `wikimedia`. Art whose origin was not recorded has no source (NULL). A want list cover with no source is remembered for `MISS_TTL` = 7 days before it is looked for again.

**Key.** None.

**Terms.** [Cover Art Archive API](https://musicbrainz.org/doc/Cover_Art_Archive/API). The page states that no rate limiting rules are in place, and asks that requests to fetch cover art go through the coverartarchive.org service.

Source: `metadata_providers/coverart.py:17-22`, `:25-51`, `:54-83`, `:92-115`; `art.py:36-40`, `:47`, `:86-112`, `:186-218`; `art_candidates.py:34`, `:53-76`, `:149-155`, `:220-237`; `custom_art.py:143-186`; `models.py:974-996`; `routers/art.py:96`, `:233-238`; `want_metadata.py:46`, `:64-84`, `:117-157`; `enrichment.py:185-189`, `:234-261`; `library_tasks.py:482-505`.

## ListenBrainz

**What is sent.**

- Reads (no key, no account), to `https://labs.api.listenbrainz.org` and `https://api.listenbrainz.org`:
  - GET `/similar-artists/json` with one artist MBID, and GET `/similar-recordings/json` with one recording MBID, both with the similarity algorithm named in `SIMILAR_ARTISTS_ALGORITHM` (labs API).
  - GET `/1/popularity/top-recordings-for-artist/<artist MBID>` for an artist's most listened songs.
  - POST `/1/popularity/artist`, `/1/popularity/recording` and `/1/popularity/release-group` with MBIDs, for listen and listener counts.
  - GET `/1/metadata/artist/?artist_mbids=...&inc=tag` for artists' MusicBrainz community tags, up to `MBID_BATCH` = 50 artists per request.
- Token check (GET, once, when a user links ListenBrainz): `/1/validate-token` with the user's token in `Authorization: Token <token>`. It answers with the ListenBrainz user name.
- Scrobbles (POST): `/1/submit-listens` with `listen_type` `single`, the token, artist, title, start time (`listened_at`), and album, duration, recording MBID and artist MBIDs when known. "Playing now" is not sent.

Everything is asked by MusicBrainz ID; no name is sent on a read. Only well-formed MBIDs are sent: file tags are stored as written, so a value holding several IDs (`id1/id2`, or separated by `;` or `,`) is split, and anything that is not a well-formed MBID is skipped (`valid_mbids`, `first_mbid`). Where one ID is needed, the first well-formed one is used; a scrobble sends every well-formed artist MBID. Similar artists for mixes are asked only by an artist MBID already stored (from file tags, the MusicBrainz tasks or an info dialog): a mix never makes a MusicBrainz name search for this. The artist top tracks and the info dialog may find a missing MBID with a MusicBrainz name search first (see MusicBrainz).

**When.**

- Similar songs: after each recorded play, in the background, to fill the cache that autoplay reads (skipped for a user who turned off the switch below, and for a track with no recording MBID). Autoplay itself never calls out. Discovery, Song Mix and artist/album mixes may also fetch them.
- Similar artists: Discovery and Explore refreshes (a page click or the daily refresh). Song Mix, autoplay and artist/album mixes read only what is cached.
- Live look-ups for one mix or refresh stop after `ONLINE_FETCH_BUDGET_SECONDS` = 6 seconds, so a slow ListenBrainz cannot hold a mix for minutes. Seeds not reached by then use what is cached; for similar songs they are also fetched in the background for next time.
- Page views: artist top tracks; listener and play counts in the artist, album and song info dialogs; similar artists in the artist info dialog, asked by the artist's MBID for any artist that has one. Each info dialog links to the ListenBrainz page of the artist, album or song (`listenbrainz_url`). The info dialogs' tags come from MusicBrainz only.
- Admin "ListenBrainz tags" task (modes `listenbrainz_missing` and `listenbrainz_all`), also part of "Full setup" (no key needed). It reads the artist tags for tracks whose artist has an MBID.
- Admin "Artist info" task: listener counts, similar artists and top tracks for each artist (and the Wikimedia Commons photo, see below).
- Scrobble: after a play is recorded, in the background, for users who linked ListenBrainz. Tracks of `MIN_SCROBBLE_SECONDS` = 30 seconds or less are skipped.
- Scheduled: each user's daily Discovery and Explore refresh, at the hour they chose (default on, 06:00 server time), and the scrobble retry on every 600-second tick.

**How often.**

- All ListenBrainz calls (reads, token checks and scrobbles) go through one connection pool and one throttle: at most one request every 0.4 s (`Throttle(0.4)`), which stays under the advertised window of about 30 requests per 10 seconds. So no single user can use up the server's allowance for everyone. A 429 pauses all of them for `X-RateLimit-Reset-In` (or `Retry-After`), or 10 seconds without one; a 503 on a read does the same. When a read's answer says `X-RateLimit-Remaining: 0`, the next call waits out the rest of the window. When the throttle's wait is too long, a token check answers "couldn't be reached" and a scrobble is queued. Timeouts: 10 s for reads and scrobbles, 15 s for the token check.
- A 404 is a miss ("ListenBrainz has nothing"), and so is a 400 on a look-up of one ID. A 400 on a batch request (artist tags, popularity) counts as a failure instead, since one bad ID would otherwise hide the answers for all the others. A failed read is logged as a warning at most once every `FAILURE_LOG_INTERVAL` = 300 seconds.
- A scrobble that fails for a passing reason (network error, 429, 5xx) is queued. It is retried after 2 minutes, then 4, 8 and so on, at most 6 hours apart (`_RETRY_BASE`, `_RETRY_MAX`). Queued scrobbles older than `SCROBBLE_MAX_AGE` = 14 days are dropped. After a successful scrobble, up to 5 of that user's queued scrobbles are sent too. A queued scrobble keeps the track's recording and artist MBIDs, so a retry is as precise as a direct one. A rejected scrobble (any other 4xx) is dropped, not retried.
- A 401 or 403 on a scrobble means ListenBrainz no longer accepts the token (revoked or reset). The link is marked "needs reconnecting" (the flag `revoked` in the stored credential), and that listen is queued. Nothing more is sent with that token: the user's queued scrobbles wait, and later plays are not scrobbled until the user links ListenBrainz again. Linking again stores a fresh credential without the flag, and the queued scrobbles then go out on the next retry, as long as they are under 14 days old.

**What is kept.** ListenBrainz's data is CC0, so it may be stored.

- `external_cache` (provider `listenbrainz`): similar recordings, and the info dialog's similar artists (kind `similar-artists`, keyed by artist MBID), `SIMILAR_TTL` = 30 days; top recordings `TOP_TRACKS_TTL` = 14 days; artist, recording and release group popularity `INFO_TTL` = 30 days.
- Similar artists for Discovery, Explore and mixes in the `artist_metadata_cache` table, keyed by artist name, with the MBID they were asked by and each artist's match (0 to 1): 30 days (`ARTIST_CACHE_TTL`), an empty answer 6 hours (`ARTIST_MISS_TTL`). This table is not pruned by maintenance.
- On each track: the artist's tags (tags with fewer than `MIN_TAG_COUNT` = 2 votes are dropped, at most 25 kept), stored with source `listenbrainz` beside the recording's own MusicBrainz tags, which win; and `online_tags_fetched_at`. A tag naming a genre the library already has is stored as a genre.
- Per user, encrypted: the user token (with the `revoked` flag once ListenBrainz refuses it) and the ListenBrainz user name. The queue of unsent scrobbles (`scrobble_queue`, with `recording_mbid` and `artist_mbids`).

**Key.** None for reads. Scrobbling needs each user's own user token, pasted in Settings (copied from listenbrainz.org/settings; `POST /api/integrations/listenbrainz`, unlinked with `DELETE`). The token must be a UUID (spaces around it are trimmed); anything else is refused before ListenBrainz is asked. A token ListenBrainz does not accept (401 or 403) is refused.

**Terms.** [API documentation](https://listenbrainz.readthedocs.io/en/latest/users/api/index.html) and [rate limiting](https://listenbrainz.readthedocs.io/en/latest/users/api/index.html#rate-limiting). The provider sets a per-client request window and reports it in `X-RateLimit-*` headers, asks clients to honour them, and publishes its data under CC0.

Source: `metadata_providers/listenbrainz.py:32-50`, `:53-87`, `:90-144`, `:170-195`, `:196-225`, `:226-255`, `:256-280`, `:281-317`, `:320-342`, `:345-398`; `listenbrainz_data.py:33-48`, `:54-84`, `:99-148`, `:151-174`, `:180-198`, `:202-247`, `:250-293`; `scrobbling.py:44-83`, `:125-131`, `:134-157`, `:159-237`, `:240-301`; `schemas.py:663-666`; `routers/integrations.py:21-36`; `routers/stream.py:604-610`; `metadata_info.py:258-330`, `:362-372`, `:425-432`, `:552-560`; `discovery/similar.py:50-65`, `:140-237`; `enrichment.py:98-129`; `library_tasks.py:416-479`, `:540-580`, `:631-633`, `:740-741`; `routers/admin.py:494-495`; `routers/metadata.py:67-80`; `models.py:77-80`, `:309-311`, `:493-495`, `:694-717`, `:929-946`; `discovery/scheduler.py:21-60`; `main.py:272`, `:383-388`.

## Deezer

**What is sent.** GET requests to `https://api.deezer.com/search`: one plain search per artist name (10 results; Deezer's `artist:"<name>"` syntax does not match reliably), for a track with a 30-second preview. Only a result whose artist has exactly that name counts (compared after `normalize_artist_name`, which ignores case, accents and extra spaces); when there is none, the artist has no preview, rather than a fuzzy match that would preview and link to someone else.

**When.** Explore only. An Explore refresh (a page click, or the user's daily refresh) searches for candidate artists. Loading the Explore rail (`GET /api/library/discovery/state`) never calls Deezer itself: it shows the stored artists whose previews are already in the process's memory (`deezer.peek_preview`). Artists missing from memory (after a restart, or once their answer has expired) are looked up by one background job on the provider queue (`warm_previews`), and the answer says `explore_pending: true` meanwhile, so the app asks again. The candidates come from ListenBrainz's similar artists, so for a user who turned ListenBrainz off Explore finds no artists and makes no Deezer calls. Deezer is not used for the art picker or the want list.

**How often.** No spacing of its own (`Throttle(0.0)`). A 429 or 503 pauses calls for the `Retry-After` time, or 5 seconds without one; Deezer's quota error (code 4) also pauses for 5 seconds. Timeout 5 s. One Explore refresh checks at most `MAX_EXPLORE_CANDIDATES` = 60 artists, waits `EXPLORE_REQUEST_DELAY_SECONDS` = 0.2 s between calls, stops after `EXPLORE_TIME_BUDGET_SECONDS` = 30 s or `EXPLORE_FAILURE_LIMIT` = 3 failures in a row, and keeps up to `MAX_EXPLORE_RESULTS` = 13 results. The background preview job has the same 30-second budget and 3-failure limit. A failed look-up is remembered for `FAILURE_TTL_SECONDS` = 60 seconds, and that artist is not asked again until then, so a Deezer outage costs at most one request per artist per minute. The code notes Deezer's quota as 50 requests per 5 seconds.

**What is kept.** Nothing Deezer answers is stored. Explore stores only the artist names, as ListenBrainz gives them (Deezer's spelling is not kept). The preview, artwork and link for each artist are looked up live and kept in the process's memory for `MEMORY_TTL_SECONDS` = 6 hours (at most `MEMORY_MAX_ENTRIES` = 2000 artists; "no preview" is remembered too, a failure for 60 seconds). Deezer images are never downloaded: the browser shows Explore's covers and plays its previews straight from `*.dzcdn.net`.

**Key.** None.

**Terms.** [Terms of use](https://developers.deezer.com/termsofuse) (updated 2026-10-27). Section I counts metadata as Content; section III.4 forbids extracting Content into a separate database or making any local copy of it. That is why nothing is stored. The page also states that Deezer may monitor, restrict or remove API access at its discretion; it gives no rate limit. The code links each preview back to its Deezer page.

Source: `metadata_providers/deezer.py:19-30`, `:33-63`, `:66-117`, `:119-180`; `discovery/explore.py:1-6`, `:38-55`, `:73`, `:147-189`, `:192-249`; `discovery/state.py:22-37`; `discovery/similar.py:160-163`; `schemas.py:262-266`; `artist_credits.py:73-78`; `discovery/scheduler.py:53-54`; `art.py:38-40`; `security_headers.py:36-57`.

## lrclib

**What is sent.** GET `https://lrclib.net/api/get` with artist, title, album and duration (when album and duration are known), then GET `https://lrclib.net/api/search` with artist and title.

**When.** When a user opens the lyrics of a track that has no stored lyrics and no lyrics tag in the file. Tracks in ignored folders are never sent.

**How often.** At most one request every 0.3 s (`Throttle(0.3)`). A 429 or 503 on the search pauses calls for the `Retry-After` time, if one is given. Timeout 10 s per request.

**What is kept.** Lyrics found (plain and synced) are stored on the track and never fetched again. When lrclib has nothing, `lyrics_fetched_at` is set and the track is not asked about again for `LYRICS_MISS_TTL` = 6 hours. A failed request is not remembered as a miss.

**Key.** None. (lrclib offers optional registration; the app does not use it.)

**Terms.** [API documentation](https://lrclib.net/docs.md). The provider asks clients to send one request at a time with 200 to 500 ms between them, to honour `Retry-After`, and to identify themselves in the User-Agent with name, version and a homepage or email.

The lrclib throttle interval is 0.3 s, inside the range the provider asks for; each lyrics view makes one or two requests.

Source: `metadata_providers/lyrics.py:14-16`, `:91-131`; `enrichment.py:24`, `:265-313`; `routers/library/folders.py:314-332`; `models.py:294-296`.

## Wikipedia, Wikidata and Wikimedia Commons

**What is sent.** The links come from the artist's MusicBrainz relations.

- Bios: GET `https://www.wikidata.org/w/api.php` (`wbgetentities` with a Wikidata ID, for the article title) and GET `https://<lang>.wikipedia.org/api/rest_v1/page/summary/<title>`.
- Artist photos: GET `https://www.wikidata.org/w/api.php` (`wbgetclaims`, property P18, the entity's image), then GET `https://commons.wikimedia.org/w/api.php` (`prop=imageinfo`) for that file's 1000-pixel URL, author and licence. The image itself is downloaded from `upload.wikimedia.org` when it is chosen in the art picker, used for a want list artist, or saved by the admin "Artist info" task.

**When.** Bios: artist info (page view, want list backfill or the admin "Artist info" task). When there is no Wikipedia article, the bio is MusicBrainz's annotation for the artist, if any. Photos: the art picker for an artist, want list artist pictures (page views and the scheduled backfill), and the admin "Artist info" task (also part of "Full setup"), which saves the photo as the picture of each artist that has no art of its own. It does this once per artist: art someone picked or uploaded is never replaced, and a photo removed with "Reset art" is not put back (remembered in the cache as `artistphoto`). The artist info dialog never fetches a photo; it shows the credit stored with the picture actually shown (see below).

**How often.** At most 5 per second (`Throttle(0.2)`), one throttle for Wikipedia, Wikidata and Commons. A 429 or 503 pauses calls for the `Retry-After` time, if one is given. Timeout 10 s.

**What is kept.**

- The bio summary in `external_cache` (provider `wikipedia`) for `MB_TTL` = 30 days.
- The photo's URL, author, licence, licence URL and file page in `external_cache` (provider `wikimedia`) for 30 days. Only files under a free licence are used: CC BY and CC BY-SA (any version), CC0, public domain and GFDL. Non-commercial (NC) and no-derivatives (ND) variants, fair use and anything unclear count as no photo. The licence URL and file page are kept only when they are `https://` links. When the MusicBrainz look-up behind the photo fails, nothing is cached, so it is tried again later instead of being remembered as "no photo".
- A chosen photo goes to the custom art store (`custom_art`) with `source` `wikimedia` and its credit (author, licence, licence URL, file page) in `credit_json`; so does a want list artist picture. The art picker accepts a Commons image only when it is the very image the artist's Wikidata entry names, so an arbitrary Commons URL is refused.
- The credit is shown with the photo: in the art picker's label, from `GET /api/art/{type}/{key}/credit` and `GET /api/catalog/art/{kind}/{mbid}/credit` (null when the art has none), and as `image_credit` in the artist info dialog. The dialog takes it only from the art actually shown: the library artist's own art, else the wanted artist's catalogue art.

**Key.** None.

**Terms.** [User-Agent policy](https://foundation.wikimedia.org/wiki/Policy:User-Agent_policy), [API etiquette](https://www.mediawiki.org/wiki/API:Etiquette) and [Reusing content outside Wikimedia](https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia). The provider asks for a descriptive User-Agent with contact information and for read requests to be made in series rather than in parallel; Commons files may be reused under their licence, with the author and licence credited.

Source: `metadata_providers/wikipedia.py:1-4`, `:25`, `:28-51`, `:54-68`, `:71-82`, `:85-112`, `:115-134`, `:136-173`, `:176-188`, `:191-202`; `metadata_info.py:258-330`, `:333-356`; `art_candidates.py:158-168`, `:220-242`; `want_metadata.py:74-84`, `:117-157`; `routers/art.py:176-188`, `:233-238`; `routers/want_list.py:262-272`; `models.py:974-996`; `art.py:38-40`; `security_headers.py:36-57`.

## Internet radio

**What is sent.** When a user plays a station, the server opens the stream itself and relays it to the browser. It resolves the station's host once, refuses private or reserved addresses (unless `ATRIOLYRIC_ALLOW_PRIVATE_RADIO_HOSTS` is set), connects to the resolved address only, and sends a plain `GET` with `Host`, the User-Agent and `Icy-MetaData: 1`. `.pls` and `.m3u` playlists are followed to the real stream. The station sees the server's IP address and User-Agent, not the listener's.

**When.** Only while someone is listening. Nothing connects to a station in the background.

**How often.** One upstream connection per listener. At most `MAX_STREAMS_PER_USER` = 2 per user and `MAX_STREAMS_TOTAL` = 8 in total. Connect timeout 10 s in total, 4 s per address; read timeout 10 s; at most 3 redirects; playlists at most 64 KiB.

**What is kept.** The current title from the ICY metadata, in memory, while someone listens. Nothing is written to disk.

**Default stations.** A new install is seeded with 21 stations: 7 Radio Paradise, 4 WFMU, 3 RTÉ, 3 Radio Swiss, 1 KEXP, 1 FIP, 1 Newstalk and 1 Today FM. SomaFM stations are not bundled, because SomaFM's terms ask for prior permission to re-stream its channels. A user may add any station by URL and is responsible for that station's terms. Every default uses HTTPS: Radio Paradise's Main Mix is `https://stream.radioparadise.com/mp3-192` (the same path on Radio Paradise's own host; its published link list still says `http://`), and RTÉ Radio 1 and RTÉ 2FM use the live links RTÉ publishes, `https://www.rte.ie/manifests/radio1.m3u` and `https://www.rte.ie/manifests/2fm.m3u` (each redirects to the station's HTTPS stream on StreamTheWorld; the relay follows up to three redirects). Each default is added once; a default the admin deletes is not added back.

**Key.** None.

**Terms.** SomaFM: [Terms of Service](https://somafm.com/contact/tos.html) and [link to us](https://somafm.com/linktous/). The provider asks that its streams not be re-streamed or embedded without prior permission, and says it is not approving new third-party clients. Atriolyric does not bundle SomaFM stations, but relays each stream through the server, so admins who keep or add SomaFM stations should read these terms. The other default stations' terms are not summarised here: see each station's own site.

Source: `radio.py:22-33`, `:49-50`, `:52-58`, `:114-155`, `:158-187`, `:190-200`, `:353-420`; `config.py:61`; `default_radio_stations.py:11-12`, `:72`, `:78`; `main.py:228-265`, `:429`.

## Turning things off

There is no single switch for outside services. What the code allows:

- **ListenBrainz per user.** "Use ListenBrainz to find similar music for what I play" (Settings, privacy; API field `online_recommendations`, column `users.online_recommendations`) off: no ListenBrainz look-up is made for that user. No similar-song lookup follows their plays, and their mixes, Discovery and Explore neither ask ListenBrainz nor use its similar artists or similar songs, even what is already cached. Explore then has no candidate artists, so no Deezer calls are made for them either. Info-dialog listener and play counts, the artist info dialog's similar artists and artist top tracks show only what is already cached. ListenBrainz artist tags that the admin task stored on tracks are library data and still count for everyone. On by default. Their scrobbling, if linked, is not affected.
- **Scrobbling.** Happens only for users who link ListenBrainz with their token. Unlinking stops it, and queued scrobbles for that user are dropped.
- **Daily Discovery and Explore.** Each user can turn off "Auto-refresh daily" in Settings. That stops the scheduled ListenBrainz and Deezer calls for them.
- **Bulk metadata.** MusicBrainz, Cover Art Archive, ListenBrainz tags and artist info are fetched in bulk only by tasks an admin starts from the admin page. Scans read local tags only.
- **Ignored folders.** Tracks in ignored folders are skipped by the metadata tasks and are never sent to lrclib.
- **Radio.** Delete the default stations you do not want; they are not re-added.

MusicBrainz, Cover Art Archive, Deezer, lrclib, Wikipedia and Wikimedia Commons have no setting of their own: opening info views, the art picker, the want list, Explore or lyrics fetches from them on demand.

Source: `listenbrainz_data.py:45-48`, `:128-129`, `:217-233`; `discovery/similar.py:160-163`; `metadata_info.py:269-273`, `:362-370`; `routers/stream.py:604-610`; `routers/auth.py:240-252`; `scrobbling.py:85-91`, `:159-169`, `:255-258`, `:272-275`; `models.py:77-80`; `library_tasks.py:724-744`; `routers/admin.py:480-497`; `folder_exclusions.py:7`, `:51`; `main.py:228-265`.
