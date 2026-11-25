"""Song Mix, collection mixes, autoplay candidates and topping up or saving auto playlists."""

import json
import random
from collections import Counter
from collections.abc import Collection, Sequence

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.access import accessible_root_ids
from app.discovery import audio
from app.discovery.clusters import (
    cluster_add_more,
    is_cluster_selector,
    tags_by_identity,
)
from app.discovery.common import (
    MIX_MAX_PER_ARTIST,
    SHORT_MIX_MAX_PER_ARTIST,
    InsufficientMatchesError,
)
from app.discovery.discovery_playlist import _discovery_tracks
from app.discovery.eligibility import artist_identity, main_credits, mix_eligible
from app.discovery.familiarity import (
    Kind,
    Taste,
    draw_weights,
    log_mix_build,
    user_taste,
)
from app.discovery.labels import parse_mix_selector
from app.discovery.mixes import _store_mix_preview_ids
from app.discovery.pools import _mix_pool
from app.discovery.sampling import (
    _balanced_track_sample,
    _finish_mix,
    _group_key,
    _load_tracks,
    _Pick,
    _picks,
    _picks_for_ids,
    _tiered_track_sample,
    recording_keys_of,
)
from app.discovery.similar import (
    _artists_similar_by_genre,
    _online_allowed,
    _track_labels,
)
from app.discovery.sounds_like import sounds_like
from app.discovery.state import _get_or_create_state
from app.folder_exclusions import is_path_ignored, not_in_ignored_folder
from app.library_query import filter_tracks_query
from app.listenbrainz_data import similar_owned_weights
from app.models import (
    MediaItem,
    MediaKind,
    PlayHistory,
    Playlist,
    PlaylistItem,
    User,
)
from app.preferences import (
    liked_levels,
    not_disliked,
    visible_auto_items,
)

GENERATED_PLAYLIST_NAME_PREFIX = "Song Mix: "
MAX_GENERATED_TRACKS = 25
ADD_MORE_TRACKS_COUNT = 25
# Below this, a "Song Mix" is just the seed track on its own - not a real
# mix, and not worth creating/overwriting the managed playlist slot for.
MIN_GENERATED_TRACKS = 2


# How many of an artist's/album's songs ask ListenBrainz for similar tracks.
COLLECTION_SIMILAR_SEEDS = 5
# How many of an artist's/album's songs (the most played first) are the
# seeds of its "sounds like" candidates (app.discovery.audio).
COLLECTION_AUDIO_SEEDS = 20
# A Song Mix brings its seed's artist back about every this many tracks
# (positions 5, 10, 15, 20 of 25) while that artist has eligible songs
# left: a song radio should sound like the song's artist now and then, not
# only like its neighbours. The per-artist cap applies to the other artists.
SONG_MIX_SEED_ARTIST_EVERY = 5
# How many of the latest tracks steer a radio besides its anchor: autoplay's
# earlier played tracks, a Song Mix's last additions for "Add more".
RADIO_CONTEXT_TRACKS = 4


