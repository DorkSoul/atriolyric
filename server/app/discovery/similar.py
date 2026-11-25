"""Listening statistics and similar-artist lookups (ListenBrainz and shared library labels)."""

import math
import random
import time
from collections.abc import Collection, Mapping
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy import func, literal, select, union_all
from sqlalchemy.orm import Session

from app.discovery.blocklist import blocked_for_similarity
from app.discovery.common import _utcnow
from app.discovery.eligibility import _cached, artist_identity
from app.discovery.eligibility import (
    _overlap_cache as _overlap_cache,  # re-exported: tests clear it
)
from app.discovery.sampling import _rng
from app.folder_exclusions import not_in_ignored_folder
from app.library_cache import library_version
from app.library_query import label_track_rows
from app.listenbrainz_data import (
    ONLINE_FETCH_BUDGET_SECONDS,
    artist_mbid,
    online_allowed,
)
from app.metadata_providers import listenbrainz
from app.metadata_taxonomy import normalise_name
from app.models import (
    ArtistMetadataCache,
    Genre,
    MediaItem,
    MediaItemGenre,
    MediaItemMusicBrainzTag,
    MediaItemTag,
    MediaKind,
    MusicBrainzTag,
    PlayHistory,
    Tag,
    User,
)
from app.preferences import artist_not_disliked, not_disliked

TOP_ARTIST_LIMIT = 5


# How many library artists can feed the discovery track query - a ceiling
# on the candidate pool, not on the final track count (MAX_DISCOVERY_TRACKS
# still caps that).
MAX_DISCOVERY_ARTISTS = 20
# Score bonus for a candidate that ListenBrainz's similar-artists data
# also names, times its match (0..1), on top of its label score (a cosine,
# 0..1): a perfect match adds half of a perfect label match. Optional - with
# the feature off, ListenBrainz unreachable or nothing cached it simply
# never applies (see _artists_similar_by_genre).
SIMILAR_ARTIST_BONUS = 0.5
# "Recommended from your library" seeds from *recent* listening rather than
# all-time favorites, since taste drifts - an artist played heavily two
# years ago and never since shouldn't keep steering today's recommendations.
RECENT_ARTIST_WINDOW_DAYS = 30


ARTIST_CACHE_TTL = timedelta(days=30)
ARTIST_MISS_TTL = timedelta(hours=6)


# Discovery: the user's most played recent songs whose ListenBrainz similar
# tracks join the candidates.
DISCOVERY_SIMILAR_SEEDS = 5


def _top_played_artists(
    db: Session, user: User, *, since: datetime | None = None
) -> list[str]:
    query = (
        select(MediaItem.artist)
        .join(PlayHistory, PlayHistory.media_item_id == MediaItem.id)
        .where(
            PlayHistory.user_id == user.id,
            MediaItem.kind == MediaKind.track,
            MediaItem.artist.is_not(None),
            not_in_ignored_folder(),
            # Before the LIMIT, so a disliked artist doesn't just take a
            # seed slot and leave Discovery/Explore one seed short.
            artist_not_disliked(user.id, MediaItem.artist),
        )
    )
    if since is not None:
        query = query.where(PlayHistory.played_at >= since)
    rows = db.execute(
        query.group_by(MediaItem.artist)
        .order_by(func.count(PlayHistory.id).desc())
        .limit(TOP_ARTIST_LIMIT)
    ).scalars()
    # The query already excludes NULL artists; this narrows the type.
    return [artist for artist in rows if artist is not None]


def _recently_top_played_artists(db: Session, user: User) -> list[str]:
    """Same as _top_played_artists, but scoped to the last
    RECENT_ARTIST_WINDOW_DAYS - falls back to all-time history if nothing
    was played in that window (an infrequent listener, or a fresh library)
    so Discovery doesn't just go empty during a quiet stretch."""
    recent = _top_played_artists(
        db, user, since=_utcnow() - timedelta(days=RECENT_ARTIST_WINDOW_DAYS)
    )
    return recent if recent else _top_played_artists(db, user)


