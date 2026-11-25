"""Picking tracks: lightweight candidate rows and the one artist-balanced
sampler behind every mix kind and autoplay.

Every automatic pick goes the same way (`_balanced_track_sample`): an
artist is chosen first and then one of that artist's recordings, so a
prolific artist gets no more turns than any other; each artist is capped;
two versions of one recording never both make it in; and the version that
does is the one the user likes, plays or would expect (`_prefer_versions`).
The picks are then ordered by app.discovery.labels._order_by_cohesion."""

import random
from collections.abc import Collection, Mapping, Sequence
from typing import NamedTuple, Protocol, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.discovery import audio
from app.discovery.common import MAX_TRACK_WEIGHT
from app.discovery.eligibility import (
    _collapse_duets,
    artist_identity,
    main_credits,
    recording_keys,
)
from app.discovery.labels import _LABEL_LOADS, _order_by_cohesion
from app.models import Album, MediaItem, PlayHistory, User

# A liked song, album or artist gets about this many times an ordinary
# artist's share of every mix kind and autoplay. Discovery counts only an
# artist liked as a whole (it is for music the user has not heard much, so a
# liked song must not crowd it).
LIKE_BOOST = 1.5


# Order within one artist's turn: liked-album tracks, then liked songs, then
# tracks liked only through their artist, then everything else.
_LIKE_ORDER = {"album": 0, "song": 1, "artist": 2}
# Which version of a recording to keep: one liked as a song first.
_VERSION_LIKE_ORDER = {"song": 0, "album": 1, "artist": 2}


class _Pick(NamedTuple):
    """A candidate track as a sample sees it. Mix pools can hold most of a
    library; loading each as a full MediaItem (with its genre/tag
    relationships) would cost hundreds of megabytes, so pools
    are a few columns and only the tracks finally picked are loaded.

    `artist_key` is who the track is by for balancing (see
    app.discovery.eligibility.artist_identity); `title` and `mbid` tell
    versions of one recording apart."""

    id: int
    artist: str | None
    artist_key: str | None = None
    album_id: int | None = None
    title: str | None = None
    mbid: str | None = None


class _Track(Protocol):
    @property
    def id(self) -> int: ...

    @property
    def artist(self) -> str | None: ...


_T = TypeVar("_T", bound=_Track)


def _rng(rng: random.Random | None) -> random.Random:
    """The given generator, else the module's shared one (so random.seed()
    still steers a test that does not pass its own)."""
    return rng if rng is not None else random._inst  # the stdlib's module-level generator


def _picks(db: Session, query: Select) -> list[_Pick]:
    """The candidate columns of every track a MediaItem select matches, by id
    so the samples drawn from them are reproducible. Who each is by comes
    from the cached artist credits (main_credits), resolved once per
    distinct artist tag."""
    rows = db.execute(
        query.with_only_columns(
            MediaItem.id,
            MediaItem.artist,
            MediaItem.album_id,
            MediaItem.title,
            MediaItem.musicbrainz_recording_mbid,
        ).order_by(MediaItem.id)
    ).all()
    credits = main_credits(db) if rows else {}
    identities: dict[str | None, str | None] = {}
    for row in rows:
        artist = row[1]
        if artist not in identities:
            identities[artist] = artist_identity(credits.get(artist) if artist else None, artist)
    collapse = _collapse_duets(identities.values())
    return [
        _Pick(media_id, artist, collapse[key] if (key := identities[artist]) else None, album_id, title, mbid)
        for media_id, artist, album_id, title, mbid in rows
    ]


_ID_BATCH = 900


def _picks_for_ids(db: Session, ids: Collection[int]) -> list[_Pick]:
    """_picks for known ids (a queue, a playlist's items), batched under
    SQLite's parameter limit."""
    wanted = sorted(set(ids))
    found: list[_Pick] = []
    for start in range(0, len(wanted), _ID_BATCH):
        found += _picks(db, select(MediaItem).where(MediaItem.id.in_(wanted[start : start + _ID_BATCH])))
    return found


def _load_tracks(db: Session, picks: Sequence[_Track]) -> list[MediaItem]:
    """Full MediaItems (labels loaded) for the picked tracks, in their order."""
    ids = [pick.id for pick in picks]
    loaded = {
        item.id: item
        for item in db.scalars(select(MediaItem).options(*_LABEL_LOADS).where(MediaItem.id.in_(ids)))
    } if ids else {}
    return [loaded[media_id] for media_id in ids if media_id in loaded]


