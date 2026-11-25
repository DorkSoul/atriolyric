"""Mix selectors and track labels, and ordering a mix for tag cohesion and artist spacing."""

import json
import random
import statistics
from collections.abc import Mapping

from sqlalchemy import func, or_
from sqlalchemy.orm import selectinload

from app.discovery.eligibility import _collapse_duets, artist_identity
from app.metadata_taxonomy import genre_filter, normalise_name, tag_filter
from app.models import MediaItem

# Per unit of audio distance, against one shared label (see
# _order_by_cohesion and app.discovery.audio).
AUDIO_ORDER_WEIGHT = 1.5


def _mix_selector(kind: str, name: str) -> str:
    return f"specified:{kind}:{name}"


def _selector_parts(selector: str) -> tuple[str, str]:
    if selector.startswith("specified:"):
        kind, separator, name = selector[10:].partition(":")
        # A malformed selector with no kind is read as a bare genre name.
        return (kind, name) if separator else ("genre", kind)
    if selector.startswith("tag:"):
        return ("tag", selector[4:])
    return ("genre", selector)


Label = tuple[str, str]


def _clean_labels(labels: list[Label]) -> list[Label]:
    """Strip, lower-case the kind, drop blanks and duplicates (names compare
    normalised), in a canonical order so equal sets give equal selectors."""
    seen: dict[Label, Label] = {}
    for kind, name in labels:
        kind, name = kind.strip().lower(), name.strip()
        if kind not in {"genre", "tag"}:
            raise ValueError("A mix must specify a genre or tag")
        if name:
            seen.setdefault((kind, normalise_name(name)), (kind, name))
    return [seen[key] for key in sorted(seen)]


def _specified_selector(includes: list[Label], excludes: list[Label]) -> str:
    """The stable `mix_genre` of a specified mix. One include and no excludes
    is `specified:<kind>:<name>`; anything else is
    `specified:` plus canonical JSON, so a mix is identified by its labels."""
    if len(includes) == 1 and not excludes:
        return _mix_selector(*includes[0])
    payload = {"include": includes, "exclude": excludes}
    return "specified:" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# A cluster mix's selector (app.discovery.clusters): this prefix plus JSON
# {"name": display name, "artists": [normalised artist identities]}.
CLUSTER_PREFIX = "cluster:"


def parse_cluster_selector(selector: str) -> tuple[str, list[str]] | None:
    """(display name, artist identities) of a cluster mix's selector, or
    None for any other selector (or a malformed one)."""
    if not selector.startswith(CLUSTER_PREFIX):
        return None
    try:
        payload = json.loads(selector[len(CLUSTER_PREFIX):])
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    name, artists = payload.get("name"), payload.get("artists")
    if not isinstance(name, str) or not isinstance(artists, list):
        return None
    return name, [artist for artist in artists if isinstance(artist, str)]


def parse_mix_selector(selector: str) -> tuple[list[Label], list[Label]]:
    """(includes, excludes) of any mix selector: a plain genre, `tag:<name>`,
    or a specified mix in either form. A cluster mix has no labels."""
    if selector.startswith(CLUSTER_PREFIX):
        return [], []
    if selector.startswith("specified:{"):
        payload = json.loads(selector[10:])
        return (
            [(kind, name) for kind, name in payload["include"]],
            [(kind, name) for kind, name in payload["exclude"]],
        )
    return [_selector_parts(selector)], []


def selector_display_name(selector: str) -> str:
    cluster = parse_cluster_selector(selector)
    if cluster is not None:
        return cluster[0]
    if selector.startswith(CLUSTER_PREFIX):
        return "Mix"
    includes, excludes = parse_mix_selector(selector)
    display = " + ".join(name for _kind, name in includes)
    if excludes:
        display += " (no " + ", ".join(name for _kind, name in excludes) + ")"
    return display


def _label_filter(kind: str, name: str):
    """SQL predicate on MediaItem: the track carries this genre or tag, names
    compared as _label_sets does (case- and spacing-insensitively) - the
    legacy genre column included, which genre_filter matches exactly, so a
    "Rock" file tag counts for a "rock" mix."""
    if kind != "genre":
        return tag_filter(name)
    return or_(genre_filter(name), func.lower(func.trim(MediaItem.genre)) == normalise_name(name))


