# How mixes are made

This page describes how Atriolyric builds mixes, Discovery, Song Mix and autoplay, what it learns from your listening, and what it stores for that. Every number below comes from the code (`server/app/discovery/`, `server/app/listening.py`, `server/app/audio_*.py`, `server/app/playback.py`). The settings are in the README's configuration table.

Everything runs on the server, from your own library and your own listening. No account or key is needed (see "ListenBrainz is optional"). A fresh install, with no plays and no listens, draws as it did before this learning existed.

## The kinds of mix

| Kind | Size | Made from | When it is replaced |
|------|------|-----------|---------------------|
| Cluster mixes ("Radiohead, Portishead & more Mix") | 40 tracks, up to 5 | Artists you play together | Daily refresh |
| Label mixes ("Shoegaze Mix") | 40 tracks, the rest of the 13 slots | One genre or tag, with a few related labels if it is short | Daily refresh |
| Discovery | 25 tracks | Library artists like your 5 most played artists of the last 30 days (all-time if none), favouring tracks you have not played much | Daily refresh, or by hand |
| Specify mix | 40 tracks | The genres and tags you pick, with excludes | When you make it again with the same labels; any refresh of the mixes drops it |
| Song Mix | 25 tracks | One song | Each new Song Mix or artist/album mix |
| Artist or album mix | 25 tracks | An artist or an album | Each new Song Mix or artist/album mix |
| Autoplay | up to 24 tracks per batch | The song that just ended and the last few played | Never stored; a new batch is made each time the queue runs out |

The daily refresh runs once a day per user, at the hour set in Settings (default 06:00, server time; "Auto-refresh daily" is on by default). It rebuilds Discovery, Explore and all the mixes. A user is due once their hour has passed and today's run has not happened. Refreshing the mixes by hand runs the same mix job.

A mix is replaced when it refreshes: a label or cluster mix that is not picked again is deleted, and the others get new tracks. Discovery, Song Mix and artist/album mixes each have one managed slot that the next one overwrites. To keep one, use "Save as playlist". That copies its tracks (minus anything you have disliked since) into an ordinary playlist that nothing replaces. "Add more" on a mix appends 25 tracks built the same way, with nothing already in it and no other version of those songs.

Song Mix and artist/album mixes share one slot, called "Generated". A Song Mix needs at least 2 tracks, otherwise the app reports that there are not enough matches and keeps the old one.

## Shared rules

Every automatic pick (all the kinds above, and autoplay) follows these rules.

**Which tracks are eligible.**

- Tracks, not videos, in a library you can access and not in an ignored folder.
- Not disliked. A dislike applies to a song, an album or an artist; the most specific like or dislike wins, so a liked song survives a disliked artist.
- Length from `ATRIOLYRIC_MIX_MIN_TRACK_SECONDS` (45) to `ATRIOLYRIC_MIX_MAX_TRACK_SECONDS` (1200). A track of unknown length is kept.
- Not on an album MusicBrainz types as Spokenword, Interview, Audiobook or Audio drama. Live albums are allowed unless `ATRIOLYRIC_MIX_ALLOW_LIVE_ALBUMS` is `false`. A track with no album, or an album MusicBrainz has not typed, is eligible.
- What you pick yourself is never filtered: a Song Mix's seed song, an album, an artist, a playlist.

**One version per song.** Two tracks are one recording when they share a MusicBrainz recording id, or the same main artist and the same normalised title. The normalised title drops "(2011 Remaster)", "(feat. X)", "(Explicit)", "(Clean)", "(Deluxe)", "(Bonus Track)", "- Single Version", "- Album Version", "(Mono)" and "(Stereo)", and ignores case, accents and punctuation. Live, acoustic, demo, instrumental and remix versions stay separate songs, also when the same note mentions a remaster: "(Live at Wembley; 2011 Remaster)" and "(Clean Bandit Remix)" are not the studio song. A title that is only punctuation ("+", "÷") is matched as itself. Only one version gets in. The one kept is, in order: the one you liked (as a song, then through its album, then through its artist); the one you have played most; the one on a studio album rather than an EP, single, untyped release, compilation or live release.

**Artist first, with a cap per artist.** A track's artist is its first main credit; "A feat. B" counts as A, and "A & B" counts as A when A also appears alone in the pool. The picker goes round the artists in random order, and each round every artist earns one pick and spends it on one of its songs, so an artist with a huge catalogue gets no more turns than one with a single song. Each artist is capped while others remain:

