"""Automatic genre and tag mixes, cluster mixes and user-specified mixes."""

import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.access import accessible_root_ids
from app.collage import collage_media_item_ids
from app.custom_art import purge_custom_art
from app.discovery.blocklist import blocked_for_mixes
from app.discovery.clusters import (
    MAX_CLUSTER_MIXES,
    cluster_label_profiles,
    cluster_name,
    cluster_selector,
    cluster_tracks,
    find_clusters,
)
from app.discovery.common import MIX_MAX_PER_ARTIST
from app.discovery.eligibility import mix_eligible
from app.discovery.familiarity import draw_weights, log_mix_build, user_taste
from app.discovery.labels import (
    CLUSTER_PREFIX,
    Label,
    _clean_labels,
    _label_filter,
    _specified_selector,
    parse_mix_selector,
    selector_display_name,
)
from app.discovery.pools import (
    _co_occurring_labels,
    _label_overlaps,
    _mix_pool,
    _umbrella_labels,
    _weighted_mix_selection,
    label_listening,
    listening_mix_selection,
)
from app.discovery.sampling import (
    _balanced_track_sample,
    _finish_mix,
    _load_tracks,
    _Pick,
    _picks,
    _rng,
    _tiered_track_sample,
)
from app.discovery.similar import _label_index
from app.folder_exclusions import not_in_ignored_folder
from app.library_query import label_track_rows
from app.metadata_taxonomy import normalise_name
from app.models import (
    MediaItem,
    MediaKind,
    Playlist,
    PlaylistItem,
    PlaySourceHistory,
    User,
)
from app.preferences import (
    liked_levels,
    not_disliked,
)

# A selector with only a couple of tracks makes a lousy automatic mix -
# shuffling the same 2-3 songs over and over isn't much of a mix.
GENRE_MIX_MIN_TRACKS = 15
MAX_GENRE_MIXES = 13
GENRE_MIX_TRACK_LIMIT = 40

logger = logging.getLogger("app.discovery")


def list_genre_mix_playlists(db: Session, user: User) -> list[Playlist]:
    """The user's current featured genre/tag mixes - generated (and
    refreshed) by refresh_all_genre_mixes, not computed on the fly, so
    opening one is as instant as opening any other playlist instead of
    generating it at click-time."""
    playlists = db.scalars(
        select(Playlist).where(Playlist.user_id == user.id, Playlist.auto_kind == "mix")
    ).all()
    # Cluster mixes come first, heaviest first (the order they were made
    # in), then the label mixes in alphabetical order. Specified mixes are
    # appended in creation order so clicking "Specify mix" always adds its
    # result to the end of the Home rail.
    def order(playlist: Playlist) -> tuple[int, int, str]:
        selector = playlist.mix_genre or ""
        if selector.startswith(CLUSTER_PREFIX):
            return (0, playlist.id, "")
        if selector.startswith("specified:"):
            return (2, playlist.id, "")
        return (1, 0, selector.casefold())

    return sorted(playlists, key=order)


def _preserve_mix_history_art(db: Session, user: User, playlist: Playlist) -> None:
    """Save a mix's current cover on its history entry before replacement.

    Genre mixes are managed slots: a refresh can remove a previously
    featured genre altogether.  Its history row still provides a useful
    record, but can no longer resolve art through the removed playlist.
    """
    art_media_item_id = db.execute(
        select(PlaylistItem.media_item_id)
        .where(PlaylistItem.playlist_id == playlist.id)
        .order_by(PlaylistItem.position)
        .limit(1)
    ).scalar_one_or_none()
    db.execute(
        update(PlaySourceHistory)
        .where(
            PlaySourceHistory.user_id == user.id,
            PlaySourceHistory.kind == "playlist",
            PlaySourceHistory.payload_json == {"playlist_id": playlist.id},
        )
        .values(
            {
                PlaySourceHistory.art_media_item_id: func.coalesce(
                    art_media_item_id, PlaySourceHistory.art_media_item_id
                )
            }
        ),
        execution_options={"synchronize_session": False},
    )


