"""The Discovery playlist: tracks by similar artists the user owns but does not play."""


import random
from collections.abc import Collection, Mapping

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.access import accessible_root_ids
from app.discovery.common import SHORT_MIX_MAX_PER_ARTIST, _utcnow
from app.discovery.eligibility import mix_eligible
from app.discovery.familiarity import (
    draw_weights,
    fatigued_ids,
    log_mix_build,
    user_taste,
)
from app.discovery.sampling import (
    _balanced_track_sample,
    _finish_mix,
    _group_key,
    _Pick,
    _picks,
    _picks_for_ids,
    recording_keys_of,
)
from app.discovery.similar import (
    _artists_similar_by_genre,
    _online_allowed,
    _recent_top_tracks,
    _recently_top_played_artists,
)
from app.discovery.state import _get_or_create_state
from app.folder_exclusions import not_in_ignored_folder
from app.listenbrainz_data import similar_owned_weights
from app.models import (
    MediaItem,
    MediaKind,
    Playlist,
    PlaylistItem,
    User,
)
from app.preferences import liked_levels, not_disliked

DISCOVERY_PLAYLIST_NAME = "Discovery"


MAX_DISCOVERY_TRACKS = 25


def refresh_discovery_playlist(
    db: Session, user: User, rng: random.Random | None = None
) -> dict:
    """Rebuild the user's auto-managed "Discovery" playlist: seed from
    their most-played local artists *in the last RECENT_ARTIST_WINDOW_DAYS*
    (falling back to all-time if nothing recent), then draw similar artists
    from the rest of the library by their genres and tags (with an optional
    ListenBrainz bonus) - see _artists_similar_by_genre - and fill the playlist
    from their tracks the user has not played much (_discovery_tracks).
    Only ever recommends what's actually in the library - there's nothing
    else this app could play - and works with ListenBrainz off or unreachable."""
    top_artists = _recently_top_played_artists(db, user)
    if not top_artists:
        return {"seed_artists": [], "similar_artists": [], "tracks_added": 0}

    accessible = accessible_root_ids(db, user)
    online = _online_allowed(user)
    similar_names = _artists_similar_by_genre(
        db,
        accessible,
        top_artists,
        online=online,
        skip_disliked_for=user.id,
        rng=rng,
    )

    # Songs ListenBrainz calls similar to the user's most played recent
    # songs join the pool, weighted by match - an explicit refresh, so it may
    # ask ListenBrainz (once per seed song, then cached).
    track_weights = similar_owned_weights(db, accessible, _recent_top_tracks(db, user), online)
    candidates = _discovery_tracks(
        db, user, similar_names, track_weights, MAX_DISCOVERY_TRACKS, rng=rng
    )

    playlist = db.scalars(
        select(Playlist)
        .where(Playlist.user_id == user.id, Playlist.auto_kind == "discovery")
        .limit(1)
    ).first()
    if playlist is None:
        playlist = Playlist(
            user_id=user.id,
            name=DISCOVERY_PLAYLIST_NAME,
            is_auto_generated=True,
            auto_kind="discovery",
        )
        db.add(playlist)
        db.commit()
        db.refresh(playlist)
    else:
        db.execute(delete(PlaylistItem).where(PlaylistItem.playlist_id == playlist.id))

    for position, item in enumerate(candidates):
        db.add(
            PlaylistItem(
                playlist_id=playlist.id, media_item_id=item.id, position=position
            )
        )
    db.commit()

    state = _get_or_create_state(db, user)
    state.discovery_seed_artists_json = list(top_artists)
    state.discovery_similar_artists_json = sorted(similar_names)
    state.discovery_tracks_added = len(candidates)
    state.discovery_updated_at = _utcnow()
    db.commit()

    return {
        "playlist_id": playlist.id,
        "seed_artists": top_artists,
        "similar_artists": sorted(similar_names),
        "tracks_added": len(candidates),
    }


def _discovery_tracks(
    db: Session,
    user: User,
    similar_names: Collection[str],
    track_weights: Mapping[int, float],
    limit: int,
    *,
    exclude_ids: Collection[int] = (),
    rng: random.Random | None = None,
) -> list[MediaItem]:
    """Discovery's tracks (also its "Add more"): by the similar artists, or
    ListenBrainz-similar to the user's recent favourites (drawn more often by
    match), never a disliked, ignored or ineligible one, nor a version of
    one in `exclude_ids`, nor one the user has worn out lately (fatigue at
    DISCOVERY_FATIGUE_EXCLUDE or more). How established a track already is
    lowers its chance smoothly instead (app.discovery.familiarity, kind
    "discovery"): an unplayed track counts fully, a half-familiar one a
    quarter, and a song played a few times long ago comes back into reach.

    Drawn with the shared sampler (artist first, SHORT_MIX_MAX_PER_ARTIST
    each, one version per recording) and ordered for cohesion. Likes count
    only for an artist liked as a whole - Discovery is for music the user has
    not heard much, so a few liked songs must not take it over."""
    if not similar_names and not track_weights:
        return []
    by_artist_or_similar = []
    if similar_names:
        by_artist_or_similar.append(MediaItem.artist.in_(list(similar_names)))
    if track_weights:
        by_artist_or_similar.append(MediaItem.id.in_(list(track_weights)))
    pool = _picks(
        db,
        select(MediaItem).where(
            MediaItem.library_root_id.in_(accessible_root_ids(db, user)),
            MediaItem.kind == MediaKind.track,
            or_(*by_artist_or_similar),
            not_in_ignored_folder(),
            not_disliked(user.id),
            mix_eligible(),
        ),
    )
    # Filtered here rather than as NOT IN lists: a long listening history
    # holds thousands of recently played tracks.
    taste = user_taste(db, user)
    skip = fatigued_ids(taste) | set(exclude_ids)
    used = recording_keys_of(_picks_for_ids(db, exclude_ids)) if exclude_ids else set()
    pool = [item for item in pool if item.id not in skip]
    weights = draw_weights(taste, pool, "discovery", track_weights)
    liked_artists = {
        _group_key(item)
        for item, level in zip(pool, _levels(db, user, pool), strict=True)
        if level == "artist"
    }
    liked = {item.id: "artist" for item in pool if _group_key(item) in liked_artists}
    versions: dict[int, list[_Pick]] = {}
    chosen = _balanced_track_sample(
        pool, limit, liked, weights, SHORT_MIX_MAX_PER_ARTIST,
        rng=rng, used=used, versions=versions,
    )
    log_mix_build("discovery", DISCOVERY_PLAYLIST_NAME, len(pool), chosen, weights, taste)
    return _finish_mix(db, user, chosen, versions, liked, rng)


def _levels(db: Session, user: User, pool: list[_Pick]) -> list[str | None]:
    levels = liked_levels(db, user, [item.id for item in pool])
    return [levels.get(item.id) for item in pool]