| Mix | Tracks per artist |
|-----|-------------------|
| Cluster, label and Specify mixes (40 tracks) | 3 |
| Discovery, Song Mix, artist/album mix, autoplay batch (25 tracks) | 2 |
| A cluster mix's "new to you" share | 1 |

The cap is soft. It is lifted only when too few artists are left to fill the mix.

**Ordering and spacing.** The picks are ordered one track at a time. The first track is random (a Song Mix starts with its seed). Each next track is the one sharing the most genres and tags with the previous one. A track by an artist heard in the last six places loses 6, 4.5, 3, 2, 1 or 0.5 shared tags, depending on how recently; a track from an album heard in the last two places loses 4 or 2. A repair pass then moves any track that still sits within three places of the same artist, or two of the same album, to the nearest slot that helps. A run of one artist survives only when there is nothing else to put between. When most tracks have been analysed, sound also steers the order (see "Audio analysis").

**Likes and dislikes.** A liked song, album or artist gets about 1.5 times an ordinary artist's share of turns, and its tracks go first within the artist's turn. Likes only reorder tracks that are already candidates; they never add any. A liked artist counts as a whole; a liked song or album counts until its tracks are used. Discovery counts only an artist liked as a whole, because it is for music you have not heard much. Dislikes are hard excludes.

## What the app learns from listening

Two kinds of data feed the draw: counted plays (`play_history`) and how each listen ended (`listen_events`).

### Plays: familiarity and fatigue

Each play of a track counts by its age. The memory strength of a track is the sum over its plays of `age ^ -0.5`, with age in days. Plays are grouped by whole day and count at the middle of that day, so a play a minute ago counts as half a day old (strength 1.41). Only the 50 most recent plays of a track, and 250 of an artist, are summed. From the strength:

- Familiarity, 0 to 1: `strength / (strength + 2)` for a track and `strength / (strength + 4)` for an artist (all its tracks together). Never played is 0.
- Fatigue, 0 to 1: the same sum over the last 7 days only, then `s^2 / (s^2 + 5^2)` for a track and `s^2 / (s^2 + 12^2)` for an artist.

Some values: one play today gives a track familiarity 0.41 and fatigue 0.07; two plays today give fatigue 0.24; five plays over the last three days (two today, two yesterday, one the day before) give 0.51, enough to leave the track out of Discovery; ten over three days (four, three, three) give 0.80. One play yesterday gives familiarity 0.29 and fatigue 0.03; one play a year ago gives familiarity 0.03. For an artist, whose fatigue counts the plays of all its tracks, an afternoon of fifteen plays today gives 0.76, which lowers its tracks' mix weight by about a quarter (never more than 30%); three days later the same plays give 0.31, and after a week nothing. A track counts as "familiar" in a cluster mix and in the build log at familiarity 0.15 or more, which is about one play in the last week, or three plays in the last two months.

### How each play ended: preference

The device producing the sound reports how every track or music video it started ended (`POST /api/stream/{id}/listened`, see [playback-protocol.md](playback-protocol.md)). The outcomes:

| Outcome | Meaning |
|---------|---------|
| `finished` | Played to its end |
| `skipped` | You moved on while it was playing (Next, Previous, another item, removed from the queue) |
| `stopped` | Left while paused, or playback stopped |
| `transferred`, `error`, `unknown` | Another device took over; it could not be played; the page went away. These never say anything about taste |

A listen with less than 2 seconds heard is not reported (that is scanning past items). A skip after 90% or more of the track counts as finished. A skip before 30 seconds is an early skip (weight 1); a later skip is a late skip (weight 0.4); a skip of a track you picked yourself counts half as much. A finish has weight 1. `stopped` is counted but adds no evidence. Each listen's weight halves every 90 days.

The preference is `(positive + 2) / (positive + negative + 4)`. With no listens it is 0.5 (neutral). One early skip gives 0.4 and one finish 0.6, so no single listen bans or crowns a track. A track's preference comes from its own listens; an artist's from the listens of all tracks it is a main or featured artist of.

### The draw weight

Everything multiplies into one weight per track. The sampler draws heavier tracks sooner and gives their artist more turns.

```
weight = base x preference x familiarity term x fatigue term
```

kept within 0.05 and 3.0. A liked track's weight is multiplied by 1.5 in the sampler, and the product is capped at 3.0, so no track or artist can take over a mix.