def _store_mix_preview_ids(db: Session, playlist: Playlist) -> None:
    """Materialize a mix's Home collage ids onto the playlist row so /mixes
    can serve them without the client fetching each mix's full track list.
    Only meaningful right after the mix's items were (re)built; safe on an
    empty mix (stores "[]")."""
    playlist.preview_media_item_ids_json = collage_media_item_ids(playlist.items)


def _coherent_mix_tracks(
    db: Session,
    user: User,
    primary: list[_Pick],
    primary_name: str,
    rng: random.Random | None = None,
) -> list[MediaItem]:
    """An automatic mix's tracks: every one carries the primary label when the
    library has enough of them; only a shortfall is made up from the tags that
    most often co-occur with it. The result is ordered so neighbours share the
    most tags."""
    limit = GENRE_MIX_TRACK_LIMIT
    secondary: list[_Pick] = []
    used: set[tuple] = set()
    versions: dict[int, list[_Pick]] = {}
    liked: dict[int, str] = {}
    taste = user_taste(db, user)
    # The primary label's tracks are drawn first, so a co-occurring label
    # only ever fills what they cannot.
    primary_liked = liked_levels(db, user, [item.id for item in primary])
    liked.update(primary_liked)
    weights = draw_weights(taste, primary, "mix")
    chosen = _balanced_track_sample(
        primary, limit, primary_liked, weights, MIX_MAX_PER_ARTIST,
        rng=rng, used=used, versions=versions,
    )
    shortfall = limit - len(chosen)
    # Fewer than `limit` tracks, so loading them with their labels is cheap.
    co_labels = (
        _co_occurring_labels(_load_tracks(db, primary), primary_name) if shortfall > 0 else []
    )
    if co_labels:
        primary_ids = {item.id for item in primary}
        pool = _picks(
            db,
            select(MediaItem).where(
                MediaItem.library_root_id.in_(accessible_root_ids(db, user)),
                MediaItem.kind == MediaKind.track,
                not_in_ignored_folder(),
                not_disliked(user.id),
                mix_eligible(),
                or_(*[
                    clause
                    for label in co_labels
                    for clause in (_label_filter("genre", label), _label_filter("tag", label))
                ]),
            )
        )
        pool = [item for item in pool if item.id not in primary_ids]
        pool_liked = liked_levels(db, user, [item.id for item in pool])
        liked.update(pool_liked)
        pool_weights = draw_weights(taste, pool, "mix")
        weights.update(pool_weights)
        secondary = _balanced_track_sample(
            pool, shortfall, pool_liked, pool_weights, MIX_MAX_PER_ARTIST,
            rng=rng, used=used, versions=versions,
        )
    log_mix_build("label", primary_name, len(primary), chosen + secondary, weights, taste)
    return _finish_mix(db, user, chosen + secondary, versions, liked, rng)