_LABEL_LOADS = (
    selectinload(MediaItem.genres),
    selectinload(MediaItem.tags),
    selectinload(MediaItem.musicbrainz_tags),
)


def _label_sets(item: MediaItem) -> tuple[set[str], set[str]]:
    """(genres, tags) a track carries, as normalised names."""
    genres = {genre.normalized_name for genre in item.genres}
    if item.genre:
        genres.add(normalise_name(item.genre))
    tags = {normalise_name(tag.name) for tag in item.tags}
    tags |= {tag.normalized_name for tag in item.musicbrainz_tags}
    return genres, tags


def _item_labels(item: MediaItem) -> set[str]:
    genres, tags = _label_sets(item)
    return genres | tags


# Penalty (in shared tags) for placing a track by the same artist this many
# places after that artist's previous track. Steep enough that a clearly
# better tag match elsewhere wins, but a long run of one artist only happens
# when nothing else is left.
_ARTIST_SPACING_PENALTY = (6.0, 4.5, 3.0, 2.0, 1.0, 0.5)
# The same for two tracks of one album (a compilation's various artists
# included), over a shorter reach: an album's tracks already share an
# artist most of the time, so this mostly spreads compilations.
_ALBUM_SPACING_PENALTY = (4.0, 2.0)


def _artist_key(item: MediaItem) -> str | None:
    """Who a track is by for spacing: its artist tag without "feat."
    guests, normalised (app.discovery.eligibility.artist_identity)."""
    return artist_identity(None, item.artist)


def _album_key(item: MediaItem) -> object | None:
    album_id = getattr(item, "album_id", None)
    if album_id is not None:
        return album_id
    album = getattr(item, "album", None)
    if album and album.strip():
        owner = getattr(item, "album_owner", None) or getattr(item, "album_artist", None) or item.artist
        return (normalise_name(owner or ""), normalise_name(album))
    return None


def _spacing_keys(items: list[MediaItem]) -> tuple[list[str | None], list[object | None]]:
    raw = [_artist_key(item) for item in items]
    collapse = _collapse_duets(raw)
    return [collapse[key] if key else None for key in raw], [_album_key(item) for item in items]


def _order_by_cohesion(
    items: list[MediaItem],
    rng: random.Random | None = None,
    first: MediaItem | None = None,
    distances: Mapping[tuple[int, int], float] | None = None,
) -> list[MediaItem]:
    """Greedy nearest-neighbour ordering: start from `first` (a Song Mix's
    seed) or a random track, then repeatedly append the remaining track
    sharing the most genres/tags with the last one. A track by an artist
    heard in the last few places is penalised (most heavily right after
    them, fading over six places), and so is one from an album heard in the
    last two, so one artist's or album's tracks spread out instead of
    arriving in a block - yet can still sit close together when nothing
    else fits. Ties go to the random pre-shuffle order. `first`, when given,
    stays first.

    `distances` (pairs of media ids -> audio distance, app.discovery.audio
    .order_distances) adds sound: each candidate also loses
    AUDIO_ORDER_WEIGHT per unit of distance from the last track, a pair
    without a known distance counting as the median one, so the order also
    flows in tempo, loudness and timbre while the spacing rules hold."""
    if not items:
        return []
    if rng is None:
        rng = random._inst  # the stdlib's module-level generator
    remaining = list(items)
    rng.shuffle(remaining)
    labels = {item.id: _item_labels(item) for item in remaining}
    artist_keys, album_keys = _spacing_keys(remaining)
    artists = {item.id: key for item, key in zip(remaining, artist_keys, strict=True)}
    albums = {item.id: key for item, key in zip(remaining, album_keys, strict=True)}
    if first is not None and any(item.id == first.id for item in remaining):
        start = next(item for item in remaining if item.id == first.id)
        remaining.remove(start)
    else:
        first = None
        start = remaining.pop()
    ordered = [start]
    unknown_distance = statistics.median(distances.values()) if distances else 0.0
    while remaining:
        last = ordered[-1]
        recent = [
            artists[item.id] for item in ordered[-len(_ARTIST_SPACING_PENALTY):][::-1]
        ]
        recent_albums = [
            albums[item.id] for item in ordered[-len(_ALBUM_SPACING_PENALTY):][::-1]
        ]

        def closeness(item: MediaItem) -> float:
            score = float(len(labels[last.id] & labels[item.id]))  # noqa: B023 - closure runs inside this iteration (max() below), so late binding cannot bite
            if distances:
                score -= AUDIO_ORDER_WEIGHT * distances.get((last.id, item.id), unknown_distance)  # noqa: B023 - as above
            artist = artists[item.id]
            if artist is not None:
                for distance, heard in enumerate(recent):  # noqa: B023 - closure runs inside this iteration (max() below), so late binding cannot bite
                    if heard == artist:
                        score -= _ARTIST_SPACING_PENALTY[distance]
                        break
            album = albums[item.id]
            if album is not None:
                for distance, heard in enumerate(recent_albums):  # noqa: B023 - closure runs inside this iteration (max() below), so late binding cannot bite
                    if heard == album:
                        score -= _ALBUM_SPACING_PENALTY[distance]
                        break
            return score

        best = max(remaining, key=closeness)
        remaining.remove(best)
        ordered.append(best)
    return _spread_artists(ordered, keep_first=first is not None, keys=(artists, albums))