def similar_tracks_for_seed(
    db: Session,
    user: User,
    seed_media_item_id: int,
    allow_fetch: bool = False,
    max_tracks: int = MAX_GENERATED_TRACKS,
    boost_likes: bool = False,
    *,
    exclude_ids: Collection[int] = (),
    rng: random.Random | None = None,
    context_ids: Sequence[int] = (),
    seed_artist_every: int | None = None,
    kind: Kind = "mix",
) -> list[int] | None:
    """The actual recommendation logic behind generate_playlist_from_track,
    without the playlist-writing side effect - `[seed_id, *similar_ids]`, or
    None if the seed doesn't exist/isn't accessible. Split out so callers
    that just want candidate ids (e.g. the playback queue's end-of-queue
    autoplay continuation) don't overwrite the user's "Song Mix: ..." playlist
    slot every time they run - only the explicit "Generate playlist from
    this song" action should do that. See _similar_tracks_for_seeds for how
    tracks are chosen.

    `allow_fetch` defaults to False here - unlike Discovery's 5 recurring
    top-played artists (which stay warm in ArtistMetadataCache across
    refreshes), this can be triggered on literally any track in the library,
    so a live ListenBrainz round trip (the client enforces a blocking throttle -
    see _throttle.py) for an artist nothing's ever looked up before would
    make "Generate playlist from this song" slow on every first use. The
    library's own labels are already a real, library-scoped match.

    `boost_likes` marks an explicit Song Mix (and its "Add more"): it may ask
    ListenBrainz once for the seed's similar tracks (then cached). Likes count
    for every caller, autoplay included, whatever this flag says.
    `exclude_ids` (and every other version of those songs) never comes
    back - autoplay passes the queue and what was just played.

    `context_ids` are tracks heard just before (autoplay's earlier played
    tracks, a Song Mix's latest additions): their artists and labels are
    blended in as a second profile, the seed keeping ANCHOR_WEIGHT (see
    _artists_similar_by_genre). `seed_artist_every` (a Song Mix) brings the
    seed's own artist back about every that many tracks while it has
    eligible songs left. `kind` picks the draw weights
    (app.discovery.familiarity): "mix" for a Song Mix, "plain" for autoplay."""
    seed = db.get(MediaItem, seed_media_item_id)
    if seed is None or seed.library_root_id not in accessible_root_ids(db, user):
        return None
    if is_path_ignored(db, seed.library_root_id, seed.relative_path):
        return None
    context = [
        item for media_id in context_ids
        if media_id != seed.id and (item := db.get(MediaItem, media_id)) is not None
    ]
    tracks = _similar_tracks_for_seeds(
        db,
        user,
        [seed],
        allow_fetch=allow_fetch,
        fetch_similar_tracks=allow_fetch or boost_likes,
        max_tracks=max_tracks - 1,
        exclude_ids=exclude_ids,
        rng=rng,
        context=context,
        seed_artist_every=seed_artist_every,
        kind=kind,
    )
    return [seed.id, *(item.id for item in tracks if item.id != seed.id)]