def refresh_all_genre_mixes(
    db: Session, user: User, rng: random.Random | None = None
) -> list[Playlist]:
    """Rebuilds the featured "mix" playlists - real auto-managed playlists
    (like Discovery), not just a client-side genre filter, so they're
    save-able via save_auto_playlist_as_new and open instantly. Any
    previously-featured mix not picked this round is dropped - the same
    "overwritten unless saved" contract as Discovery/Generated.

    Two families share the MAX_GENRE_MIXES slots:

    * cluster mixes first (app.discovery.clusters): up to MAX_CLUSTER_MIXES
      groups of artists the user plays together, about three-quarters
      familiar tracks and the rest new to them; none without listening
      history;
    * label mixes for the rest: genres and tags with GENRE_MIX_MIN_TRACKS
      tracks or more, never a non-genre tag (app.discovery.blocklist). For a
      user with recent plays they are drawn by how much of that listening
      carries the label times how specific it is, with about a quarter of
      the slots kept for labels they have not been listening to but which
      sit next to their favourites, or are simply big
      (pools.listening_mix_selection). Without recent plays the draw is by
      size (_weighted_mix_selection)."""
    rng_ = _rng(rng)
    accessible = accessible_root_ids(db, user)
    # Label sizes are an aggregate and only the qualifying labels' pairwise
    # overlaps are counted; tracks are ids until the final picks.
    # The length/album-type rules (mix_eligible) apply to each mix's own
    # pool below, not to these counts: on a 100k-track library they would
    # add more than half a second to every refresh for the rare label made
    # mostly of interludes or spoken word.
    rows = label_track_rows(accessible, not_disliked(user.id)).subquery()
    counts = db.execute(
        select(rows.c.kind, rows.c.name, func.count(rows.c.media_item_id))
        .group_by(rows.c.kind, rows.c.name)
        .order_by(rows.c.kind, rows.c.name)
    ).all()
    label_names: dict[str, str] = {}
    for _kind, name, _count in counts:  # genres first: their spelling wins
        label_names.setdefault(normalise_name(name), name)
    qualifying = [
        (kind, name, count)
        for kind, name, count in counts
        if count >= GENRE_MIX_MIN_TRACKS and not blocked_for_mixes(name)
    ]

    existing_by_genre = {
        p.mix_genre: p
        for p in db.scalars(
            select(Playlist).where(Playlist.user_id == user.id, Playlist.auto_kind == "mix")
        )
    }

    def store(selector: str, playlist_name: str, track_ids: list[int]) -> None:
        playlist = existing_by_genre.pop(selector, None)
        if playlist is None:
            playlist = Playlist(
                user_id=user.id,
                name=playlist_name,
                is_auto_generated=True,
                auto_kind="mix",
                mix_genre=selector,
            )
            db.add(playlist)
            db.commit()
            db.refresh(playlist)
        else:
            playlist.name = playlist_name
            _preserve_mix_history_art(db, user, playlist)
            db.execute(delete(PlaylistItem).where(PlaylistItem.playlist_id == playlist.id))
        for position, media_item_id in enumerate(track_ids):
            db.add(
                PlaylistItem(
                    playlist_id=playlist.id,
                    media_item_id=media_item_id,
                    position=position,
                )
            )
        db.commit()
        db.refresh(playlist)
        _store_mix_preview_ids(db, playlist)
        db.commit()

    made = 0
    clusters = find_clusters(db, user, rng_)
    if clusters:
        index, profiles = cluster_label_profiles(db, user)
        for cluster in clusters:
            if made >= MAX_CLUSTER_MIXES:
                break
            name = cluster_name(cluster, index, profiles, label_names)
            tracks = cluster_tracks(db, user, name, cluster.artists, GENRE_MIX_TRACK_LIMIT, rng=rng)
            if len(tracks) < GENRE_MIX_MIN_TRACKS:
                continue
            store(cluster_selector(name, cluster), name if " mix: " in name else f"{name} Mix", [t.id for t in tracks])
            made += 1

    label_slots = MAX_GENRE_MIXES - made
    overlaps = _label_overlaps(db, rows, qualifying)
    umbrellas = _umbrella_labels(qualifying, overlaps)
    taste = user_taste(db, user)
    shares = label_listening(db, accessible, user, taste.track_window) if taste.track_window else {}
    if shares:
        listened, explored = listening_mix_selection(
            qualifying, label_slots, umbrellas, shares, _label_index(db, accessible).idf, overlaps, rng_,
        )
        logger.info(
            "Label mixes for user %s: from listening %s; to explore %s",
            user.id, [name for _k, name, _c in listened], [name for _k, name, _c in explored],
        )
        chosen = listened + explored
    else:
        chosen = _weighted_mix_selection(qualifying, label_slots, umbrellas)

    for kind, name, _ in chosen:
        selector = name if kind == "genre" else f"tag:{name}"
        candidates = _picks(
            db,
            select(MediaItem).where(
                MediaItem.library_root_id.in_(accessible),
                MediaItem.kind == MediaKind.track,
                _label_filter(kind, name),
                not_in_ignored_folder(),
                not_disliked(user.id),
                mix_eligible(),
            )
        )
        store(selector, f"{name} Mix", [c.id for c in _coherent_mix_tracks(db, user, candidates, name, rng)])

    # Anything left in existing_by_genre wasn't picked this round.
    for stale in existing_by_genre.values():
        _preserve_mix_history_art(db, user, stale)
        purge_custom_art(db, "playlist", [stale.id])
        db.delete(stale)
    db.commit()

    return list_genre_mix_playlists(db, user)