# Cost of the same artist appearing this many places apart (1 = back to back).
_SPACING_COST = {1: 10, 2: 3, 3: 1}
# ...and of the same album.
_ALBUM_SPACING_COST = {1: 4, 2: 1}


def _spacing_cost(keys: list[str | None], albums: list[object | None] | None = None) -> int:
    cost = 0
    for gap, weight in _SPACING_COST.items():
        for i in range(gap, len(keys)):
            if keys[i] is not None and keys[i] == keys[i - gap]:
                cost += weight
    for gap, weight in _ALBUM_SPACING_COST.items():
        for i in range(gap, len(albums or [])):
            assert albums is not None
            if albums[i] is not None and albums[i] == albums[i - gap]:
                cost += weight
    return cost


def _spread_artists(
    items: list[MediaItem],
    keep_first: bool = False,
    keys: tuple[dict[int, str | None], dict[int, object | None]] | None = None,
) -> list[MediaItem]:
    """Repair pass over a cohesion-ordered mix: move a track that sits within
    three places of the same artist (or two of the same album) to the nearest
    slot that lowers the spacing cost, until no single move helps. Tracks
    only move as far as they must, so the tag-based order is mostly kept, and
    a run of one artist survives only when there is no other artist left to
    put between them. `keep_first` leaves the first track where it is."""
    items = list(items)
    if keys is None:
        artist_keys, album_keys = _spacing_keys(items)
    else:
        artist_keys = [keys[0][item.id] for item in items]
        album_keys = [keys[1][item.id] for item in items]
    fixed = 1 if keep_first else 0
    improved = True
    while improved:
        improved = False
        current = _spacing_cost(artist_keys, album_keys)
        for i in range(fixed, len(items)):
            key, album = artist_keys[i], album_keys[i]
            near_artist = key is not None and any(
                0 <= i + offset < len(items) and artist_keys[i + offset] == key
                for offset in (-3, -2, -1, 1, 2, 3)
            )
            near_album = album is not None and any(
                0 <= i + offset < len(items) and album_keys[i + offset] == album
                for offset in (-2, -1, 1, 2)
            )
            if not near_artist and not near_album:
                continue
            rest_items = items[:i] + items[i + 1:]
            rest_keys = artist_keys[:i] + artist_keys[i + 1:]
            rest_albums = album_keys[:i] + album_keys[i + 1:]
            for target in sorted(range(fixed, len(items)), key=lambda p: abs(p - i)):  # noqa: B023 - sorted() runs now
                if target == i:
                    continue
                trial_keys = rest_keys[:target] + [key] + rest_keys[target:]
                trial_albums = rest_albums[:target] + [album] + rest_albums[target:]
                if _spacing_cost(trial_keys, trial_albums) < current:
                    items = rest_items[:target] + [items[i]] + rest_items[target:]
                    artist_keys, album_keys = trial_keys, trial_albums
                    improved = True
                    break
            if improved:
                break
    return items