def _recent_top_tracks(db: Session, user: User) -> list[MediaItem]:
    """The user's most played songs of the last RECENT_ARTIST_WINDOW_DAYS -
    leaving out disliked songs and ignored folders before the LIMIT, as the
    seed artists do, so a song the user has turned against never steers
    Discovery."""
    since = _utcnow() - timedelta(days=RECENT_ARTIST_WINDOW_DAYS)
    ids = db.execute(
        select(PlayHistory.media_item_id)
        .join(MediaItem, MediaItem.id == PlayHistory.media_item_id)
        .where(
            PlayHistory.user_id == user.id,
            PlayHistory.played_at >= since,
            MediaItem.kind == MediaKind.track,
            not_in_ignored_folder(),
            not_disliked(user.id),
        )
        .group_by(PlayHistory.media_item_id)
        .order_by(func.count(PlayHistory.id).desc())
        .limit(DISCOVERY_SIMILAR_SEEDS)
    ).scalars().all()
    return [item for media_id in ids if (item := db.get(MediaItem, media_id)) is not None]


def _online_allowed(user: User) -> bool:
    """Whether ListenBrainz may be asked, and its data used, for this user
    (app.listenbrainz_data.online_allowed)."""
    return online_allowed(user)


def _similar_artists_for(
    db: Session, artist_name: str, online: bool, allow_fetch: bool = True
) -> set[str]:
    """ListenBrainz's similar-artist names for one artist, cached per artist name
    (not per user or request). Successful lookups use a long TTL and empty
    answers a shorter one; failures are not cached so they retry.

    `allow_fetch=False` (used by the end-of-queue autoplay continuation,
    see app.playback._handle_context_exhaustion) means "only ever use what
    was already cached - never call out." External APIs should only ever
    be hit from an explicit user refresh (Discovery, Explore, "Generate
    playlist from this song") or the scheduled daily job, never as a side
    effect of ordinary playback, both for responsiveness and so this app
    doesn't hammer the API's rate limits."""
    cache = db.get(ArtistMetadataCache, artist_name)
    cached = (
        set(cache.similar_artists_json)
        if cache is not None and cache.similar_artists_json is not None
        else None
    )
    if not online:
        # The user turned ListenBrainz off: none of its data shapes their
        # recommendations, cached or not.
        return set()
    if cached is not None:
        ttl = ARTIST_CACHE_TTL if cached else ARTIST_MISS_TTL
        assert cache is not None  # `cached` is only set from an existing cache row
        if not allow_fetch or _utcnow() - cache.fetched_at < ttl:
            return cached
    if not allow_fetch:
        return cached or set()
    # Only an MBID already known (tags, the MusicBrainz tasks, an info
    # dialog): a mix never waits on a MusicBrainz name search.
    mbid = artist_mbid(db, artist_name, allow_fetch=False)
    if mbid is None:
        return cached or set()
    rows = listenbrainz.similar_artists(mbid)
    if rows is None:
        return cached or set()
    scores = _similar_scores(rows)
    names = set(scores)
    if cache is None:
        cache = ArtistMetadataCache(artist=artist_name)
        db.add(cache)
    cache.mbid = mbid
    cache.similar_artists_json = sorted(names)
    cache.similar_scores_json = scores
    cache.fetched_at = _utcnow()
    db.commit()
    return names


def _similar_scores(rows) -> dict[str, float]:
    """{name: match} from ListenBrainz's {"name", "match"} rows, (name,
    match) pairs or plain names (match unknown, counted as a full match)."""
    scores: dict[str, float] = {}
    for row in rows:
        if isinstance(row, dict):
            name, match = row["name"], row["match"]
        else:
            name, match = (row, 1.0) if isinstance(row, str) else row
        scores[name] = max(scores.get(name, 0.0), float(match))
    return scores


def _similar_artist_match_scores(db: Session, top_artists: list[str]) -> dict[str, float]:
    """Cached ListenBrainz match (0..1) of each similar artist to the best of the
    seeds. Entries cached without a score count as full matches."""
    scores: dict[str, float] = {}
    for artist_name in top_artists:
        cache = db.get(ArtistMetadataCache, artist_name)
        if cache is None or cache.similar_artists_json is None:
            continue
        if cache.similar_scores_json is not None:
            found = cache.similar_scores_json
        else:
            found = dict.fromkeys(cache.similar_artists_json, 1.0)
        for name, match in found.items():
            scores[name] = max(scores.get(name, 0.0), float(match))
    return scores