- `base` is 1, or `1 + 4 x match` for a song ListenBrainz calls similar (match 0 to 1).
- `preference` is the track's `0.25 + 1.5 p` (0.25 to 1.75) times its artist's `0.6 + 0.8 p` (0.6 to 1.4), the product kept within 0.25 and 1.75. An artist you keep skipping lowers all its tracks, but less than skipping the track itself.
- In mixes (cluster, label, Specify, Song Mix, artist/album mix) the familiarity term is `1 + 0.5 x familiarity` (up to 1.5, a familiar track is liked a little more) and the fatigue term is `(1 - 0.8 x track fatigue) x (1 - 0.3 x artist fatigue)`.
- In autoplay and in a cluster mix's "new to you" share the familiarity term is 1 and the fatigue term is the same.
- In Discovery the familiarity term is `(1 - familiarity)^2`: an unplayed track counts fully, a half-established one a quarter. There is no fatigue term, but tracks with fatigue 0.5 or more are left out.

With no plays and no listens every term is 1. The summary of your plays and listens is cached per user until a play or listen is added, the day changes or the library changes.

## How similar artists are found

Song Mix, autoplay, artist/album mixes, Discovery and the cluster mixes' "new to you" share all ask the same question: which library artists are like these ones? It is answered from the library's own labels. No outside lookup is needed.

1. **Labels.** Each track carries genres (file tags, MusicBrainz, the older genre column), library tags, and MusicBrainz folksonomy tags: the recording's own, plus its artist's tags from ListenBrainz. A label's weight on a track is 1 for a genre or library tag; for a MusicBrainz tag 0.5 with one vote, 0.75 with two, 1 with three or more (at least 0.25). A ListenBrainz artist tag counts the same way by its votes. A label that two sources both give counts once.
2. **Profiles.** An artist's profile gives each label its rarity times how much of the artist's catalogue carries it. Rarity is `ln(1 + artists / artists carrying the label)`: a label on every artist counts about 0.7, one on a single artist in 10,000 about 9.2. The share is the label's weights summed over the artist's tracks, divided by their number, at most 1. A broadly tagged big catalogue therefore does not win by breadth.
3. **Likeness.** The seed's profile is the sum of its artists' profiles (each normalised, so a big catalogue counts as much as a small one). A Song Mix also adds the song's own labels, weighted by rarity. The likeness of a library artist is the cosine between its profile and the seed's, from 0 to 1.
4. **Shortlist and draw.** The 60 best-scoring artists (ties in random order) form the shortlist. From it 20 artists are drawn without replacement, each with a chance proportional to its score squared. The best match is about nine times as likely to come up first as one scoring a third of it. So the closest artists usually make it, the rest of the shortlist rotates, and a refresh does not always bring the same twenty. The seed's own name variants ("A", "A feat. B") are never recommended.