def _similar_tracks_for_seeds(
    db: Session,
    user: User,
    seeds: list[MediaItem],
    *,
    allow_fetch: bool,
    fetch_similar_tracks: bool,
    max_tracks: int,
    exclude_ids: Collection[int] = (),
    rng: random.Random | None = None,
    context: Sequence[MediaItem] = (),
    seed_artist_every: int | None = None,
    kind: Kind = "mix",
) -> list[MediaItem]:
    """Up to `max_tracks` tracks for a mix grown from `seeds` (one song for
    Song Mix and autoplay), the last seed first, steered also by `context`
    (see similar_tracks_for_seed).

    Candidates are tracks by the artists most alike the seeds' artists and
    the seeds' own genres and tags (_artists_similar_by_genre - no online
    look-up needed), plus the songs ListenBrainz calls similar to a seed when it has said
    so before (drawn more often by how close a match). They are drawn with
    the shared sampler - artist first, at most SHORT_MIX_MAX_PER_ARTIST per
    artist while others remain, liked songs/albums/artists LIKE_BOOST times
    as often, one version per recording - from the tracks that are not
    disliked, in an ignored folder, outside the mix length limits or on a
    spoken-word album (app.discovery.eligibility), and never a seed, an
    `exclude_ids` track, or another version of one of those. The seeds
    themselves are never filtered: the user picked them."""
    accessible = accessible_root_ids(db, user)
    online = _online_allowed(user)
    seed_artists = sorted({seed.artist for seed in seeds if seed.artist})
    seed_labels: set[str] = set()
    for seed in seeds:
        seed_labels |= _track_labels(seed)
    # The context's centroid: the labels most of its tracks share, so one
    # track's side label (a bridge into another style) does not pull the
    # radio along it.
    label_counts = Counter(label for item in context for label in _track_labels(item))
    context_labels = {label for label, count in label_counts.items() if 2 * count >= len(context)}
    similar_names: set[str] = set()
    if seed_artists or seed_labels:
        similar_names = _artists_similar_by_genre(
            db,
            accessible,
            seed_artists,
            allow_fetch=allow_fetch,
            online=online,
            extra_seed_labels=seed_labels,
            rng=rng,
            context_artists=sorted({item.artist for item in context if item.artist}),
            context_labels=context_labels,
        )
    # Songs ListenBrainz calls similar to these, wherever they are in the
    # library: drawn more often, by how close a match they are. A Song Mix
    # asks ListenBrainz once per seed (then it's cached); autoplay only reads
    # what playing the song already prefetched.
    track_weights = similar_owned_weights(
        db, accessible, seeds, online, allow_fetch=fetch_similar_tracks
    )
    audio_seeds = [seed.id for seed in seeds] + [item.id for item in context]
    has_audio = audio.available(db)
    if not similar_names and not track_weights and not has_audio:
        return []
    by_artist_or_similar = []
    if similar_names:
        by_artist_or_similar.append(MediaItem.artist.in_(sorted(similar_names)))
    if track_weights:
        by_artist_or_similar.append(MediaItem.id.in_(list(track_weights)))
    pool = _picks(
        db,
        select(MediaItem).where(
            MediaItem.library_root_id.in_(accessible),
            MediaItem.kind == MediaKind.track,
            or_(*by_artist_or_similar),
            not_in_ignored_folder(),
            # The seed itself is added separately, so a Song Mix from a
            # disliked song still starts with that song.
            not_disliked(user.id),
            mix_eligible(),
        ),
    ) if by_artist_or_similar else []
    skip = {seed.id for seed in seeds} | set(exclude_ids)
    pool = [item for item in pool if item.id not in skip]
    used = recording_keys_of(_picks_for_ids(db, skip))
    taste = user_taste(db, user)
    own = (
        _seed_artist_picks(db, user, seeds[-1], skip, used, max_tracks // seed_artist_every, taste, rng)
        if seed_artist_every and seeds[-1].artist
        else []
    )
    liked = liked_levels(db, user, [item.id for item in pool])
    weights = draw_weights(taste, pool, kind, track_weights)
    versions: dict[int, list[_Pick]] = {}
    heard = sounds_like(
        db, user, accessible, audio_seeds, pool, weights, skip, taste, kind,
        audio.max_audio_picks(max_tracks), used, versions, liked, rng,
    ) if has_audio else []
    chosen = _balanced_track_sample(
        pool, max_tracks - len(own) - len(heard), liked, weights, SHORT_MIX_MAX_PER_ARTIST,
        rng=rng, used=used, versions=versions,
    )
    log_mix_build(
        "autoplay" if kind == "plain" else "song", seeds[-1].title or "", len(pool), chosen + heard, weights, taste
    )
    ordered = _finish_mix(db, user, chosen + heard, versions, liked, rng, first_id=seeds[-1].id)
    return _interleave(ordered, _load_tracks(db, own), seed_artist_every) if own else ordered


def _seed_artist_picks(
    db: Session,
    user: User,
    seed: MediaItem,
    skip: Collection[int],
    used: set[tuple],
    count: int,
    taste: Taste,
    rng: random.Random | None,
) -> list[_Pick]:
    """Up to `count` other songs by the seed's own artist for a Song Mix
    (eligible, not disliked, not skipped, one version per recording - the
    versions taken are added to `used`), drawn by the user's draw weights."""
    if count <= 0 or not seed.artist:
        return []
    accessible = accessible_root_ids(db, user)
    pool = [
        pick
        for pick in _picks(
            db,
            select(MediaItem).where(
                MediaItem.library_root_id.in_(accessible),
                MediaItem.kind == MediaKind.track,
                MediaItem.artist.in_(_artist_tags(db, accessible, seed.artist)),
                not_in_ignored_folder(),
                not_disliked(user.id),
                mix_eligible(),
            ),
        )
        if pick.id not in skip
    ]
    liked = liked_levels(db, user, [pick.id for pick in pool])
    return _balanced_track_sample(
        pool, count, liked, draw_weights(taste, pool, "mix"), rng=rng, used=used
    )


def _artist_tags(db: Session, accessible: Collection[int], artist: str) -> list[str]:
    """Every library artist tag that is the same artist as `artist` ("A",
    "A feat. B"), from the cached identity map."""
    identity = artist_identity(main_credits(db).get(artist), artist)
    tags = set(tags_by_identity(db, accessible).get(identity, ())) if identity else set()
    return sorted(tags | {artist})


def _interleave(ordered: list[MediaItem], own: list[MediaItem], every: int | None) -> list[MediaItem]:
    """`own` (the seed artist's songs) placed at every `every`-th position of
    `ordered` (whose first track is the seed), the rest kept in order."""
    result = list(ordered)
    step = every or 1
    for n, item in enumerate(own, start=1):
        result.insert(min(n * step, len(result)), item)
    return result


def add_more_tracks_to_auto_playlist(
    db: Session, user: User, playlist: Playlist, rng: random.Random | None = None
) -> int:
    """Append fresh tracks to an auto-managed playlist without replacing its
    current contents. Each kind reuses the criteria that created it: the
    labels for mixes, the persisted Discovery similar-artist set for
    Discovery, the artist or album for an artist/album mix, and the first
    track for a Song Mix. Nothing already in the playlist comes back, nor
    another version of it."""
    existing_ids = {item.media_item_id for item in playlist.items}
    collection = _collection_source(playlist.mix_genre) if playlist.auto_kind == "generated" else None

    candidates: list[MediaItem]
    if playlist.auto_kind == "mix" and is_cluster_selector(playlist.mix_genre):
        candidates = cluster_add_more(db, user, playlist.mix_genre or "", existing_ids, ADD_MORE_TRACKS_COUNT, rng)
    elif playlist.auto_kind == "mix" and playlist.mix_genre:
        includes, excludes = parse_mix_selector(playlist.mix_genre)
        pool, weights = _mix_pool(db, user, includes, excludes, existing_ids)
        liked = liked_levels(db, user, [c.id for c in pool])
        versions: dict[int, list[_Pick]] = {}
        chosen = _tiered_track_sample(
            pool,
            weights,
            ADD_MORE_TRACKS_COUNT,
            liked,
            MIX_MAX_PER_ARTIST,
            rng=rng,
            used=recording_keys_of(_picks_for_ids(db, existing_ids)),
            versions=versions,
            weights=draw_weights(user_taste(db, user), pool, "mix"),
        )
        candidates = _finish_mix(db, user, chosen, versions, liked, rng)
    elif playlist.auto_kind == "discovery":
        state = _get_or_create_state(db, user)
        candidates = _discovery_tracks(
            db,
            user,
            state.discovery_similar_artists_json,
            {},
            ADD_MORE_TRACKS_COUNT,
            exclude_ids=existing_ids,
            rng=rng,
        )
    elif playlist.auto_kind == "generated" and collection is not None:
        candidates = _collection_tracks(
            db, user, collection.get("artist"), collection.get("album"),
            ADD_MORE_TRACKS_COUNT, exclude_ids=existing_ids, rng=rng,
        ) or []
    elif playlist.auto_kind == "generated":
        seed_item = playlist.items[0].media_item if playlist.items else None
        # The seed stays the anchor; the mix's latest tracks steer the rest.
        latest = [item.media_item_id for item in sorted(playlist.items, key=lambda i: i.position)[1:]]
        generated_ids = (
            similar_tracks_for_seed(
                db,
                user,
                seed_item.id,
                max_tracks=ADD_MORE_TRACKS_COUNT + 1,
                boost_likes=True,
                exclude_ids=existing_ids,
                rng=rng,
                context_ids=latest[-RADIO_CONTEXT_TRACKS:],
                seed_artist_every=SONG_MIX_SEED_ARTIST_EVERY,
            )
            if seed_item is not None
            else []
        )
        new_ids = [media_id for media_id in (generated_ids or []) if media_id not in existing_ids]
        candidates = [
            item
            for media_id in new_ids[:ADD_MORE_TRACKS_COUNT]
            if (item := db.get(MediaItem, media_id)) is not None
        ]
    else:
        return 0

    position = max((item.position for item in playlist.items), default=-1) + 1
    for offset, item in enumerate(candidates):
        db.add(PlaylistItem(playlist_id=playlist.id, media_item_id=item.id, position=position + offset))
    db.commit()
    # The session keeps expired-on-commit off, so the loaded `items` list is
    # stale until reloaded; callers serialize the playlist right after this.
    db.refresh(playlist, ["items"])
    _store_mix_preview_ids(db, playlist)
    db.commit()
    return len(candidates)


def generate_playlist_from_track(
    db: Session, user: User, seed_media_item_id: int, rng: random.Random | None = None
) -> dict | None:
    """Build a temporary "Generated" playlist seeded by one track: that
    track first, then owned tracks by artists similar to its own - the
    single-song equivalent of refresh_discovery_playlist. Overwrites the
    same managed slot each time, like Discovery, unless the user saves it
    first (see save_auto_playlist_as_new).

    Returns None if the seed track doesn't exist/isn't accessible. Raises
    InsufficientMatchesError (rather than silently creating a 1-track
    "mix") if fewer than MIN_GENERATED_TRACKS tracks were found - this can
    happen for an artist with no genre tag and nothing in ArtistMetadataCache
    yet, so there's nothing local to match against."""
    track_ids = similar_tracks_for_seed(
        db, user, seed_media_item_id, boost_likes=True, rng=rng,
        seed_artist_every=SONG_MIX_SEED_ARTIST_EVERY,
    )
    if track_ids is None:
        return None
    if len(track_ids) < MIN_GENERATED_TRACKS:
        raise InsufficientMatchesError()
    seed = db.get(MediaItem, seed_media_item_id)
    assert seed is not None  # similar_tracks_for_seed returned ids, so the seed exists
    return _write_generated_playlist(
        db, user, f"{GENERATED_PLAYLIST_NAME_PREFIX}{seed.title}", track_ids
    )


def _write_generated_playlist(
    db: Session, user: User, name: str, track_ids: list[int], source: str | None = None
) -> dict:
    """Fills the user's single managed "Generated" slot, creating it on
    first use and overwriting it afterwards. `source` is what an artist or
    album mix was made from (see _collection_selector), kept in the
    playlist's mix_genre so "Add more" can draw from it again; a Song Mix
    leaves it empty (its first track is its seed)."""
    playlist = db.scalars(
        select(Playlist)
        .where(Playlist.user_id == user.id, Playlist.auto_kind == "generated")
        .limit(1)
    ).first()
    if playlist is None:
        playlist = Playlist(
            user_id=user.id, name=name, is_auto_generated=True, auto_kind="generated",
            mix_genre=source,
        )
        db.add(playlist)
        db.commit()
        db.refresh(playlist)
    else:
        playlist.name = name
        playlist.mix_genre = source
        db.execute(delete(PlaylistItem).where(PlaylistItem.playlist_id == playlist.id))

    for position, media_item_id in enumerate(track_ids):
        db.add(
            PlaylistItem(
                playlist_id=playlist.id, media_item_id=media_item_id, position=position
            )
        )
    db.commit()

    return {"playlist_id": playlist.id, "name": name, "track_count": len(track_ids)}


# An artist/album mix stores what it was made from in its playlist's
# mix_genre (unused by the generated slot otherwise), as this prefix plus
# JSON {"artist": ..., "album": ...}.
_COLLECTION_PREFIX = "collection:"


def _collection_selector(artist: str | None, album: str | None) -> str:
    return _COLLECTION_PREFIX + json.dumps(
        {"artist": artist, "album": album}, ensure_ascii=False, separators=(",", ":")
    )


def _collection_source(selector: str | None) -> dict | None:
    if not selector or not selector.startswith(_COLLECTION_PREFIX):
        return None
    try:
        source = json.loads(selector[len(_COLLECTION_PREFIX):])
    except ValueError:
        return None
    if not isinstance(source, dict) or (source.get("artist") is None and source.get("album") is None):
        return None
    return source


def generate_playlist_from_collection(
    db: Session,
    user: User,
    label: str,
    artist: str | None = None,
    album: str | None = None,
    rng: random.Random | None = None,
) -> dict | None:
    """The artist/album equivalent of generate_playlist_from_track: a
    "Generated" mix of owned tracks by artists similar to the given
    artist (or the album's artists). See _collection_tracks. Returns None if
    the collection has no accessible tracks; raises InsufficientMatchesError
    if too few similar tracks exist."""
    chosen = _collection_tracks(db, user, artist, album, MAX_GENERATED_TRACKS, rng=rng)
    if chosen is None:
        return None
    if len(chosen) < MIN_GENERATED_TRACKS:
        raise InsufficientMatchesError()
    return _write_generated_playlist(
        db, user, f"{'Album' if album is not None else 'Artist'} Mix: {label}",
        [c.id for c in chosen],
        _collection_selector(artist, album),
    )


def _collection_tracks(
    db: Session,
    user: User,
    artist: str | None,
    album: str | None,
    limit: int,
    *,
    exclude_ids: Collection[int] = (),
    rng: random.Random | None = None,
) -> list[MediaItem] | None:
    """Tracks for an artist or album mix, or None when the collection has no
    accessible tracks.

    The collection is found the way its own page lists it
    (app.library_query.filter_tracks_query): an artist's tracks by its
    credits, album-artist tag or name; an album by its owner (so a
    compilation's tracks by many artists are all its seeds); and an album
    with no artist at all means that album, not every album of that title.
    The mix is then grown from those tracks like a Song Mix
    (_similar_tracks_for_seeds' sampler and filters): tracks by artists
    alike the collection's artists and its tracks' labels, plus ListenBrainz's
    similar tracks of its most played songs when cached. The collection's
    own tracks are left out, and for an artist mix every track by that
    artist."""
    accessible = accessible_root_ids(db, user)
    filters = {"artist": artist, "album": album, "artist_is_null": artist is None and album is not None}
    seeds = _picks(db, filter_tracks_query(db, user, filters).where(not_in_ignored_folder()))
    if not seeds:
        return None

    seed_artists = sorted({s.artist for s in seeds if s.artist})
    labels: set[str] = set()
    for seed in _load_tracks(db, seeds[:50]):
        labels |= _track_labels(seed)
    online = _online_allowed(user)
    similar_names = _artists_similar_by_genre(
        db,
        accessible,
        seed_artists,
        allow_fetch=False,
        online=online,
        extra_seed_labels=labels,
        rng=rng,
    )
    # ListenBrainz similar tracks of a few of the collection's songs (the
    # most played, else the first): read from the cache, or one request each.
    seed_ids = {s.id for s in seeds}
    track_weights = {
        media_id: weight
        for media_id, weight in similar_owned_weights(
            db, accessible, _load_tracks(db, _most_played(db, user, seeds, COLLECTION_SIMILAR_SEEDS)),
            online,
        ).items()
        if media_id not in seed_ids
    }
    has_audio = audio.available(db)
    if not similar_names and not track_weights and not has_audio:
        return []
    by_artist_or_similar = []
    if similar_names:
        by_artist_or_similar.append(MediaItem.artist.in_(sorted(similar_names)))
    if track_weights:
        by_artist_or_similar.append(MediaItem.id.in_(list(track_weights)))
    pool = _picks(
        db,
        select(MediaItem).where(
            MediaItem.library_root_id.in_(accessible),
            MediaItem.kind == MediaKind.track,
            or_(*by_artist_or_similar),
            not_in_ignored_folder(),
            not_disliked(user.id),
            mix_eligible(),
        )
    ) if by_artist_or_similar else []
    skip = seed_ids | set(exclude_ids)
    own = artist_identity(None, artist) if artist is not None and album is None else None
    pool = [c for c in pool if c.id not in skip and (own is None or _group_key(c) != own)]
    used = recording_keys_of(_picks_for_ids(db, exclude_ids)) if exclude_ids else set()
    liked = liked_levels(db, user, [c.id for c in pool])
    taste = user_taste(db, user)
    weights = draw_weights(taste, pool, "mix", track_weights)
    versions: dict[int, list[_Pick]] = {}
    heard = sounds_like(
        db, user, accessible, [s.id for s in _most_played(db, user, seeds, COLLECTION_AUDIO_SEEDS)],
        pool, weights, skip, taste, "mix", audio.max_audio_picks(limit), used, versions, liked, rng,
        accept=lambda pick: own is None or _group_key(pick) != own,
    ) if has_audio else []
    chosen = _balanced_track_sample(
        pool, limit - len(heard), liked, weights, SHORT_MIX_MAX_PER_ARTIST,
        rng=rng, used=used, versions=versions,
    )
    log_mix_build("collection", artist or album or "", len(pool), chosen + heard, weights, taste)
    return _finish_mix(db, user, chosen + heard, versions, liked, rng)


def _most_played(db: Session, user: User, items: list[_Pick], limit: int) -> list[_Pick]:
    counts = dict(
        db.execute(
            select(PlayHistory.media_item_id, func.count(PlayHistory.id))
            .where(
                PlayHistory.user_id == user.id,
                PlayHistory.media_item_id.in_([i.id for i in items[:500]]),
            )
            .group_by(PlayHistory.media_item_id)
        ).all()
    )
    return sorted(items, key=lambda i: (-counts.get(i.id, 0), i.id))[:limit]


def save_auto_playlist_as_new(
    db: Session, user: User, playlist_id: int, name: str | None
) -> Playlist | None:
    """Copies an auto-managed playlist's (Discovery or Generated) current
    contents into a brand new, ordinary playlist - the auto slot keeps
    getting overwritten on the next refresh/generate, this copy doesn't."""
    source = db.get(Playlist, playlist_id)
    if source is None or source.user_id != user.id or source.auto_kind is None:
        return None
    new_playlist = Playlist(
        user_id=user.id, name=(name or source.name).strip() or source.name
    )
    db.add(new_playlist)
    db.commit()
    db.refresh(new_playlist)
    # Only what the source currently shows - a generated playlist hides
    # songs disliked since it was built (see visible_auto_items).
    for item in visible_auto_items(db, user, source, list(source.items)):
        db.add(
            PlaylistItem(
                playlist_id=new_playlist.id,
                media_item_id=item.media_item_id,
                position=item.position,
            )
        )
    db.commit()
    return new_playlist
