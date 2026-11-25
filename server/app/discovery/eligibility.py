"""Which tracks automatic picks may use, and who and what a track is for mixing.

One eligibility rule for every mix kind and autoplay (`mix_eligible`), one
artist identity (`artist_identity`, `_collapse_duets`) and one recording
identity (`recording_keys`), so Discovery, Song Mix, artist/album mixes, the
genre mixes and autoplay never disagree on them."""

import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from functools import lru_cache
from typing import Any

from sqlalchemy import ColumnElement, String, and_, func, or_, select, type_coerce
from sqlalchemy.orm import Session

from app.artist_credits import ROLE_MAIN, normalize_artist_name
from app.config import settings
from app.library_cache import library_version
from app.models import Album, Artist, MediaItem, TrackArtist
from app.title_match import normalize_title

# MusicBrainz secondary release-group types that are never music to mix in.
# (MusicBrainz spells spoken word "Spokenword"; all compare lower-cased.)
NEVER_MIXED_ALBUM_TYPES = ("spokenword", "interview", "audiobook", "audio drama")
LIVE_ALBUM_TYPE = "live"


def _excluded_album_types() -> tuple[str, ...]:
    if settings.mix_allow_live_albums:
        return NEVER_MIXED_ALBUM_TYPES
    return (*NEVER_MIXED_ALBUM_TYPES, LIVE_ALBUM_TYPE)


def mix_eligible() -> ColumnElement[bool]:
    """SQL predicate on MediaItem: the track may be picked automatically.

    Its length is within mix_min/max_track_seconds (unknown length is fine),
    and its album is not typed as spoken word, interview, audiobook or audio
    drama (or live, when mix_allow_live_albums is off). A track with no album
    row, or an album MusicBrainz has not typed, is eligible.

    The album test is one uncorrelated subquery over the (small) albums
    table, which SQLite runs once per statement, not once per track."""
    lengths = or_(
        MediaItem.duration_seconds.is_(None),
        and_(
            MediaItem.duration_seconds >= settings.mix_min_track_seconds,
            MediaItem.duration_seconds <= settings.mix_max_track_seconds,
        ),
    )
    types_text = func.lower(type_coerce(Album.secondary_types_json, String))
    excluded_albums = select(Album.id).where(
        Album.secondary_types_json.is_not(None),
        or_(*[types_text.like(f'%"{kind}"%') for kind in _excluded_album_types()]),
    )
    albums = or_(MediaItem.album_id.is_(None), MediaItem.album_id.notin_(excluded_albums))
    return and_(lengths, albums)


# "A feat. B", "A ft. B", "A featuring B": the guest part is not who the
# track is by.
_FEAT_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+", re.IGNORECASE)
# Joins in a duet credit: "A & B", "A and B", "A, B", "A x B", "A with B",
# "A vs. B", "A / B", "A; B".
_DUET_RE = re.compile(r"\s*(?:&|,|;|/|\+)\s*|\s+(?:and|x|with|vs\.?)\s+", re.IGNORECASE)


def artist_identity(credit: str | None, artist: str | None) -> str | None:
    """Who a track is by, for spreading and capping artists in a mix: the
    first main artist credit (app.artist_credits) when the track is linked,
    else its artist tag without any "feat." guests - normalised, so "A",
    "a " and "A feat. B" are one artist. None when the track has neither."""
    if credit:
        return credit
    if not artist or not artist.strip():
        return None
    return _tag_identity(artist)


@lru_cache(maxsize=65536)
def _tag_identity(artist: str) -> str | None:
    return normalize_artist_name(_FEAT_RE.split(artist, maxsplit=1)[0]) or None


def _collapse_duets(keys: Iterable[str | None]) -> dict[str, str]:
    """{key: identity} where a duet credit "a & b" (or "a and b", "a, b", ...)
    counts as "a" when "a" also appears on its own among `keys` - so "A" and
    "A & B" share one artist's turn and cap in a mix. A name that only ever
    appears joined ("Simon & Garfunkel") stays itself."""
    present = {key for key in keys if key}
    mapping: dict[str, str] = {}
    for key in present:
        parts = _DUET_RE.split(key, maxsplit=1)
        first = parts[0].strip() if len(parts) > 1 else ""
        mapping[key] = first if first and first != key and first in present else key
    return mapping