def _similar_artist_names(
    db: Session,
    top_artists: list[str],
    online: bool,
    allow_fetch: bool = True,
) -> set[str]:
    # Live look-ups stop at the budget (a slow ListenBrainz must not hold a
    # mix for minutes); the rest use what is cached.
    deadline = time.monotonic() + ONLINE_FETCH_BUDGET_SECONDS
    similar_names: set[str] = set()
    for artist_name in top_artists:
        similar_names |= _similar_artists_for(
            db, artist_name, online, allow_fetch=allow_fetch and time.monotonic() < deadline
        )
    return similar_names


def _genres_by_artist(
    db: Session, accessible: Collection[int], artists: list[str]
) -> dict[str, set[str]]:
    """Each artist's distinct genre tags, straight from the library's own
    file tags - no external lookup, so this is always available even for
    an artist nothing's ever been fetched for.  Include every normalized
    embedded genre, not only the first value retained in MediaItem.genre;
    provider genres are additive and are included when available too."""
    result: dict[str, set[str]] = {}
    # Batched: one IN list of every artist would overrun SQLite's
    # host-parameter limit on a library with thousands of artists.
    for start in range(0, len(artists), _ARTIST_BATCH):
        rows = label_track_rows(accessible, MediaItem.artist.in_(artists[start : start + _ARTIST_BATCH])).subquery()
        for artist, name in db.execute(
            select(MediaItem.artist, rows.c.name).join(rows, rows.c.media_item_id == MediaItem.id).distinct()
        ):
            if artist is not None:
                result.setdefault(artist, set()).add(normalise_name(name))
    return result


def _track_labels(item: MediaItem) -> set[str]:
    """Every genre and tag on one track, from the file, MusicBrainz
    and ListenBrainz alike."""
    names = [item.genre, *(g.name for g in item.genres)]
    names += [t.name for t in item.tags] + [t.name for t in item.musicbrainz_tags]
    return {normalise_name(n) for n in names if n}