@dataclass(frozen=True)
class SpecifiedMix:
    """A freshly built specified mix and how well it matched the request."""

    playlist: Playlist
    # How many of its tracks carry every include. Fewer than the mix holds
    # means the rest were filled from tracks carrying only some of them.
    full_match_count: int


def add_specified_mix_playlist(
    db: Session,
    user: User,
    kind: str | None = None,
    name: str | None = None,
    *,
    include: Sequence[Label] = (),
    exclude: Sequence[Label] = (),
    rng: random.Random | None = None,
) -> Playlist:
    """build_specified_mix, for callers that only want the playlist."""
    return build_specified_mix(db, user, kind, name, include=include, exclude=exclude, rng=rng).playlist


def build_specified_mix(
    db: Session,
    user: User,
    kind: str | None = None,
    name: str | None = None,
    *,
    include: Sequence[Label] = (),
    exclude: Sequence[Label] = (),
    rng: random.Random | None = None,
) -> SpecifiedMix:
    """Create or replace the temporary mix for chosen genres/tags.

    A track qualifies with at least one include and none of the excludes.
    The mix is filled from the tracks carrying every include first, then
    those missing one, and so on, so a subset of the labels is only used
    for what the full set cannot supply. `kind`/`name` add one more include
    (the one-label form).
    """
    labels = list(include)
    if kind is not None or name is not None:
        labels.append((kind or "", name or ""))
    includes = _clean_labels(labels)
    excludes = _clean_labels(list(exclude))
    if not includes:
        raise ValueError("A mix must specify a genre or tag")
    selector = _specified_selector(includes, excludes)
    candidates, weights = _mix_pool(db, user, includes, excludes)
    if not candidates:
        raise ValueError("That genre or tag has no accessible tracks")
    def label_key(labels: list[Label]) -> frozenset[Label]:
        return frozenset((kind, normalise_name(name)) for kind, name in labels)

    wanted = (label_key(includes), label_key(excludes))
    playlist = next(
        (
            p
            for p in db.scalars(
                select(Playlist).where(
                    Playlist.user_id == user.id,
                    Playlist.auto_kind == "mix",
                    Playlist.mix_genre.like("specified:%"),
                )
            )
            if tuple(label_key(part) for part in parse_mix_selector(p.mix_genre or "")) == wanted
        ),
        None,
    )
    if playlist is None:
        playlist = Playlist(
            user_id=user.id,
            name=f"{selector_display_name(selector)} Mix",
            is_auto_generated=True,
            auto_kind="mix",
            mix_genre=selector,
        )
        db.add(playlist)
        db.commit()
        db.refresh(playlist)
    else:
        db.execute(delete(PlaylistItem).where(PlaylistItem.playlist_id == playlist.id))
    liked = liked_levels(db, user, [c.id for c in candidates])
    versions: dict[int, list[_Pick]] = {}
    taste = user_taste(db, user)
    draw = draw_weights(taste, candidates, "mix")
    picked = _tiered_track_sample(
        candidates, weights, GENRE_MIX_TRACK_LIMIT, liked, MIX_MAX_PER_ARTIST,
        rng=rng, versions=versions, weights=draw,
    )
    log_mix_build("specified", selector, len(candidates), picked, draw, taste)
    sample = _finish_mix(db, user, picked, versions, liked, rng)
    for position, item in enumerate(sample):
        db.add(PlaylistItem(playlist_id=playlist.id, media_item_id=item.id, position=position))
    db.commit()
    db.refresh(playlist)
    _store_mix_preview_ids(db, playlist)
    db.commit()
    # `weights` is each candidate's count of matching includes (a preferred
    # version swapped in by _finish_mix is a candidate too).
    full = sum(1 for item in sample if weights.get(item.id, 1) >= len(includes))
    return SpecifiedMix(playlist, full)