def _group_key(item: _Track) -> str:
    """The artist a candidate is balanced under. _Picks carry it; plain
    objects (tests, MediaItems) derive it from their artist tag."""
    if isinstance(item, _Pick):
        key = item.artist_key
    else:
        key = artist_identity(None, item.artist)
    return key or f"__item_{item.id}"


def _keys_of(item: _Track, artist_key: str) -> tuple[tuple, ...]:
    return recording_keys(
        item.id,
        artist_key,
        getattr(item, "title", None),
        getattr(item, "mbid", None),
    )


def recording_keys_of(items: Sequence[_Pick]) -> set[tuple]:
    """Every recording identity of `items`: seed a sample's `used` set with
    these to keep other versions of those songs out of it."""
    keys: set[tuple] = set()
    for item in items:
        keys.update(_keys_of(item, _group_key(item)))
    return keys


class _Recording:
    """One recording of one artist in a pool: every version of it (an album
    track, its remaster, a compilation copy), best first."""

    __slots__ = ("keys", "versions")

    def __init__(self, versions: list, keys: set[tuple]) -> None:
        self.versions = versions
        self.keys = keys


def _recordings(
    tracks: list[_T],
    artist_key: str,
    liked: Mapping[int, str],
    weights: Mapping[int, float],
    rng: random.Random,
) -> list[_Recording]:
    """An artist's tracks grouped into recordings and ordered for popping
    from the end: heavier ones (Efraimidis-Spirakis keys) and liked ones
    last, so they are drawn first."""
    groups: dict[tuple, list[_T]] = {}
    keys: dict[tuple, set[tuple]] = {}
    for item in tracks:
        item_keys = _keys_of(item, artist_key)
        # Versions share the (artist, title) key; without a title, the
        # recording id (or the track itself) is the group.
        group = item_keys[-1]
        groups.setdefault(group, []).append(item)
        keys.setdefault(group, set()).update(item_keys)
    recordings: list[tuple[int, float, _Recording]] = []
    for group, versions in groups.items():
        if len(versions) > 1:
            rng.shuffle(versions)
            versions.sort(key=lambda item: _LIKE_ORDER.get(liked.get(item.id, ""), 3))
        weight = max(weights.get(item.id, 1.0) for item in versions) if weights else 1.0
        like = min(_LIKE_ORDER.get(liked.get(item.id, ""), 3) for item in versions) if liked else 3
        draw = rng.random()
        recordings.append((-like, draw if weight == 1.0 else draw ** (1.0 / weight), _Recording(versions, keys[group])))
    # Ascending: unliked before liked, then by weight key, so the liked and
    # the heaviest are popped first.
    recordings.sort(key=lambda entry: (entry[0], entry[1]))
    return [recording for _like, _key, recording in recordings]