def _artists_similar_by_genre(
    db: Session,
    accessible: Collection[int],
    top_artists: list[str],
    allow_fetch: bool = True,
    online: bool = False,  # the user's ListenBrainz switch; allow_fetch says whether to ask now
    skip_disliked_for: int | None = None,
    extra_seed_labels: set[str] | None = None,
    rng: random.Random | None = None,
    context_artists: Collection[str] = (),
    context_labels: Collection[str] = (),
) -> set[str]:
    """Up to MAX_DISCOVERY_ARTISTS library artists similar to the seed
    artists, as the artist tags their tracks carry.

    Scored against every *other* artist actually in the library by their
    genres and tags (_label_overlap_by_artist: rarer and stronger labels
    count more, an artist is compared by its whole label profile, so a huge
    broadly tagged catalogue does not win by breadth) - always available,
    with no external lookup. ListenBrainz's cached similar-artist data,
    when there is any, adds SIMILAR_ARTIST_BONUS times its
    match on top (already fetched for the seeds via _similar_artist_names
    when `allow_fetch` is True, so this adds no extra API calls; callers
    whose seeds are not kept warm elsewhere - see similar_tracks_for_seed -
    pass `allow_fetch=False` so this never waits on the network).

    The result is drawn, not just the top of the list: the best
    SIMILAR_ARTIST_SHORTLIST artists (ties in random order) are sampled
    without replacement with a chance growing with their score
    (SIMILAR_ARTIST_SAMPLE_EXPONENT), so a refresh or the next Song Mix from
    the same seed does not always bring the same twenty artists. `rng`
    makes the draw reproducible in tests.

    "A", "a" and "A feat. B" are one artist here: the seeds' own variants
    are never recommended, and a recommended artist brings all its variants.

    `context_artists` / `context_labels` (what was heard just before the
    seed: autoplay's earlier tracks, a Song Mix's latest additions) blend in
    a second, centroid profile: an artist's label score is ANCHOR_WEIGHT
    times its likeness to the seed plus the rest times its likeness to the
    context, so a radio follows where the listening went without drifting
    away from where it is anchored. Without them the seed alone counts.

    `skip_disliked_for` (a user id) drops that user's disliked artists from
    the candidates - Discovery picks artists first, so a disliked artist is
    never recommended there. Song Mix and autoplay leave it unset: they pick
    tracks, and a song liked on its own by a disliked artist still
    qualifies (not_disliked filters the rest)."""
    seeds = frozenset(top_artists)
    label_scores = _label_overlap_by_artist(
        db, accessible, seeds, frozenset(extra_seed_labels or ())
    )
    if context_artists or context_labels:
        context = _label_overlap_by_artist(
            db, accessible, frozenset(context_artists), frozenset(context_labels)
        )
        label_scores = {
            artist: ANCHOR_WEIGHT * label_scores.get(artist, 0.0)
            + (1.0 - ANCHOR_WEIGHT) * context.get(artist, 0.0)
            for artist in label_scores.keys() | context.keys()
        }
    seed_identities = {artist_identity(None, artist) for artist in seeds}

    similar = _similar_artist_names(db, top_artists, online, allow_fetch=allow_fetch)
    online_matches = _library_artists(db, accessible, similar - seeds) if similar else set()
    if skip_disliked_for is not None:
        disliked = _disliked_artists(db, accessible, skip_disliked_for, set(label_scores) | online_matches)
        label_scores = {artist: score for artist, score in label_scores.items() if artist not in disliked}
        online_matches -= disliked
    match_scores = _similar_artist_match_scores(db, top_artists) if online_matches else {}

    # Variants of one artist ("A", "A feat. B") pool under one identity,
    # scored by the best of them.
    scores: dict[str, float] = {}
    names: dict[str, set[str]] = {}
    for artist in set(label_scores) | online_matches:
        identity = artist_identity(None, artist)
        if identity is None or identity in seed_identities:
            continue
        # An optional extra on top of the label score, by how similar
        # ListenBrainz says the artist is (a full match adds SIMILAR_ARTIST_BONUS).
        bonus = (
            SIMILAR_ARTIST_BONUS * match_scores.get(artist, 0.5)
            if artist in online_matches
            else 0.0
        )
        score = label_scores.get(artist, 0.0) + bonus
        if score > 0:
            scores[identity] = max(scores.get(identity, 0.0), score)
            names.setdefault(identity, set()).add(artist)
    return {name for identity in _sample_artists(scores, rng) for name in names[identity]}


# A radio's anchor (the seed of a Song Mix, the track that just ended for
# autoplay) keeps this share of an artist's label score when what was heard
# before it is blended in (see _artists_similar_by_genre): half, so the
# recent past steers but cannot pull the radio away from its anchor.
ANCHOR_WEIGHT = 0.5
# Similar artists are drawn from this many best-scoring candidates...
SIMILAR_ARTIST_SHORTLIST = 60
# ...each with a chance proportional to its score to this power: 2 makes the
# best match about nine times as likely to be drawn first as one scoring a
# third of it, so the closest artists usually make it while the rest of the
# shortlist rotates.
SIMILAR_ARTIST_SAMPLE_EXPONENT = 2.0


def _sample_artists(scores: Mapping[str, float], rng: random.Random | None) -> list[str]:
    """MAX_DISCOVERY_ARTISTS of `scores`' keys: the SIMILAR_ARTIST_SHORTLIST
    best (equal scores in random order), then a weighted draw without
    replacement (Efraimidis-Spirakis keys)."""
    rng = _rng(rng)
    shortlist = sorted(scores, key=lambda artist: (-scores[artist], rng.random()))[:SIMILAR_ARTIST_SHORTLIST]
    keyed = sorted(
        shortlist,
        key=lambda artist: rng.random() ** (1.0 / scores[artist] ** SIMILAR_ARTIST_SAMPLE_EXPONENT),
        reverse=True,
    )
    return keyed[:MAX_DISCOVERY_ARTISTS]


# Bounds each IN (...) list well under SQLite's host-parameter limit.
_ARTIST_BATCH = 500
# The label index and recent seeds' label scores live in the discovery
# cache (app.discovery.eligibility._cached), keyed by library version.