For a radio (Song Mix "Add more", autoplay) the score is half the likeness to the seed and half the likeness to the centroid of what was heard just before. That context is the artists of the last 4 tracks, and the labels at least half of those tracks share (so one track's side label does not pull the radio away).

### The generic-label blocklist

Folksonomy tags mix genres with listeners' own bookkeeping: "seen live", "favourites", "albums i own", "under 2000 listeners", "awesome". Two artists sharing "favorites" are not alike, and a mix called "seen live" is a random draw from everything. These names are never evidence of similarity and never become an automatic mix. Names are compared after lower-casing and collapsing spaces. A name is blocked when it:

- is in a built-in list (for example `seen live`, `favorites`, `favourites`, `love`, `awesome`, `albums i own`, `spotify`, `wishlist`, `best`, `good`, `cool`, `all`, `music`, `misc`, `unknown`, `various artists`; the full list is `BLOCKED_LABELS` in `server/app/discovery/blocklist.py`);
- is only digits, or contains `favo`, `seen live`, `listeners` or `spotify`;
- contains one of the whole words `seen`, `own`, `owned`, `love`, `loved`, `favs`, `fav`, `wishlist`;
- is in `ATRIOLYRIC_MIX_BLOCKED_LABELS`.

To add your own, set a JSON list and restart: `ATRIOLYRIC_MIX_BLOCKED_LABELS='["my uploads", "car"]'`. Specify mix still offers every label, because what you pick on purpose is yours.

Decade tags ("80s", "1990s", "'70s") are blocked as automatic mix labels only, since an "80s" mix jumps from synth-pop to thrash. They still count as similarity evidence next to genres, and the rarity weighting keeps a decade carried by a quarter of the library from counting much. "Male vocalists" and "female vocalists" stay allowed everywhere.

## Label mixes

A label qualifies with at least 15 accessible tracks you have not disliked and a name that is not blocked. The daily refresh gives clusters up to 5 of the 13 slots and the rest go to labels.

With plays in the last 90 days, a label's chance of being drawn is its share of that listening (each recently played track counts by its strength, so recent and often played tracks count most) times its rarity, halved for an "umbrella" label (one whose narrower label has 80% or more of its tracks inside it, such as "rock" over "shoegaze"). About a quarter of the slots (3 of 13, 2 of 8) go to labels you have hardly listened to (under 5% of your listening). Those are drawn by size, up to four times likelier for a label that overlaps your five most listened labels. Without recent plays, labels are drawn by track count, halved for umbrellas.

Each label mix is the label's own tracks, drawn artist first, 3 per artist. Only if there are fewer than 40 do the 5 labels that most often appear on the same tracks fill the rest.

Specify mix does the same from the labels you pick: tracks carrying all the includes come first, then those missing one, and so on, with none of the excludes. When fewer than the mix's tracks carry every include, the app says so as the mix is made ("Only 5 tracks match everything you chose, so the rest of the mix matches just some of it."); the create response carries that number as `full_match_count`.

## Cluster mixes

Cluster mixes are built from your own listening and the library's labels, with no ListenBrainz.

1. **Sessions.** Your latest 20,000 plays are split into listening sessions wherever 30 minutes pass without a play (using when the play started, when the client reported it). A session is cut after 40 artists, so a shuffle of the whole library does not tie everything to everything.
2. **Artists.** Each play counts for the main credited artist, halving in weight every 90 days. The 200 heaviest artists are the nodes of a graph. Nothing is made with fewer than 30 plays in total.
3. **Edges.** The weight between two artists is 0.6 times how often they are heard in the same sessions (a cosine; a marathon session counts once, a crowded session less than a focused one) plus 0.4 times how alike their label profiles are. Edges under 0.05 are dropped. So two alike artists you rarely played together can still end up together.
4. **Clustering.** Louvain community detection groups the graph. A group of fewer than 3 artists, or under 3% of the listening, is dropped. Two groups whose label centroids are 0.85 alike are merged. At most 5 are kept, heaviest first, and a mix with fewer than 15 tracks is skipped.
5. **The 75/25 split.** Of 40 tracks, 30 are familiar: tracks by the cluster's artists with familiarity 0.15 or more and fatigue under 0.5, 3 per artist. The other 10 are new to you: first tracks by the cluster's own artists that are not yet familiar, 1 per artist; then, if the audio analysis has vectors, tracks that sound like the familiar picks (at most a third of the mix); then tracks by library artists whose labels are like the cluster's, only by artists you have barely played (artist familiarity under 0.2) and outside the cluster. If the new share is still short, the rest is filled with familiar tracks.
6. **Name.** The two most played artists: "A, B & more" (the playlist is called "A, B & more Mix"). When one specific label is carried by at least 60% of the cluster's artists and by at most about 10% of all artists, it leads: "Shoegaze mix: A, B".

"Add more" on a cluster mix rebuilds from the same artists with the same split.

## Seeded radio and autoplay

Song Mix, artist/album mixes and autoplay are grown from a seed. The candidate pool is the tracks by the 20 drawn similar artists, plus songs ListenBrainz calls similar when it has said so, plus the "sounds like" extras (below). Those go through the shared rules.

**Song Mix.** The seed is first, then 24 tracks. The seed's own artist comes back at positions 5, 10, 15 and 20 while it has eligible songs, so a song radio sounds like the song's artist now and then. The per-artist cap applies to the other artists. "Add more" keeps the seed as the anchor and lets the last 4 added tracks steer.

**Artist or album mix.** The seeds are the collection's own tracks, found the way its page lists them. The mix is tracks by artists like the collection's artists and its tracks' labels, plus ListenBrainz's similar recordings of its 5 most played songs when cached or fetchable. The collection's own tracks are left out, and for an artist mix every track by that artist.

**Autoplay.** When the queue runs out and repeat is off, the server makes the next batch. The anchor is the track that just ended. The context is up to 4 earlier tracks of the queue that you actually played, oldest first (a play in the last `ATRIOLYRIC_AUTOPLAY_RECENT_HOURS` hours, or 24 when that is 0); tracks merely queued, or skipped before they counted as a play, do not steer. Autoplay does not bring the seed's artist back on a timer, and it never calls ListenBrainz. It only reads what was cached after you played the song.

Autoplay leaves out every track already in the queue, played or still to come, and every track you played in the last `ATRIOLYRIC_AUTOPLAY_RECENT_HOURS` hours (24; `0` turns the check off), and all other versions of those songs. It tries, and the first step that finds anything wins:

1. Similar tracks, as above (up to 24).
2. Other tracks by the seed's artist (this covers an artist with no tags and no cached similar artists), up to 50.
3. A random draw from the whole library, up to 50.

Only when that leaves nothing at all are the queue and recent plays allowed back, and after that the length and album-type rules are dropped as well. Playback stops only when the library has nothing left to give.

## Audio analysis

The server measures what each track sounds like, locally. Nothing leaves the server, and nothing is downloaded: ffmpeg decodes a short excerpt and numpy does the arithmetic.

**What is measured.** 45 numbers per track, stored as 180 bytes:

- tempo (40 to 240 BPM), tempo strength, and onset strength (rhythm);
- loudness level, its spread and the dynamic range;
- brightness (spectral centroid and roll-off), noisiness (flatness) and zero-crossing rate, with their spreads;
- the key and mode, how clear the key is, and a chroma profile rotated to the key;
- 12 MFCC averages and the spread of the first 4 (timbre).

**The excerpt.** At most 90 seconds per track, however long it is. For a track of 110 seconds or more, that is three 30-second windows centred at 20%, 50% and 80% of the track, decoded by one ffmpeg process. A shorter track, or one of unknown length, is decoded from its start, at most 90 seconds. A track with less than 8 seconds of audible sound is stored as a failure ("too short or silent").

**When it runs.** One background thread starts with the server. By default it waits while the loudness pass is measuring, so only one heavy ffmpeg runs at a time. It analyses the tracks you play first: most recently played, then most played, then the rest. Results are committed every 25 tracks. It carries on after a restart from whatever is still pending, and after it finishes it looks again every 10 minutes for tracks a scan added. A failure is stored too, so a broken file is not retried until the file changes or the analyser's version goes up (then everything is analysed again).

**Watching progress.** Admin → Background work shows how many tracks are analysed, failed and left, and whether the pass is running, waiting for the loudness pass or turned off. The log (INFO) shows where the library stands when a pass with work to do starts, a line every 100 tracks, one at the end, and one at start-up if it is off:

```
Audio analysis: 400 of 12,873 tracks analysed, 23 could not be analysed, 12,450 to go
Audio analysis: 400 / 12450 tracks, 396 analysed
Audio analysis complete: 12427 tracks analysed, 23 failed
Audio analysis is turned off (ATRIOLYRIC_AUDIO_ANALYSIS)
```

"stopped" replaces "complete" when the server shut down first. The loudness pass logs `Loudness: N of M tracks have a value, F could not be measured, P to go` as it starts and `Loudness backfill: N / M tracks checked, K values stored` every 100 tracks (see [operations.md](operations.md)).

**What "sounds like" is used for.** Everything here is optional and covers only analysed tracks:

1. *Candidates.* For Song Mix, autoplay, artist/album mixes and a cluster mix's "new to you" share, the 150 tracks nearest in sound to the seed (the nearest seed counts) become candidates beside the label-similar artists. Only those within distance 2.5 ("related"), at most one third of the mix, one per artist, closer ones likelier. They go through the same eligibility, dislikes, version and weight rules.
2. *Scoring.* A candidate's weight is multiplied by 0.7 at distance 4.0 or more (about the median random pair) up to 1.3 for the same sound.
3. *Ordering.* When 60% or more of a mix's tracks are analysed, the order also prefers the next track that sounds closest, at 1.5 shared labels per unit of distance, inside the artist and album spacing rules. A pair with a missing vector counts as the median distance, so labels place those tracks. This applies to every mix kind, Discovery and label mixes included.

Distance is Euclidean in a weighted space: timbre counts most, then brightness and noisiness, rhythm, loudness and dynamics, and tonality least. The key has no weight (C major and A major songs do not differ in how they sound), and tempo is folded into one octave (70 to 140 BPM) so half-time and double-time estimates still match. Each value is scaled against the library's own median, so the scale adapts to your collection. About 0.1 to 0.6 is the same recording, 1 to 2.5 related, about 4 an average random pair, 5 to 6 very different.

**Limits.** It hears timbre, rhythm, loudness and tonality in at most 90 seconds. It does not know lyrics, language, era or genre, and it can misjudge tempo. Two tracks that sound alike are not necessarily two tracks you like alike, which is why it is only a share of a mix and a nudge in weights and order. Until a track is analysed it is chosen and placed by labels alone. With no vectors at all (a new install, analysis off) nothing changes and no index is loaded. Turning analysis off keeps the stored vectors and still uses them.

## ListenBrainz is optional

Nothing in mixes requires ListenBrainz. The library's own genres, tags, plays and listens do all the work. ListenBrainz needs no key. It adds:

- **Tags.** The artist's MusicBrainz community tags, read through ListenBrainz, join the label profiles (weight from the votes, as above). They come from the admin "ListenBrainz tags" task, for tracks whose artist has a MusicBrainz ID. The recording's own MusicBrainz tags win over them. They are stored on the tracks, so they count for every user.
- **Similar artists.** A library artist that ListenBrainz also names as similar to a seed gets `SIMILAR_ARTIST_BONUS x match` (0.5 x match, 0 to 1) added to its label score. ListenBrainz is asked by the seed artist's MusicBrainz ID only, one already stored; a mix never waits on a MusicBrainz name search. A Discovery refresh may fetch these (cached 30 days; an empty answer 6 hours). Song Mix, autoplay and artist/album mixes only read what is already cached.
- **Similar songs.** A library song that ListenBrainz calls similar to a seed (its similar recordings, asked by the seed's recording MBID) gets base weight `1 + 4 x match`, wherever its artist is. Discovery uses your 5 most played songs of the last 30 days, a Song Mix its seed, an artist/album mix its collection's 5 most played songs. These may fetch from ListenBrainz (once per song, cached 30 days). A song without a recording MBID has none. Autoplay never fetches; after each play the server warms the cache for that song in the background.

Live look-ups for one mix stop after `ONLINE_FETCH_BUDGET_SECONDS` = 6 seconds, so a slow ListenBrainz cannot hold it for minutes. Seeds not reached by then use what is cached; for similar songs they are also fetched in the background, ready for next time.

Similar artists and similar songs are used only for users who have not turned off "Use ListenBrainz to find similar music for what I play" (Settings, privacy). With it off, ListenBrainz is not asked for them, and none of its similar artists or songs shape their mixes, Discovery or Explore, even what is already cached. Explore is built from similar artists, so it stays empty for them.

Daily label mixes and cluster mixes make no ListenBrainz call. What is sent to ListenBrainz is listed in [providers.md](providers.md).

## Tuning

| Setting | Effect |
|---------|--------|
| `ATRIOLYRIC_MIX_MIN_TRACK_SECONDS` (45) | Shortest track an automatic pick may use |
| `ATRIOLYRIC_MIX_MAX_TRACK_SECONDS` (1200) | Longest track an automatic pick may use |
| `ATRIOLYRIC_MIX_ALLOW_LIVE_ALBUMS` (`true`) | Whether Live albums are used |
| `ATRIOLYRIC_AUTOPLAY_RECENT_HOURS` (24) | How long autoplay avoids tracks you played; `0` turns it off |
| `ATRIOLYRIC_MIX_BLOCKED_LABELS` (empty) | Extra genre and tag names that never become a mix or count as similarity |
| `ATRIOLYRIC_AUDIO_ANALYSIS` (`true`) | Whether the background audio analysis runs |
| `ATRIOLYRIC_AUDIO_ANALYSIS_CONCURRENCY` (1) | How many tracks are analysed at once |
| `ATRIOLYRIC_PLAY_HISTORY_RETENTION_DAYS` (0) | Also bounds how long listens are kept |

The formats and defaults are in the README's configuration table. The constants in this page (caps, shares, half-lives) are fixed in the code. Costs and storage of the audio analysis are in [operations.md](operations.md).

## Reading the log

Every mix build writes one INFO line from the `app.discovery` logger:

```
Mix built: cluster 'Radiohead, Portishead & more' from 312 candidates: 40 tracks by 22 artists, 30 familiar / 10 new, weights 0.48-1.62
```

The kind is one of `cluster`, `label`, `specified`, `discovery`, `song`, `autoplay` or `collection` (artist/album mix). Then the name (a label, a selector, a seed title or an artist), the number of candidates it was drawn from, the tracks chosen and their artists, how many were familiar (familiarity 0.15 or more) and how many new, and the lowest and highest draw weight among the chosen. The daily refresh also logs which labels it picked:

```
Label mixes for user 3: from listening ['Shoegaze', 'Post-punk', ...]; to explore ['Ambient', ...]
```

Autoplay writes the same `Mix built:` line each time it makes a batch, so a log line shows what it was drawn from.