def _balanced_track_sample(
    candidates: list[_T],
    limit: int,
    liked: Mapping[int, str] | None = None,
    weights: Mapping[int, float] | None = None,
    max_per_artist: int | None = None,
    *,
    rng: random.Random | None = None,
    used: set[tuple] | None = None,
    versions: dict[int, list[_T]] | None = None,
) -> list[_T]:
    """Pick up to `limit` tracks, giving each artist an even chance to appear.

    Hierarchical: a plain random sample is biased toward artists with large
    discographies, so the draw goes round the artists - each round every
    artist earns one pick and spends it on one of its recordings - which
    makes the first pass artist-balanced and uses later passes only for the
    slots left once every artist has had a turn. Artists are `_group_key`:
    "A", "A feat. B" and (when "A" is in the pool too) "A & B" are one.

    `liked` (media id -> "song"/"album"/"artist", see
    app.preferences.liked_levels) boosts likes: each round an artist earns
    one pick, plus LIKE_BOOST - 1 more while it still has liked tracks left,
    and spends whole picks as its credit allows. An artist liked as a whole
    therefore gets ~LIKE_BOOST times an ordinary artist's share; one with a
    few liked songs or a liked album gets the extra only until those are
    used. Liked tracks go first within an artist's turn. Likes only reorder
    tracks that are already candidates, never add any.

    `weights` (media id -> relative weight, default 1) makes a heavier track
    proportionally likelier to be drawn and a lighter one less likely: it is
    picked earlier (later) within its artist's turn and earns that artist
    proportionally more (fewer) picks per round - a ListenBrainz similar-track
    match, and the user's familiarity, fatigue and listen outcomes
    (app.discovery.familiarity.draw_weights). A liked track's weight is
    multiplied by LIKE_BOOST, the product capped at MAX_TRACK_WEIGHT.

    `max_per_artist` is a soft cap: no artist gets more than that many picks
    while other artists still have tracks to offer. Only if the pool runs out
    of other artists is the cap lifted to fill the remaining slots.

    One recording, one pick: versions of a song (by MusicBrainz recording
    id, else artist and normalised title) count as one candidate, and a
    recording whose identity is already in `used` (shared across calls that
    build one mix, and updated here) is skipped. `versions`, when given,
    receives each pick's id -> every version of its recording, for
    _prefer_versions.
    """
    rng = _rng(rng)
    liked = liked or {}
    weights = weights or {}
    used = set() if used is None else used
    by_artist: dict[str, list[_T]] = {}
    for item in candidates:
        by_artist.setdefault(_group_key(item), []).append(item)
    artists = list(by_artist)
    if weights:
        # Heavier artists tend to come earlier in the round, so a weight
        # also counts when fewer tracks are wanted than there are artists.
        artists.sort(
            key=lambda artist: rng.random()
            ** (1.0 / max(weights.get(item.id, 1.0) for item in by_artist[artist])),
            reverse=True,
        )
    else:
        rng.shuffle(artists)
    # An artist's recordings are only grouped when it first gets a turn: a
    # big pool has thousands of artists and a mix visits a few dozen.
    stacks: dict[str, list[_Recording]] = {}

    def stack(artist: str) -> list[_Recording]:
        if artist not in stacks:
            stacks[artist] = _recordings(by_artist[artist], artist, liked, weights, rng)
        return stacks[artist]

    def next_fresh(artist: str) -> _Recording | None:
        recordings = stack(artist)
        while recordings and (recordings[-1].keys & used):
            recordings.pop()
        return recordings[-1] if recordings else None

    # A random fractional start keeps a boosted artist's average at exactly
    # LIKE_BOOST picks per round instead of rounding its first round down.
    # An ordinary artist still gets exactly one pick every round.
    credits = [rng.random() for _ in artists]
    chosen: list[_T] = []
    picked: dict[str, int] = {}
    held: list[str] = []
    while artists and len(chosen) < limit:
        next_round: list[str] = []
        next_credits: list[float] = []
        for artist, credit in zip(artists, credits, strict=False):
            top = next_fresh(artist)
            if top is None:
                continue
            best = top.versions[0]
            # Likes, listen outcomes, familiarity and a mix's own weights
            # multiply (app.discovery.familiarity), within a cap.
            credit += min(
                MAX_TRACK_WEIGHT,
                (LIKE_BOOST if best.id in liked else 1.0)
                * max(weights.get(item.id, 1.0) for item in top.versions),
            )
            while (
                credit >= 1.0
                and top is not None
                and len(chosen) < limit
                and (max_per_artist is None or picked.get(artist, 0) < max_per_artist)
            ):
                stack(artist).pop()
                used.update(top.keys)
                chosen.append(top.versions[0])
                if versions is not None:
                    versions[top.versions[0].id] = list(top.versions)
                picked[artist] = picked.get(artist, 0) + 1
                credit -= 1.0
                top = next_fresh(artist)
            if len(chosen) >= limit:
                break
            if top is not None and max_per_artist is not None and picked.get(artist, 0) >= max_per_artist:
                held.append(artist)
            elif top is not None:
                next_round.append(artist)
                next_credits.append(credit)
        artists, credits = next_round, next_credits
    if len(chosen) < limit and held:
        # Not enough other artists: lift the cap for the leftover slots.
        leftovers = [
            item for artist in held for recording in stack(artist) for item in recording.versions
        ]
        chosen += _balanced_track_sample(
            leftovers, limit - len(chosen), liked, weights, rng=rng, used=used, versions=versions
        )
    return chosen