# How much one label on one track counts. A genre (embedded, MusicBrainz or
# the legacy column) and a library tag are asserted outright: 1. A
# MusicBrainz folksonomy tag counts by its votes - one vote 0.5, two 0.75,
# three or more 1 (0.25 at the least, for a tag voted to zero). A
# ListenBrainz artist tag (the same MusicBrainz votes, on the artist) counts
# the same way.
def _tag_weight():
    votes = MediaItemMusicBrainzTag.vote_count
    return func.min(1.0, func.max(0.25, 0.5 + 0.25 * (votes - 1)))


class _LabelIndex(NamedTuple):
    """Every library artist's label profile: profile[artist][label] is the
    label's rarity (idf) times how much of the artist's catalogue carries it
    (the summed label weights over its track count, at most 1)."""

    idf: dict[str, float]
    profiles: dict[str, dict[str, float]]
    norms: dict[str, float]
    by_label: dict[str, list[tuple[str, float]]]


def _label_index(db: Session, accessible: Collection[int]) -> _LabelIndex:
    """The label index of the accessible library, built once per library
    version: two grouped queries over the label tables (no rows per track
    reach Python) and a pass over the (artist, label) pairs."""
    roots = frozenset(accessible)
    return _cached(("index", library_version(), roots), lambda: _build_label_index(db, roots))


def _build_label_index(db: Session, accessible: Collection[int]) -> _LabelIndex:
    base = (
        MediaItem.kind == MediaKind.track,
        MediaItem.library_root_id.in_(accessible),
        MediaItem.artist.is_not(None),
        not_in_ignored_folder(),
    )
    one = literal(1.0)
    parts = [
        # The legacy column only for a track with no normalised genres, as
        # app.library_query.genre_rows reads it.
        select(MediaItem.id.label("media_item_id"), MediaItem.artist.label("artist"), MediaItem.genre.label("name"), one.label("w"))
        .where(MediaItem.genre.is_not(None), ~MediaItem.genres.any(), *base),
        select(MediaItem.id, MediaItem.artist, Genre.normalized_name, one)
        .select_from(MediaItemGenre)
        .join(Genre, Genre.id == MediaItemGenre.genre_id)
        .join(MediaItem, MediaItem.id == MediaItemGenre.media_item_id)
        .where(*base),
        select(MediaItem.id, MediaItem.artist, Tag.name, one)
        .select_from(MediaItemTag)
        .join(Tag, Tag.id == MediaItemTag.tag_id)
        .join(MediaItem, MediaItem.id == MediaItemTag.media_item_id)
        .where(*base),
        select(MediaItem.id, MediaItem.artist, MusicBrainzTag.normalized_name, _tag_weight())
        .select_from(MediaItemMusicBrainzTag)
        .join(MusicBrainzTag, MusicBrainzTag.id == MediaItemMusicBrainzTag.musicbrainz_tag_id)
        .join(MediaItem, MediaItem.id == MediaItemMusicBrainzTag.media_item_id)
        .where(*base),
    ]
    rows = union_all(*parts).subquery()
    # A track carrying a label twice (a genre from two sources) counts once.
    per_track = (
        select(rows.c.artist, rows.c.name, func.max(rows.c.w).label("w"))
        .group_by(rows.c.media_item_id, rows.c.artist, rows.c.name)
        .subquery()
    )
    sums: dict[str, dict[str, float]] = {}
    for artist, name, total in db.execute(
        select(per_track.c.artist, per_track.c.name, func.sum(per_track.c.w))
        .group_by(per_track.c.artist, per_track.c.name)
    ):
        if artist is None or not name:
            continue
        label = normalise_name(name)
        if label and not blocked_for_similarity(label):
            labels = sums.setdefault(artist, {})
            labels[label] = labels.get(label, 0.0) + float(total)
    tracks = {
        artist: count
        for artist, count in db.execute(
            select(MediaItem.artist, func.count(MediaItem.id)).where(*base).group_by(MediaItem.artist)
        )
        if artist is not None
    }
    # Rarity over the artists: a label on every artist still counts
    # log 2 (~0.7), one on a single artist of 10,000 counts ~9.2.
    carriers: dict[str, int] = {}
    for labels in sums.values():
        for label in labels:
            carriers[label] = carriers.get(label, 0) + 1
    total_artists = max(len(tracks), 1)
    idf = {label: math.log(1.0 + total_artists / count) for label, count in carriers.items()}
    profiles: dict[str, dict[str, float]] = {}
    norms: dict[str, float] = {}
    by_label: dict[str, list[tuple[str, float]]] = {}
    for artist, labels in sums.items():
        size = max(tracks.get(artist, 1), 1)
        profile = {label: idf[label] * min(1.0, total / size) for label, total in labels.items()}
        norm = math.sqrt(sum(weight * weight for weight in profile.values()))
        if norm <= 0:
            continue
        profiles[artist] = profile
        norms[artist] = norm
        for label, weight in profile.items():
            by_label.setdefault(label, []).append((artist, weight))
    return _LabelIndex(idf, profiles, norms, by_label)


