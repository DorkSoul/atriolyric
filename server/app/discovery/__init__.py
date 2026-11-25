"""Discovery, Explore, Song Mix and the automatic mixes, one module per area.
The public names are re-exported here, so `from app.discovery import x` and
`discovery.x` work.

Tests that patch a collaborator must patch it where it is looked up, which is
the submodule that calls it (for example `app.discovery.explore.time.sleep`
or `app.discovery.scheduler.refresh_discovery_playlist`), not this package."""

from app.discovery.common import MIX_MAX_PER_ARTIST, InsufficientMatchesError
from app.discovery.discovery_playlist import (
    DISCOVERY_PLAYLIST_NAME,
    MAX_DISCOVERY_TRACKS,
    refresh_discovery_playlist,
)
from app.discovery.explore import (
    EXPLORE_FAILURE_LIMIT,
    EXPLORE_REQUEST_DELAY_SECONDS,
    EXPLORE_TIME_BUDGET_SECONDS,
    MAX_EXPLORE_CANDIDATES,
    MAX_EXPLORE_RESULTS,
    explore_new_artists,
)
from app.discovery.labels import Label, parse_mix_selector, selector_display_name
from app.discovery.mixes import (
    GENRE_MIX_MIN_TRACKS,
    GENRE_MIX_TRACK_LIMIT,
    MAX_GENRE_MIXES,
    add_specified_mix_playlist,
    build_specified_mix,
    list_genre_mix_playlists,
    refresh_all_genre_mixes,
)
from app.discovery.pools import (
    MIX_CO_TAG_COUNT,
    MIX_SELECTOR_WEIGHT_EXPONENT,
    MIX_UMBRELLA_OVERLAP,
    MIX_UMBRELLA_WEIGHT_FACTOR,
)
from app.discovery.sampling import LIKE_BOOST
from app.discovery.scheduler import run_due_auto_refreshes
from app.discovery.similar import (
    ARTIST_CACHE_TTL,
    ARTIST_MISS_TTL,
    DISCOVERY_SIMILAR_SEEDS,
    MAX_DISCOVERY_ARTISTS,
    RECENT_ARTIST_WINDOW_DAYS,
    SIMILAR_ARTIST_BONUS,
    TOP_ARTIST_LIMIT,
)
from app.discovery.song_mix import (
    ADD_MORE_TRACKS_COUNT,
    COLLECTION_SIMILAR_SEEDS,
    GENERATED_PLAYLIST_NAME_PREFIX,
    MAX_GENERATED_TRACKS,
    MIN_GENERATED_TRACKS,
    add_more_tracks_to_auto_playlist,
    generate_playlist_from_collection,
    generate_playlist_from_track,
    save_auto_playlist_as_new,
    similar_tracks_for_seed,
)
from app.discovery.state import (
    get_discovery_settings,
    get_discovery_state,
    update_discovery_settings,
)

__all__ = [
    "ADD_MORE_TRACKS_COUNT",
    "ARTIST_CACHE_TTL",
    "ARTIST_MISS_TTL",
    "COLLECTION_SIMILAR_SEEDS",
    "DISCOVERY_PLAYLIST_NAME",
    "DISCOVERY_SIMILAR_SEEDS",
    "EXPLORE_FAILURE_LIMIT",
    "EXPLORE_REQUEST_DELAY_SECONDS",
    "EXPLORE_TIME_BUDGET_SECONDS",
    "GENERATED_PLAYLIST_NAME_PREFIX",
    "GENRE_MIX_MIN_TRACKS",
    "GENRE_MIX_TRACK_LIMIT",
    "InsufficientMatchesError",
    "SIMILAR_ARTIST_BONUS",
    "LIKE_BOOST",
    "Label",
    "MAX_DISCOVERY_ARTISTS",
    "MAX_DISCOVERY_TRACKS",
    "MAX_EXPLORE_CANDIDATES",
    "MAX_EXPLORE_RESULTS",
    "MAX_GENERATED_TRACKS",
    "MAX_GENRE_MIXES",
    "MIN_GENERATED_TRACKS",
    "MIX_CO_TAG_COUNT",
    "MIX_MAX_PER_ARTIST",
    "MIX_SELECTOR_WEIGHT_EXPONENT",
    "MIX_UMBRELLA_OVERLAP",
    "MIX_UMBRELLA_WEIGHT_FACTOR",
    "RECENT_ARTIST_WINDOW_DAYS",
    "TOP_ARTIST_LIMIT",
    "add_more_tracks_to_auto_playlist",
    "add_specified_mix_playlist",
    "build_specified_mix",
    "explore_new_artists",
    "generate_playlist_from_collection",
    "generate_playlist_from_track",
    "get_discovery_settings",
    "get_discovery_state",
    "list_genre_mix_playlists",
    "parse_mix_selector",
    "refresh_all_genre_mixes",
    "refresh_discovery_playlist",
    "run_due_auto_refreshes",
    "save_auto_playlist_as_new",
    "selector_display_name",
    "similar_tracks_for_seed",
    "update_discovery_settings",
]