def _tiered_track_sample(
    candidates: list[_T],
    matches: Mapping[int, float],
    limit: int,
    liked: Mapping[int, str] | None = None,
    max_per_artist: int | None = None,
    *,
    rng: random.Random | None = None,
    used: set[tuple] | None = None,
    versions: dict[int, list[_T]] | None = None,
    weights: Mapping[int, float] | None = None,
) -> list[_T]:
    """Fill a specified mix best-match first. `matches` is how many of the
    chosen labels each track carries: tracks carrying all of them are taken
    first, then those missing one, and so on - a lower tier is only reached
    when the tiers above it cannot fill `limit`, so a subset of the labels is
    used only for what the full set cannot provide."""
    used = set() if used is None else used
    tiers: dict[int, list[_T]] = {}
    for item in candidates:
        tiers.setdefault(int(matches.get(item.id, 1)), []).append(item)
    chosen: list[_T] = []
    for level in sorted(tiers, reverse=True):
        room = limit - len(chosen)
        if room <= 0:
            break
        chosen.extend(
            _balanced_track_sample(
                tiers[level], room, liked, weights, max_per_artist,
                rng=rng, used=used, versions=versions,
            )
        )
    return chosen


def _album_rank(primary: str | None, secondary: Sequence[str] | None) -> int:
    """How much a version's album is "the" album: a plain studio album
    first, then an EP/single or an untyped album, then a compilation, live
    or other release."""
    extra = {kind.lower() for kind in secondary or []}
    if extra:
        return 2
    if primary == "Album":
        return 0
    if primary in (None, "EP", "Single"):
        return 1
    return 2


def _prefer_versions(
    db: Session,
    user: User,
    chosen: list[_T],
    versions: Mapping[int, Sequence[_T]],
    liked: Mapping[int, str],
) -> list[_T]:
    """Swap each pick for the best version of its recording: the one the user
    liked as a song (then via its album, then its artist), else the one they
    played most, else the one on a studio album rather than a compilation or
    live release. A tie keeps the sampled version. Two queries, only when a
    pick has more than one version."""
    alternatives = {
        pick.id: list(versions[pick.id]) for pick in chosen if len(versions.get(pick.id, ())) > 1
    }
    if not alternatives:
        return chosen
    ids = sorted({item.id for group in alternatives.values() for item in group})
    plays: dict[int, int] = {}
    album_types: dict[int, tuple[str | None, list[str] | None]] = {}
    for start in range(0, len(ids), _ID_BATCH):
        batch = ids[start : start + _ID_BATCH]
        plays.update(
            db.execute(
                select(PlayHistory.media_item_id, func.count(PlayHistory.id))
                .where(PlayHistory.user_id == user.id, PlayHistory.media_item_id.in_(batch))
                .group_by(PlayHistory.media_item_id)
            ).all()
        )
        for media_id, primary, secondary in db.execute(
            select(MediaItem.id, Album.primary_type, Album.secondary_types_json)
            .join(Album, Album.id == MediaItem.album_id)
            .where(MediaItem.id.in_(batch))
        ):
            album_types[media_id] = (primary, secondary)

    def rank(item: _T) -> tuple[int, int, int]:
        primary, secondary = album_types.get(item.id, (None, None))
        return (
            _VERSION_LIKE_ORDER.get(liked.get(item.id, ""), 3),
            -plays.get(item.id, 0),
            _album_rank(primary, secondary),
        )

    result: list[_T] = []
    for pick in chosen:
        group = alternatives.get(pick.id)
        if group is None:
            result.append(pick)
            continue
        # min() keeps the first of equals, and the sampled pick goes first.
        ordered = [pick, *(item for item in group if item.id != pick.id)]
        result.append(min(ordered, key=rank))
    return result


def _finish_mix(
    db: Session,
    user: User,
    chosen: Sequence[_T],
    versions: Mapping[int, Sequence[_T]],
    liked: Mapping[int, str],
    rng: random.Random | None = None,
    first_id: int | None = None,
) -> list[MediaItem]:
    """The last steps every mix kind shares: keep the best version of each
    pick, load the picks, and order them for cohesion with artists and
    albums spread out (`first_id`, a Song Mix's seed, stays first) - and,
    when most of them have been analysed, flowing in sound
    (app.discovery.audio.order_distances)."""
    picks = _prefer_versions(db, user, list(chosen), versions, liked)
    ids = [pick.id for pick in picks]
    items = _load_tracks(db, [_Pick(media_id, None) for media_id in ([first_id, *ids] if first_id is not None else ids)])
    head = items[0] if first_id is not None and items and items[0].id == first_id else None
    distances = audio.order_distances(db, [item.id for item in items])
    return _order_by_cohesion(items, rng=rng, first=head, distances=distances)