def recording_keys(
    media_id: int, artist_key: str | None, title: str | None, mbid: str | None
) -> tuple[tuple, ...]:
    """The identities under which two tracks are one recording: the
    MusicBrainz recording id when known, and (artist, normalised title) -
    title suffixes such as "(2011 Remaster)", "(feat. X)" or "(Deluxe)"
    dropped, live and remix versions kept apart (app.title_match). A track
    with no usable title is only ever itself."""
    keys: list[tuple] = []
    if mbid:
        keys.append(("mbid", mbid))
    normalised = normalize_title(title)
    if normalised:
        keys.append(("title", artist_key or f"#{media_id}", normalised))
    if not keys:
        keys.append(("id", media_id))
    return tuple(keys)


# What discovery computes once per library version: the label index and
# recent seeds' label scores (app.discovery.similar) and the artist credits
# below. Any library change moves the version (ignoring a folder included),
# and entries of an older version are dropped on the next store, so nothing
# outlives what it was computed from. (Tests clear it between databases as
# app.discovery.similar._overlap_cache, the same object.)
#
# Request threads and the scheduler's refresh thread share it. `_cache_lock`
# guards `_overlap_cache` and `_key_locks` (every read, reorder, store and
# eviction; it is never held while computing). `_key_locks` holds one lock
# per key being computed, so requests that miss the same key together - the
# label index after a library change, say - compute it once, while other
# keys go ahead (the same shape as app.library_cache.get_or_compute).
_CACHE_SIZE = 32
_overlap_cache: OrderedDict[tuple, Any] = OrderedDict()
_cache_lock = threading.Lock()
_key_locks: dict[tuple, threading.Lock] = {}


def _lookup(key: tuple) -> Any:
    """The cached value, marked recently used. Call with `_cache_lock` held."""
    cached = _overlap_cache.get(key)
    if cached is not None:
        _overlap_cache.move_to_end(key)
    return cached


def _cached(key: tuple, compute: Callable[[], Any]) -> Any:
    """`compute()` cached under `key`, whose second item is the library
    version it was computed at."""
    with _cache_lock:
        cached = _lookup(key)
        if cached is not None:
            return cached
        key_lock = _key_locks.setdefault(key, threading.Lock())
    with key_lock:
        try:
            with _cache_lock:
                cached = _lookup(key)  # computed while this caller waited
                if cached is not None:
                    return cached
            value = compute()
            with _cache_lock:
                version = library_version()
                for stale in [k for k in _overlap_cache if k[1] != version]:
                    del _overlap_cache[stale]
                # A library change during the computation: this caller gets
                # the value, but it is already out of date and is not kept.
                if key[1] == version:
                    _overlap_cache[key] = value
                    while len(_overlap_cache) > _CACHE_SIZE:
                        _overlap_cache.popitem(last=False)
            return value
        finally:
            with _cache_lock:
                if _key_locks.get(key) is key_lock:
                    del _key_locks[key]


def main_credits(db: Session) -> dict[str, str]:
    """{artist tag: normalised name of its first main-artist credit} for every
    artist tag a scan has linked (app.artist_credits), built once per library
    version in one grouped query - so a mix pool resolves "A feat. B" to "a"
    without a lookup per track. Tracks sharing an artist tag share their
    credits (both come from that tag)."""

    def build() -> dict[str, str]:
        first = func.min(TrackArtist.position)
        rows = db.execute(
            select(MediaItem.artist, Artist.normalized_name, first)
            .join(TrackArtist, TrackArtist.media_item_id == MediaItem.id)
            .join(Artist, Artist.id == TrackArtist.artist_id)
            .where(TrackArtist.role == ROLE_MAIN, MediaItem.artist.is_not(None))
            .group_by(MediaItem.artist)
        )
        # SQLite returns the bare column from the row holding the minimum.
        return {artist: name for artist, name, _position in rows if artist is not None}

    return _cached(("credits", library_version()), build)