def _label_overlap_by_artist(
    db: Session,
    accessible: Collection[int],
    seed_artists: frozenset[str],
    seed_labels: frozenset[str] = frozenset(),
) -> dict[str, float]:
    """How alike each other library artist's labels are to the seed's
    (0..1, cosine similarity), for artists sharing at least one label.

    The seed is the seed artists' label profiles (each normalised, so a big
    seed catalogue counts as much as a small one) plus, when given, one
    song's or album's own labels (`seed_labels`, normalised names) weighted
    by rarity. A label carried by most artists ("rock", "seen live") adds
    little; a rare one shared ("shoegaze") adds a lot; and dividing by each
    candidate's whole profile means an artist does not score by carrying
    every label there is. Cached per library version and seed."""
    roots = frozenset(accessible)
    return _cached(
        ("scores", library_version(), roots, seed_artists, seed_labels),
        lambda: _score_label_overlap(_label_index(db, roots), seed_artists, seed_labels),
    )


def _score_label_overlap(
    index: _LabelIndex, seed_artists: frozenset[str], seed_labels: frozenset[str]
) -> dict[str, float]:
    seed: dict[str, float] = {}
    for artist in seed_artists:
        profile = index.profiles.get(artist)
        if profile:
            norm = index.norms[artist]
            for label, weight in profile.items():
                seed[label] = seed.get(label, 0.0) + weight / norm
    own = {label: index.idf[label] for label in seed_labels if label in index.idf}
    own_norm = math.sqrt(sum(weight * weight for weight in own.values()))
    for label, weight in own.items():
        seed[label] = seed.get(label, 0.0) + weight / own_norm
    seed_norm = math.sqrt(sum(weight * weight for weight in seed.values()))
    if seed_norm <= 0:
        return {}
    dots: dict[str, float] = {}
    for label, weight in seed.items():
        for artist, other in index.by_label.get(label, ()):
            if artist not in seed_artists:
                dots[artist] = dots.get(artist, 0.0) + weight * other
    return {
        artist: dot / (seed_norm * index.norms[artist])
        for artist, dot in dots.items()
        if dot > 0
    }


def _library_artists(db: Session, accessible: Collection[int], names: Collection[str]) -> set[str]:
    """Which of `names` are an artist on an accessible track outside ignored
    folders."""
    wanted = sorted(names)
    found: set[str] = set()
    for start in range(0, len(wanted), _ARTIST_BATCH):
        found.update(
            artist
            for artist in db.execute(
                select(MediaItem.artist)
                .where(
                    MediaItem.library_root_id.in_(accessible),
                    MediaItem.kind == MediaKind.track,
                    MediaItem.artist.in_(wanted[start : start + _ARTIST_BATCH]),
                    not_in_ignored_folder(),
                )
                .distinct()
            ).scalars()
            if artist is not None
        )
    return found


def _disliked_artists(
    db: Session, accessible: Collection[int], user_id: int, names: Collection[str]
) -> set[str]:
    """Which of `names` the user dislikes as an artist."""
    wanted = sorted(names)
    found: set[str] = set()
    for start in range(0, len(wanted), _ARTIST_BATCH):
        found.update(
            artist
            for artist in db.execute(
                select(MediaItem.artist)
                .where(
                    MediaItem.library_root_id.in_(accessible),
                    MediaItem.kind == MediaKind.track,
                    MediaItem.artist.in_(wanted[start : start + _ARTIST_BATCH]),
                    ~artist_not_disliked(user_id, MediaItem.artist),
                )
                .distinct()
            ).scalars()
            if artist is not None
        )
    return found
