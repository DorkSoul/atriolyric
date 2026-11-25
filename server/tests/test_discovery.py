from __future__ import annotations

import time
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from app.cleanup import delete_media_records
from app.discovery import (
    MAX_EXPLORE_RESULTS,
    MAX_GENRE_MIXES,
    add_more_tracks_to_auto_playlist,
    add_specified_mix_playlist,
    explore_new_artists,
    generate_playlist_from_track,
    get_discovery_settings,
    get_discovery_state,
    list_genre_mix_playlists,
    refresh_all_genre_mixes,
    refresh_discovery_playlist,
    run_due_auto_refreshes,
    save_auto_playlist_as_new,
    update_discovery_settings,
)
from app.discovery.pools import _weighted_mix_selection
from app.metadata_taxonomy import replace_item_genres
from app.models import (
    DiscoveryState,
    Genre,
    IgnoredLibraryFolder,
    LibraryRoot,
    MediaItem,
    MediaItemGenre,
    MediaKind,
    PlayHistory,
    Playlist,
    PlaylistItem,
    PlaySourceHistory,
    Tag,
    User,
    utcnow,
)
from app.security import hash_password


@pytest.fixture(autouse=True)
def _known_artist_mbids(monkeypatch):
    """Similar artists are asked by MusicBrainz ID; here every artist has
    one (its own name stands in for it), so the mocked ListenBrainz answers
    by name."""
    monkeypatch.setattr("app.discovery.similar.artist_mbid", lambda db, name, **_kw: name)


def _make_track(db_session, root_id, title, artist) -> MediaItem:
    item = MediaItem(
        library_root_id=root_id,
        kind=MediaKind.track,
        relative_path=f"{title}.mp3",
        title=title,
        artist=artist,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


def _play(db_session, user, item, times=1, played_at=None):
    for _ in range(times):
        row = PlayHistory(user_id=user.id, media_item_id=item.id)
        if played_at is not None:
            row.played_at = played_at
        db_session.add(row)
    db_session.commit()


def test_discovery_recommends_by_owned_genre_overlap_without_listenbrainz_naming_it(
    db_session,
):
    """Recommends "Foo Fighters" purely from shared genre tags with the
    seed artist: never depend on
    ListenBrainz's raw similar-artist list actually happening to name
    something owned - genre overlap against the library is the primary,
    always-available signal. "Jazz Guy" shares no genre with the seed and
    isn't returned by ListenBrainz either, so it gets no score at all."""
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()

    seed = _make_track(db_session, root.id, "Bohemian Rhapsody", "Queen")
    seed.genre = "Rock"
    match = _make_track(db_session, root.id, "Everlong", "Foo Fighters")
    match.genre = "Rock"
    _make_track(db_session, root.id, "Take Five", "Jazz Guy").genre = "Jazz"
    db_session.commit()
    _play(db_session, user, seed, times=1)

    with (
        patch("app.discovery.similar.listenbrainz.similar_artists", return_value=[]),
    ):
        result = refresh_discovery_playlist(db_session, user)

    assert result["similar_artists"] == ["Foo Fighters"]
    assert result["tracks_added"] == 1


def test_discovery_uses_all_embedded_genres_when_musicbrainz_is_missing(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()

    seed = _make_track(db_session, root.id, "Seed Song", "Local Artist")
    seed.genre = "Rock"
    match = _make_track(db_session, root.id, "Electronic Song", "Another Local Artist")
    # The useful shared genre is the second embedded value, not the legacy
    # primary genre column. Neither track needs an external metadata result.
    match.genre = "Pop"
    replace_item_genres(db_session, seed, ["Rock", "Electronic"], "embedded")
    replace_item_genres(db_session, match, ["Pop", "Electronic"], "embedded")
    db_session.commit()
    _play(db_session, user, seed, times=1)

    with (
        patch("app.discovery.similar.listenbrainz.similar_artists", return_value=[]),
    ):
        result = refresh_discovery_playlist(db_session, user)

    assert result["similar_artists"] == ["Another Local Artist"]
    assert result["tracks_added"] == 1


def test_discovery_seeds_from_recent_plays_not_stale_all_time_favorites(db_session):
    """An artist played heavily long ago but not since shouldn't keep
    steering recommendations - only the recently-played artist should seed
    Discovery, even though the old favorite has far more total plays."""
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()

    old_favorite = _make_track(db_session, root.id, "Old Song", "Old Favorite")
    old_favorite.genre = "Rock"
    recent = _make_track(db_session, root.id, "New Song", "Recent Pick")
    recent.genre = "Jazz"
    match = _make_track(db_session, root.id, "Related Song", "Related Artist")
    match.genre = "Jazz"
    db_session.commit()

    _play(
        db_session,
        user,
        old_favorite,
        times=20,
        played_at=utcnow() - timedelta(days=90),
    )
    _play(db_session, user, recent, times=1)

    with (
        patch("app.discovery.similar.listenbrainz.similar_artists", return_value=[]),
    ):
        result = refresh_discovery_playlist(db_session, user)

    assert result["seed_artists"] == ["Recent Pick"]
    assert result["similar_artists"] == ["Related Artist"]


def test_discovery_falls_back_to_all_time_when_nothing_played_recently(db_session):
    """A quiet stretch (no plays within the recency window) shouldn't leave
    Discovery empty - it should fall back to all-time top artists rather
    than seeding from nothing."""
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()

    old_favorite = _make_track(db_session, root.id, "Old Song", "Old Favorite")
    old_favorite.genre = "Rock"
    match = _make_track(db_session, root.id, "Related Song", "Related Artist")
    match.genre = "Rock"
    db_session.commit()

    _play(
        db_session,
        user,
        old_favorite,
        times=5,
        played_at=utcnow() - timedelta(days=90),
    )

    with (
        patch("app.discovery.similar.listenbrainz.similar_artists", return_value=[]),
    ):
        result = refresh_discovery_playlist(db_session, user)

    assert result["seed_artists"] == ["Old Favorite"]
    assert result["similar_artists"] == ["Related Artist"]


def test_no_play_history_yields_empty_discovery(db_session):
    user = db_session.query(User).one()
    result = refresh_discovery_playlist(db_session, user)
    assert result == {"seed_artists": [], "similar_artists": [], "tracks_added": 0}


def test_discovery_surfaces_owned_tracks_by_similar_artists(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()

    seed = _make_track(db_session, root.id, "Seed Song", "Queen")
    _play(db_session, user, seed, times=1)

    similar_track = _make_track(db_session, root.id, "Similar Song", "David Bowie")
    unrelated_track = _make_track(
        db_session, root.id, "Unrelated Song", "Some Other Band"
    )

    with (
        patch(
            "app.discovery.similar.listenbrainz.similar_artists",
            return_value=[
                "David Bowie"
            ],
        ),
    ):
        result = refresh_discovery_playlist(db_session, user)

    assert result["seed_artists"] == ["Queen"]
    assert result["similar_artists"] == ["David Bowie"]
    assert result["tracks_added"] == 1

    playlist = db_session.query(Playlist).filter(Playlist.name == "Discovery").one()
    assert playlist.is_auto_generated is True
    track_ids = [i.media_item_id for i in playlist.items]
    assert track_ids == [similar_track.id]
    assert unrelated_track.id not in track_ids


def _cache_row(db_session, artist):
    from app.models import ArtistMetadataCache

    return db_session.get(ArtistMetadataCache, artist)


def test_similar_artists_are_cached_not_repeated_per_call(db_session):
    from app.discovery.similar import _similar_artists_for

    with patch(
        "app.discovery.similar.listenbrainz.similar_artists", return_value=["David Bowie"]
    ) as mock_similar:
        assert _similar_artists_for(db_session, "Queen", "key") == {"David Bowie"}
        assert _similar_artists_for(db_session, "Queen", "key") == {"David Bowie"}
    assert mock_similar.call_count == 1
    mock_similar.assert_called_once_with("Queen")

    cache = _cache_row(db_session, "Queen")
    assert cache.similar_artists_json == ["David Bowie"]


def test_empty_similar_artists_are_cached_with_the_short_ttl(db_session):
    from app.discovery import ARTIST_CACHE_TTL, ARTIST_MISS_TTL
    from app.discovery.similar import _similar_artists_for

    with patch(
        "app.discovery.similar.listenbrainz.similar_artists", return_value=[]
    ) as mock_similar:
        _similar_artists_for(db_session, "Unknown", "key")
        _similar_artists_for(db_session, "Unknown", "key")
        assert mock_similar.call_count == 1

        cache = _cache_row(db_session, "Unknown")
        assert cache.similar_artists_json == []
        # Past the miss TTL but well inside the hit TTL: refetched.
        cache.fetched_at = utcnow() - ARTIST_MISS_TTL - timedelta(seconds=1)
        assert ARTIST_MISS_TTL < ARTIST_CACHE_TTL
        db_session.commit()
        _similar_artists_for(db_session, "Unknown", "key")
        assert mock_similar.call_count == 2


def test_stale_similar_artists_cache_is_refetched(db_session):
    from app.discovery import ARTIST_CACHE_TTL
    from app.discovery.similar import _similar_artists_for
    from app.models import ArtistMetadataCache

    db_session.add(
        ArtistMetadataCache(
            artist="Queen",
            similar_artists_json=["Old related artist"],
            fetched_at=utcnow() - ARTIST_CACHE_TTL - timedelta(seconds=1),
        )
    )
    db_session.commit()

    with patch(
        "app.discovery.similar.listenbrainz.similar_artists", return_value=["David Bowie"]
    ) as mock_similar:
        assert _similar_artists_for(db_session, "Queen", "key") == {"David Bowie"}
    assert mock_similar.call_count == 1
    cache = _cache_row(db_session, "Queen")
    assert cache.similar_artists_json == ["David Bowie"]


def test_failed_similar_artists_lookup_is_not_cached(db_session):
    from app.discovery.similar import _similar_artists_for

    with patch(
        "app.discovery.similar.listenbrainz.similar_artists", return_value=None
    ) as mock_similar:
        assert _similar_artists_for(db_session, "Queen", "key") == set()
        assert _similar_artists_for(db_session, "Queen", "key") == set()
    assert mock_similar.call_count == 2
    assert _cache_row(db_session, "Queen") is None


def test_failed_refetch_keeps_the_stale_cached_names(db_session):
    from app.discovery import ARTIST_CACHE_TTL
    from app.discovery.similar import _similar_artists_for
    from app.models import ArtistMetadataCache

    db_session.add(
        ArtistMetadataCache(
            artist="Queen",
            similar_artists_json=["Old related artist"],
            fetched_at=utcnow() - ARTIST_CACHE_TTL - timedelta(seconds=1),
        )
    )
    db_session.commit()
    with patch("app.discovery.similar.listenbrainz.similar_artists", return_value=None):
        assert _similar_artists_for(db_session, "Queen", "key") == {
            "Old related artist"
        }


def test_similar_artists_without_fetch_never_calls_out(db_session):
    from app.discovery import ARTIST_CACHE_TTL
    from app.discovery.similar import _similar_artists_for
    from app.models import ArtistMetadataCache

    db_session.add(
        ArtistMetadataCache(
            artist="Queen",
            similar_artists_json=["Cached"],
            fetched_at=utcnow() - ARTIST_CACHE_TTL - timedelta(days=5),
        )
    )
    db_session.commit()
    with patch("app.discovery.similar.listenbrainz.similar_artists") as mock_similar:
        # Even a stale cache is used as-is, and an uncached artist is empty.
        assert _similar_artists_for(db_session, "Queen", "key", allow_fetch=False) == {
            "Cached"
        }
        assert _similar_artists_for(db_session, "Other", "key", allow_fetch=False) == set()
    mock_similar.assert_not_called()


def test_similar_artists_with_the_feature_off_never_call_out(db_session):
    from app.discovery.similar import _similar_artists_for

    with patch("app.discovery.similar.listenbrainz.similar_artists") as mock_similar:
        assert _similar_artists_for(db_session, "Queen", None) == set()
    mock_similar.assert_not_called()
    assert _cache_row(db_session, "Queen") is None


def test_similar_artist_names_unions_every_seed(db_session):
    from app.discovery.similar import _similar_artist_names

    answers = {"Queen": ["David Bowie"], "Muse": ["Radiohead"]}
    with patch(
        "app.discovery.similar.listenbrainz.similar_artists",
        side_effect=lambda artist: answers[artist],
    ):
        names = _similar_artist_names(db_session, ["Queen", "Muse"], "key")
    assert names == {"David Bowie", "Radiohead"}


def test_online_allowed_reads_the_users_privacy_switch(db_session):
    from app.discovery.similar import _online_allowed

    user = db_session.query(User).one()
    assert _online_allowed(user) is True
    user.online_recommendations = False
    db_session.commit()
    assert _online_allowed(user) is False


def test_discovery_with_listenbrainz_turned_off_makes_no_fetch(db_session):
    user = db_session.query(User).one()
    user.online_recommendations = False
    db_session.commit()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    seed = _make_track(db_session, root.id, "Seed Song", "Queen")
    seed.genre = "Rock"
    _make_track(db_session, root.id, "Match", "Foo Fighters").genre = "Rock"
    db_session.commit()
    _play(db_session, user, seed, times=1)

    with patch("app.discovery.similar.listenbrainz.similar_artists") as mock_similar:
        result = refresh_discovery_playlist(db_session, user)
    mock_similar.assert_not_called()
    assert result["similar_artists"] == ["Foo Fighters"]



def test_generate_playlist_from_track_uses_genre_overlap_without_live_fetch(db_session):
    """ "Generate playlist from this song" can be triggered on any track in
    the library, not just Discovery's handful of recurring top-played
    "seed" artists - so unlike Discovery, it must never block on a live
    MusicBrainz/ListenBrainz round trip by default (that round trip,
    including MusicBrainz's blocking ~1 req/sec throttle, is slow).
    Genre-tag overlap alone already produces a real match."""
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()

    seed = _make_track(db_session, root.id, "Bohemian Rhapsody", "Queen")
    seed.genre = "Rock"
    match = _make_track(db_session, root.id, "Everlong", "Foo Fighters")
    match.genre = "Rock"
    db_session.commit()

    with (
        patch("app.discovery.similar.listenbrainz.similar_artists") as mock_similar,
    ):
        result = generate_playlist_from_track(db_session, user, seed.id)

    mock_similar.assert_not_called()
    assert result is not None
    assert result["track_count"] == 2


def test_discovery_excludes_tracks_worn_out_lately(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()

    seed = _make_track(db_session, root.id, "Seed Song", "Queen")
    _play(db_session, user, seed, times=1)

    already_worn_out = _make_track(db_session, root.id, "Overplayed", "David Bowie")
    _play(db_session, user, already_worn_out, times=5)  # today: fatigued

    with (
        patch(
            "app.discovery.similar.listenbrainz.similar_artists",
            return_value=[
                "David Bowie"
            ],
        ),
    ):
        result = refresh_discovery_playlist(db_session, user)

    assert result["tracks_added"] == 0


def test_discovery_respects_library_scoping(session_factory, db_session):
    shared_root = LibraryRoot(path="/shared", display_name="Shared", is_shared=True)
    private_root = LibraryRoot(path="/private", display_name="Private", is_shared=False)
    db_session.add_all([shared_root, private_root])
    db_session.commit()

    plain = User(username="plain", password_hash=hash_password("x"))
    db_session.add(plain)
    db_session.commit()

    seed = _make_track(db_session, shared_root.id, "Seed Song", "Queen")
    _play(db_session, plain, seed, times=1)
    _make_track(db_session, private_root.id, "Hidden Similar", "David Bowie")

    with (
        patch(
            "app.discovery.similar.listenbrainz.similar_artists",
            return_value=[
                "David Bowie"
            ],
        ),
    ):
        result = refresh_discovery_playlist(db_session, plain)

    # plain isn't granted the private root, so the only similar-artist match is invisible to them
    assert result["tracks_added"] == 0


def test_refresh_is_idempotent_not_duplicating_playlist(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    seed = _make_track(db_session, root.id, "Seed Song", "Queen")
    _play(db_session, user, seed, times=1)
    _make_track(db_session, root.id, "Similar Song", "David Bowie")

    with (
        patch(
            "app.discovery.similar.listenbrainz.similar_artists",
            return_value=[
                "David Bowie"
            ],
        ),
    ):
        refresh_discovery_playlist(db_session, user)
        refresh_discovery_playlist(db_session, user)

    playlists = db_session.query(Playlist).filter(Playlist.name == "Discovery").all()
    assert len(playlists) == 1
    assert len(playlists[0].items) == 1


def test_explore_no_play_history_yields_empty(db_session):
    user = db_session.query(User).one()
    state = DiscoveryState(
        user_id=user.id,
        explore_results_json=[{"artist": "Old preview"}],
    )
    db_session.add(state)
    db_session.commit()

    result = explore_new_artists(db_session, user)

    assert result == {"seed_artists": [], "results": []}
    assert get_discovery_state(db_session, user)["explore_results"] == []


def test_explore_endpoint_is_wired_up(client):
    resp = client.post("/api/library/discovery/explore")
    assert resp.status_code == 200
    assert resp.json() == {"seed_artists": [], "results": []}


def test_explore_skips_owned_artists_and_previews_the_rest(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()

    seed = _make_track(db_session, root.id, "Seed Song", "Queen")
    _play(db_session, user, seed, times=1)
    # Already owned - should be excluded even though it's "similar".
    _make_track(db_session, root.id, "Owned Similar", "David Bowie")

    preview = {
        "artist": "Freddie's Friends",
        "title": "Some Track",
        "artwork_url": "https://example.com/art.jpg",
        "preview_url": "https://example.com/preview.mp3",
        "view_url": "https://deezer.com/artist/freddies-friends",
    }
    with (
        patch(
            "app.discovery.similar.listenbrainz.similar_artists",
            return_value=[
                "David Bowie",
                "Freddie's Friends",
            ],
        ),
        patch(
            "app.discovery.explore.deezer.search_artist_track", return_value=preview
        ) as mocked_deezer,
    ):
        result = explore_new_artists(db_session, user)

    assert result["seed_artists"] == ["Queen"]
    assert result["results"] == [preview]
    mocked_deezer.assert_called_once_with("Freddie's Friends")


def test_explore_returns_nothing_when_all_similar_artists_are_owned(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()

    seed = _make_track(db_session, root.id, "Seed Song", "Queen")
    _play(db_session, user, seed, times=1)
    _make_track(db_session, root.id, "Owned Similar", "David Bowie")

    with (
        patch(
            "app.discovery.similar.listenbrainz.similar_artists",
            return_value=[
                "David Bowie"
            ],
        ),
        patch("app.discovery.explore.deezer.search_artist_track") as mocked_deezer,
    ):
        result = explore_new_artists(db_session, user)

    assert result["results"] == []
    mocked_deezer.assert_not_called()


def test_explore_refresh_prefers_new_artists_and_fills_past_failed_lookups(
    db_session, monkeypatch
):
    user = db_session.query(User).one()
    previous = [{"artist": "Artist 00", "preview_url": "old"}]
    db_session.add(
        DiscoveryState(
            user_id=user.id,
            explore_results_json=previous,
        )
    )
    db_session.commit()
    names = {f"Artist {index:02d}" for index in range(30)}
    calls = []

    def search(name):
        calls.append(name)
        # A number of unavailable previews should not leave the rail short
        # when later artists in the candidate pool have usable tracks.
        if len(calls) <= 15:
            return None
        return {"artist": name, "title": "Track", "preview_url": f"preview:{name}"}

    monkeypatch.setattr("app.discovery.explore.random.shuffle", lambda values: None)
    with (
        patch("app.discovery.explore._top_played_artists", return_value=["Seed"]),
        patch("app.discovery.explore._similar_artist_names", return_value=names),
        patch("app.discovery.explore.deezer.search_artist_track", side_effect=search),
        patch("app.discovery.explore.time.sleep"),
    ):
        result = explore_new_artists(db_session, user)

    assert len(result["results"]) == MAX_EXPLORE_RESULTS
    assert all(track["artist"] != "Artist 00" for track in result["results"])
    # 15 unavailable-artist lookups, then one per result filled past them.
    assert len(calls) == 15 + MAX_EXPLORE_RESULTS


def test_explore_refresh_clears_stale_results_when_no_similar_artists(db_session):
    user = db_session.query(User).one()
    state = DiscoveryState(
        user_id=user.id,
        explore_results_json=[{"artist": "Old preview"}],
    )
    db_session.add(state)
    db_session.commit()

    with (
        patch("app.discovery.explore._top_played_artists", return_value=["Seed"]),
        patch("app.discovery.explore._similar_artist_names", return_value=set()),
    ):
        result = explore_new_artists(db_session, user)

    assert result == {"seed_artists": ["Seed"], "results": []}
    assert get_discovery_state(db_session, user)["explore_results"] == []


def test_explore_only_returns_distinct_artists_and_preview_urls(db_session, monkeypatch):
    user = db_session.query(User).one()
    names = {f"Artist {index}" for index in range(6)}
    tracks = [
        {"artist": "Artist A", "preview_url": "preview-a"},
        {"artist": "Artist A", "preview_url": "preview-b"},
        {"artist": "Artist B", "preview_url": "preview-a"},
        {"artist": None, "preview_url": "preview-b"},
        {"artist": "Artist C", "preview_url": ""},
        None,
    ]
    monkeypatch.setattr("app.discovery.explore.random.shuffle", lambda values: None)
    with (
        patch("app.discovery.explore._top_played_artists", return_value=["Seed"]),
        patch("app.discovery.explore._similar_artist_names", return_value=names),
        patch("app.discovery.explore.deezer.search_artist_track", side_effect=tracks),
        patch("app.discovery.explore.time.sleep"),
    ):
        result = explore_new_artists(db_session, user)

    assert [track["preview_url"] for track in result["results"]] == [
        "preview-a",
        "preview-b",
        "",
    ]


def test_refresh_and_explore_persist_state_for_later_reload(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()

    seed = _make_track(db_session, root.id, "Seed Song", "Queen")
    _play(db_session, user, seed, times=1)
    _make_track(db_session, root.id, "Similar Song", "David Bowie")

    preview = {
        "artist": "Freddie's Friends",
        "title": "Some Track",
        "artwork_url": None,
        "preview_url": "https://cdn.example/preview.mp3",
        "view_url": None,
    }

    with (
        patch(
            "app.discovery.similar.listenbrainz.similar_artists",
            return_value=[
                "David Bowie",
                "Freddie's Friends",
            ],
        ),
        patch("app.discovery.explore.deezer.search_artist_track", return_value=preview),
    ):
        refresh_discovery_playlist(db_session, user)
        explore_new_artists(db_session, user)

    state = get_discovery_state(db_session, user)
    assert state["discovery_seed_artists"] == ["Queen"]
    # "Freddie's Friends" isn't owned, so it's excluded here even though
    # ListenBrainz named it too - Discovery only ever recommends what's
    # actually in the library (unlike Explore below, which finds new
    # artists on purpose).
    assert state["discovery_similar_artists"] == ["David Bowie"]
    assert state["discovery_tracks_added"] == 1
    assert state["discovery_updated_at"] is not None
    assert state["explore_seed_artists"] == ["Queen"]
    assert state["explore_results"] == [preview]
    assert state["explore_updated_at"] is not None


def test_discovery_state_defaults_are_empty_before_any_run(db_session):
    user = db_session.query(User).one()
    state = get_discovery_state(db_session, user)
    assert state == {
        "discovery_seed_artists": [],
        "discovery_similar_artists": [],
        "discovery_tracks_added": 0,
        "discovery_updated_at": None,
        "explore_seed_artists": [],
        "explore_results": [],
        "explore_pending": False,
        "explore_updated_at": None,
    }


def test_discovery_settings_default_and_update(db_session):
    user = db_session.query(User).one()
    defaults = get_discovery_settings(db_session, user)
    assert defaults == {"auto_refresh_enabled": True, "auto_refresh_hour": 6}

    updated = update_discovery_settings(db_session, user, enabled=False, hour=13)
    assert updated == {"auto_refresh_enabled": False, "auto_refresh_hour": 13}
    assert get_discovery_settings(db_session, user) == updated


def test_run_due_auto_refreshes_runs_once_per_day_at_configured_hour(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    seed = _make_track(db_session, root.id, "Seed Song", "Queen")
    _play(db_session, user, seed, times=1)

    update_discovery_settings(db_session, user, enabled=True, hour=6)

    with (
        patch("app.discovery.similar.listenbrainz.similar_artists", return_value=[]),
    ):
        run_due_auto_refreshes(db_session, current_hour=6)

        state = db_session.get(DiscoveryState, user.id)
        assert state.last_auto_refresh_date == date.today().isoformat()
        state.discovery_tracks_added = (
            999  # sentinel to prove a 2nd run this "day" is skipped
        )
        db_session.commit()

        run_due_auto_refreshes(db_session, current_hour=6)
        db_session.refresh(state)
        assert state.discovery_tracks_added == 999


def test_run_due_auto_refreshes_skips_disabled_or_wrong_hour(db_session):
    user = db_session.query(User).one()
    update_discovery_settings(db_session, user, enabled=True, hour=6)

    run_due_auto_refreshes(db_session, current_hour=5)  # not the configured hour

    state = db_session.get(DiscoveryState, user.id)
    assert state.last_auto_refresh_date is None


def test_scheduled_refresh_failure_for_one_user_does_not_skip_the_next(db_session):
    first = db_session.query(User).one()
    second = User(username="second", password_hash=hash_password("password"))
    db_session.add(second)
    db_session.commit()
    update_discovery_settings(db_session, first, enabled=True, hour=6)
    update_discovery_settings(db_session, second, enabled=True, hour=6)
    refreshed: list[int] = []

    def refresh(_db, user):
        refreshed.append(user.id)
        if user.id == first.id:
            raise RuntimeError("provider failed")

    with (
        patch("app.discovery.scheduler.refresh_discovery_playlist", side_effect=refresh),
        patch("app.discovery.scheduler.explore_new_artists"),
        patch("app.discovery.scheduler.refresh_all_genre_mixes"),
    ):
        run_due_auto_refreshes(db_session, current_hour=6)

    assert refreshed == [first.id, second.id]
    assert db_session.get(DiscoveryState, first.id).last_auto_refresh_date is None
    assert db_session.get(DiscoveryState, second.id).last_auto_refresh_date == (
        date.today().isoformat()
    )


def test_discovery_state_endpoint_returns_defaults(client):
    resp = client.get("/api/library/discovery/state")
    assert resp.status_code == 200
    body = resp.json()
    assert body["discovery_seed_artists"] == []
    assert body["explore_results"] == []


def test_discovery_settings_endpoints(client):
    resp = client.get("/api/library/discovery/settings")
    assert resp.status_code == 200
    assert resp.json() == {"auto_refresh_enabled": True, "auto_refresh_hour": 6}

    resp = client.put(
        "/api/library/discovery/settings",
        json={"auto_refresh_enabled": False, "auto_refresh_hour": 14},
    )
    assert resp.status_code == 200
    assert resp.json() == {"auto_refresh_enabled": False, "auto_refresh_hour": 14}

    resp = client.get("/api/library/discovery/settings")
    assert resp.json() == {"auto_refresh_enabled": False, "auto_refresh_hour": 14}


def test_discovery_settings_rejects_bad_hour(client):
    resp = client.put(
        "/api/library/discovery/settings",
        json={"auto_refresh_enabled": True, "auto_refresh_hour": 24},
    )
    assert resp.status_code == 422


def test_generate_playlist_from_track_seeds_with_that_track_first(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()

    seed = _make_track(db_session, root.id, "Seed Song", "Queen")
    seed.genre = "Rock"
    similar_track = _make_track(db_session, root.id, "Similar Song", "David Bowie")
    similar_track.genre = "Rock"
    db_session.commit()

    with (
        patch("app.discovery.similar.listenbrainz.similar_artists") as mock_similar,
    ):
        result = generate_playlist_from_track(db_session, user, seed.id)

    mock_similar.assert_not_called()
    assert result["name"] == "Song Mix: Seed Song"
    assert result["track_count"] == 2

    playlist = db_session.get(Playlist, result["playlist_id"])
    assert playlist.auto_kind == "generated"
    assert playlist.is_auto_generated is True
    track_ids = [i.media_item_id for i in playlist.items]
    assert track_ids[0] == seed.id
    assert similar_track.id in track_ids


def test_generate_playlist_reuses_and_overwrites_same_slot(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    seed1 = _make_track(db_session, root.id, "First Seed", "Queen")
    seed1.genre = "Rock"
    match1 = _make_track(db_session, root.id, "Match One", "Foo Fighters")
    match1.genre = "Rock"
    seed2 = _make_track(db_session, root.id, "Second Seed", "Bowie")
    seed2.genre = "Jazz"
    match2 = _make_track(db_session, root.id, "Match Two", "Miles Davis")
    match2.genre = "Jazz"
    db_session.commit()

    with (
        patch("app.discovery.similar.listenbrainz.similar_artists", return_value=[]),
    ):
        first = generate_playlist_from_track(db_session, user, seed1.id)
        second = generate_playlist_from_track(db_session, user, seed2.id)

    assert first["playlist_id"] == second["playlist_id"]
    playlists = (
        db_session.query(Playlist)
        .filter(Playlist.user_id == user.id, Playlist.auto_kind == "generated")
        .all()
    )
    assert len(playlists) == 1
    assert playlists[0].name == "Song Mix: Second Seed"
    assert [i.media_item_id for i in playlists[0].items] == [seed2.id, match2.id]


def test_generate_playlist_rejects_inaccessible_track(db_session):
    private_root = LibraryRoot(path="/private", display_name="Private", is_shared=False)
    db_session.add(private_root)
    db_session.commit()
    other = User(username="plain", password_hash=hash_password("x"))
    db_session.add(other)
    db_session.commit()
    seed = _make_track(db_session, private_root.id, "Hidden", "Nobody")

    result = generate_playlist_from_track(db_session, other, seed.id)
    assert result is None


def test_save_auto_playlist_as_new_copies_without_touching_original(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    seed = _make_track(db_session, root.id, "Seed Song", "Queen")
    _play(db_session, user, seed, times=1)
    similar = _make_track(db_session, root.id, "Similar Song", "David Bowie")

    with (
        patch(
            "app.discovery.similar.listenbrainz.similar_artists",
            return_value=[
                "David Bowie"
            ],
        ),
    ):
        refresh_discovery_playlist(db_session, user)

    discovery = (
        db_session.query(Playlist).filter(Playlist.auto_kind == "discovery").one()
    )
    saved = save_auto_playlist_as_new(db_session, user, discovery.id, "My Mix")

    assert saved.auto_kind is None
    assert saved.is_auto_generated is False
    assert saved.name == "My Mix"
    assert [i.media_item_id for i in saved.items] == [similar.id]

    # Refreshing Discovery again shouldn't touch the saved copy.
    with (
        patch("app.discovery.similar.listenbrainz.similar_artists", return_value=[]),
    ):
        refresh_discovery_playlist(db_session, user)
    db_session.refresh(saved)
    assert [i.media_item_id for i in saved.items] == [similar.id]


def test_save_auto_playlist_rejects_non_auto_playlist(db_session):
    user = db_session.query(User).one()
    ordinary = Playlist(user_id=user.id, name="My playlist")
    db_session.add(ordinary)
    db_session.commit()

    result = save_auto_playlist_as_new(db_session, user, ordinary.id, None)
    assert result is None


def test_generate_playlist_endpoint_is_wired_up(client, db_session):
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    seed = _make_track(db_session, root.id, "Seed Song", "Queen")
    seed.genre = "Rock"
    _make_track(db_session, root.id, "Match Song", "Foo Fighters").genre = "Rock"
    db_session.commit()

    with (
        patch("app.discovery.similar.listenbrainz.similar_artists", return_value=[]),
    ):
        resp = client.post(
            "/api/library/discovery/generate", json={"media_item_id": seed.id}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["track_count"] == 2
    assert body["name"] == "Song Mix: Seed Song"


def test_generate_playlist_endpoint_422s_when_not_enough_matches(client, db_session):
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    seed = _make_track(db_session, root.id, "Seed Song", "Totally Unknown Artist")

    resp = client.post(
        "/api/library/discovery/generate", json={"media_item_id": seed.id}
    )
    assert resp.status_code == 422


def test_generate_collection_playlist_endpoint(client, db_session):
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    _make_track(db_session, root.id, "Seed Song", "Queen").genre = "Rock"
    _make_track(db_session, root.id, "Match Song", "Foo Fighters").genre = "Rock"
    _make_track(db_session, root.id, "Match Two", "Foo Fighters").genre = "Rock"
    db_session.commit()

    resp = client.post(
        "/api/library/discovery/generate-collection",
        json={"label": "Queen", "artist": "Queen"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Artist Mix: Queen"
    assert body["track_count"] == 2

    missing = client.post(
        "/api/library/discovery/generate-collection",
        json={"label": "Nobody", "artist": "Nobody"},
    )
    assert missing.status_code == 404


def _make_genre_tracks(db_session, root_id, genre, count) -> list[MediaItem]:
    items = []
    for i in range(count):
        item = MediaItem(
            library_root_id=root_id,
            kind=MediaKind.track,
            relative_path=f"{genre}-{i}.mp3",
            title=f"{genre} track {i}",
            genre=genre,
        )
        db_session.add(item)
        items.append(item)
    db_session.commit()
    return items


def test_refresh_all_genre_mixes_creates_real_playlists_for_qualifying_genres(
    db_session,
):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    _make_genre_tracks(db_session, root.id, "Rock", 20)
    _make_genre_tracks(
        db_session, root.id, "Jazz", 3
    )  # below the minimum - shouldn't get a mix

    mixes = refresh_all_genre_mixes(db_session, user)
    assert [m.mix_genre for m in mixes] == ["Rock"]
    rock = mixes[0]
    assert rock.auto_kind == "mix"
    assert rock.is_auto_generated is True
    assert 1 <= len(rock.items) <= 20

    # A real, save-able playlist - not computed on the fly.
    assert list_genre_mix_playlists(db_session, user) == [rock]
    saved = save_auto_playlist_as_new(db_session, user, rock.id, "My Rock Mix")
    assert saved is not None
    assert saved.auto_kind is None


def test_automatic_genre_mix_includes_all_embedded_genre_relationships(db_session, monkeypatch):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/embedded-genres", display_name="Embedded genres")
    db_session.add(root)
    db_session.commit()
    tracks = [
        MediaItem(
            library_root_id=root.id,
            kind=MediaKind.track,
            relative_path=f"track-{index}.mp3",
            title=f"Track {index}",
        )
        for index in range(15)
    ]
    genre = Genre(name="Alternative Rock", normalized_name="alternative rock")
    db_session.add_all([*tracks, genre])
    db_session.flush()
    for track in tracks:
        db_session.add(
            MediaItemGenre(media_item_id=track.id, genre_id=genre.id, source="embedded")
        )
    db_session.commit()
    monkeypatch.setattr("app.discovery.pools.random.choices", lambda options, weights, k: [options[0]])

    mixes = refresh_all_genre_mixes(db_session, user)

    assert [mix.mix_genre for mix in mixes] == ["Alternative Rock"]
    assert len(mixes[0].items) == 15


def test_automatic_mix_selector_weights_favor_large_catalogues_but_keep_rare_options(
    monkeypatch,
):
    observed_weights = []

    def choose_last(options, weights, k):
        observed_weights.append(weights)
        return [options[-1]]

    monkeypatch.setattr("app.discovery.pools.random.choices", choose_last)
    options = [("genre", "Common", 10_000), ("tag", "Rare", 100)]
    selected = _weighted_mix_selection(options, 2)

    assert selected == [options[-1], options[0]]
    # Chance is proportional to track count: 10,000 vs 100 is 100x.
    assert observed_weights[0][0] == pytest.approx(observed_weights[0][1] * 100)
    assert observed_weights[1] == [options[0][2]]


def test_refresh_all_mixes_includes_tags_and_caps_at_ten(db_session, monkeypatch):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/weighted-mixes", display_name="Weighted mixes")
    db_session.add(root)
    db_session.commit()

    for index in range(MAX_GENRE_MIXES + 2):
        tracks = _make_genre_tracks(db_session, root.id, f"Genre {index}", 15)
        tag = Tag(name=f"Tag {index}")
        db_session.add(tag)
        db_session.commit()
        for track in tracks:
            track.tags.append(tag)
        db_session.commit()

    # Force tag options to be selected when available so the test confirms
    # they participate in normal refreshes as well as specified mixes.
    def choose_tag(options, weights, k):
        return [next(option for option in options if option[0] == "tag")]

    monkeypatch.setattr("app.discovery.pools.random.choices", choose_tag)
    mixes = refresh_all_genre_mixes(db_session, user)

    assert len(mixes) == MAX_GENRE_MIXES
    assert any(mix.mix_genre.startswith("tag:") for mix in mixes)
    assert all(len(mix.items) == 15 for mix in mixes)


def test_ignored_folder_is_excluded_from_generated_mixes_and_song_mix(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/ignored-discovery", display_name="Ignored discovery")
    db_session.add(root)
    db_session.commit()
    included = _make_genre_tracks(db_session, root.id, "Rock", 20)
    custom = [
        MediaItem(
            library_root_id=root.id,
            kind=MediaKind.track,
            relative_path=f"Custom/{index}.mp3",
            title=f"Custom song {index}",
            artist="Uncatalogued artist",
            genre="Custom genre",
        )
        for index in range(20)
    ]
    ignored_seed = MediaItem(
        library_root_id=root.id,
        kind=MediaKind.track,
        relative_path="Custom/seed.mp3",
        title="Custom seed",
        artist="Local artist",
        genre="Rock",
    )
    local_only_tag = Tag(name="Local-only tag")
    db_session.add_all([*custom, ignored_seed, local_only_tag])
    db_session.commit()
    for item in custom:
        item.tags.append(local_only_tag)
    db_session.add(IgnoredLibraryFolder(library_root_id=root.id, relative_path="Custom"))
    db_session.commit()

    _play(db_session, user, ignored_seed)
    assert refresh_discovery_playlist(db_session, user)["seed_artists"] == []

    mixes = refresh_all_genre_mixes(db_session, user)
    assert [mix.mix_genre for mix in mixes] == ["Rock"]
    assert {item.media_item.id for item in mixes[0].items} <= {item.id for item in included}
    with pytest.raises(ValueError, match="no accessible tracks"):
        add_specified_mix_playlist(db_session, user, "tag", "Local-only tag")
    assert generate_playlist_from_track(db_session, user, ignored_seed.id) is None


def test_genre_mix_balances_artists_and_specified_mix_is_temporary(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    dominant = _make_genre_tracks(db_session, root.id, "Balanced", 60)
    for item in dominant:
        item.artist = "Dominant"
    other = _make_genre_tracks(db_session, root.id, "BalancedOther", 10)
    for item in other:
        item.artist = "Other"
        item.genre = "Balanced"
    tag = Tag(name="Focus")
    db_session.add(tag)
    db_session.commit()
    for item in other:
        item.tags.append(tag)
    db_session.commit()

    mixes = refresh_all_genre_mixes(db_session, user)
    artists = {item.media_item.artist for item in mixes[0].items}
    assert artists == {"Dominant", "Other"}
    assert sum(item.media_item.artist == "Dominant" for item in mixes[0].items) == 30
    assert sum(item.media_item.artist == "Other" for item in mixes[0].items) == 10

    specified = add_specified_mix_playlist(db_session, user, "tag", "Focus")
    assert specified.mix_genre == "specified:tag:Focus"
    assert all(item.media_item.artist == "Other" for item in specified.items)

    # Re-selecting the same option replaces its temporary slot, and the tag
    # criterion is also reused when asking for more tracks.
    assert add_more_tracks_to_auto_playlist(db_session, user, specified) == 0
    replacement = add_specified_mix_playlist(db_session, user, "tag", "Focus")
    assert replacement.id == specified.id
    with pytest.raises(ValueError):
        add_specified_mix_playlist(db_session, user, "mood", "Focus")
    with pytest.raises(ValueError):
        add_specified_mix_playlist(db_session, user, "genre", "Missing")

    refresh_all_genre_mixes(db_session, user)
    assert db_session.get(Playlist, specified.id) is None


def test_add_more_tracks_reuses_each_auto_playlist_criteria(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()

    rock = _make_track(db_session, root.id, "Rock one", "Rockers")
    rock.genre = "Rock"
    rock_extra = _make_track(db_session, root.id, "Rock two", "Rockers")
    rock_extra.genre = "Rock"
    tag = Tag(name="Tag mix")
    db_session.add(tag)
    tag.media_items.extend([rock, rock_extra])
    _make_track(db_session, root.id, "Discovery", "Similar")
    generated_track = _make_track(db_session, root.id, "Generated", "Generated")
    db_session.commit()

    mix = Playlist(
        user_id=user.id, name="Rock Mix", is_auto_generated=True,
        auto_kind="mix", mix_genre="Rock",
    )
    tag_mix = Playlist(
        user_id=user.id, name="Tag Mix", is_auto_generated=True,
        auto_kind="mix", mix_genre="tag:Tag mix",
    )
    discovery = Playlist(
        user_id=user.id, name="Discovery", is_auto_generated=True,
        auto_kind="discovery",
    )
    generated = Playlist(
        user_id=user.id, name="Generated", is_auto_generated=True,
        auto_kind="generated",
    )
    ordinary = Playlist(user_id=user.id, name="Ordinary")
    db_session.add_all([mix, tag_mix, discovery, generated, ordinary])
    db_session.commit()
    db_session.add_all([
        PlaylistItem(playlist_id=mix.id, media_item_id=rock.id, position=0),
        PlaylistItem(playlist_id=tag_mix.id, media_item_id=rock.id, position=0),
        PlaylistItem(playlist_id=discovery.id, media_item_id=rock.id, position=0),
        PlaylistItem(playlist_id=generated.id, media_item_id=generated_track.id, position=0),
    ])
    state = DiscoveryState(user_id=user.id, discovery_similar_artists_json=["Similar"])
    db_session.add(state)
    db_session.commit()

    with patch("app.discovery.song_mix.similar_tracks_for_seed", return_value=[generated_track.id, rock_extra.id]):
        assert add_more_tracks_to_auto_playlist(db_session, user, mix) == 1
        assert add_more_tracks_to_auto_playlist(db_session, user, tag_mix) == 1
        assert add_more_tracks_to_auto_playlist(db_session, user, discovery) == 1
        assert add_more_tracks_to_auto_playlist(db_session, user, generated) == 1
    assert add_more_tracks_to_auto_playlist(db_session, user, ordinary) == 0


def test_refresh_all_genre_mixes_drops_genres_no_longer_featured(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    _make_genre_tracks(db_session, root.id, "Rock", 20)
    refresh_all_genre_mixes(db_session, user)
    assert {m.mix_genre for m in list_genre_mix_playlists(db_session, user)} == {"Rock"}

    # Rock no longer qualifies (all its tracks removed) - a fresh refresh
    # should drop its mix playlist entirely rather than leaving it stale.
    rock_ids = [i for (i,) in db_session.query(MediaItem.id).filter(MediaItem.genre == "Rock")]
    delete_media_records(db_session, rock_ids)
    db_session.commit()
    refresh_all_genre_mixes(db_session, user)
    assert list_genre_mix_playlists(db_session, user) == []


def test_refreshing_mixes_preserves_art_for_removed_mix_history(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    legacy_tracks = _make_genre_tracks(db_session, root.id, "ZLegacy", 15)
    for number in range(MAX_GENRE_MIXES + 2):
        _make_genre_tracks(db_session, root.id, f"Genre {number}", 15)

    stale_mix = Playlist(
        user_id=user.id,
        name="ZLegacy Mix",
        is_auto_generated=True,
        auto_kind="mix",
        mix_genre="ZLegacy",
    )
    db_session.add(stale_mix)
    db_session.commit()
    db_session.add(
        PlaylistItem(
            playlist_id=stale_mix.id,
            media_item_id=legacy_tracks[0].id,
            position=0,
        )
    )
    db_session.add(
        PlaySourceHistory(
            user_id=user.id,
            kind="playlist",
            label=stale_mix.name,
            payload_json={"playlist_id": stale_mix.id},
        )
    )
    db_session.commit()

    def skip_legacy(options, weights, k):
        return [next(option for option in options if option[1] != "ZLegacy")]

    with patch("app.discovery.pools.random.choices", side_effect=skip_legacy):
        refresh_all_genre_mixes(db_session, user)

    history = db_session.query(PlaySourceHistory).one()
    assert db_session.get(Playlist, stale_mix.id) is None
    assert history.art_media_item_id == legacy_tracks[0].id


def test_refresh_all_genre_mixes_reuses_same_playlist_across_refreshes(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    _make_genre_tracks(db_session, root.id, "Rock", 20)

    first = refresh_all_genre_mixes(db_session, user)[0]
    second = refresh_all_genre_mixes(db_session, user)[0]
    assert first.id == second.id  # same slot, overwritten - not a new row each time


def test_mixes_endpoints_are_wired_up(client, db_session):
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    _make_genre_tracks(db_session, root.id, "Rock", 20)
    tag = Tag(name="Workout")
    db_session.add(tag)
    db_session.commit()
    for item in db_session.query(MediaItem).filter(MediaItem.genre == "Rock"):
        item.tags.append(tag)
    db_session.commit()

    resp = client.get("/api/library/mixes")
    assert resp.status_code == 200
    assert resp.json() == []  # nothing generated yet

    resp = client.post("/api/library/mixes/refresh")
    assert resp.status_code == 200
    body = resp.json()
    assert {mix["genre"] for mix in body} == {"Rock", "Workout"}
    assert {mix["name"] for mix in body} == {"Rock Mix", "Workout Mix"}

    resp = client.get("/api/library/mixes")
    assert resp.status_code == 200
    assert resp.json() == body


def test_run_due_auto_refreshes_also_refreshes_genre_mixes(db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    _make_genre_tracks(db_session, root.id, "Rock", 20)
    update_discovery_settings(db_session, user, enabled=True, hour=6)

    with (
        patch("app.discovery.similar.listenbrainz.similar_artists", return_value=[]),
    ):
        run_due_auto_refreshes(db_session, current_hour=6)

    assert {m.mix_genre for m in list_genre_mix_playlists(db_session, user)} == {"Rock"}


# --- dislikes and likes (app.preferences) ---------------------------------


def _dislike_library(db_session, user):
    """A Rock library where some tracks are disliked at each level, plus the
    exceptions the rule keeps: a liked song by a disliked artist and a liked
    album by a disliked artist. Returns (excluded ids, kept-despite ids,
    tracks by title)."""
    from app.models import CollectionPreference, Favorite, TrackDislike

    root = LibraryRoot(path="/dislikes", display_name="Dislikes")
    db_session.add(root)
    db_session.commit()
    spec = (
        [("Good", "Good LP", f"Good {i}") for i in range(20)]
        + [("Bad", "Bad LP", f"Bad {i}") for i in range(4)]
        + [("Bad", "Bad LP", "Bad but liked")]
        + [("Bad", "Rescued LP", f"Rescued {i}") for i in range(2)]
        + [("Mixed", "Hated LP", f"Hated {i}") for i in range(5)]
        + [("Mixed", "OK LP", f"OK {i}") for i in range(5)]
        + [("Solo", "Solo LP", f"Solo {i}") for i in range(5)]
        + [("Seed", "Seed LP", "Seed song")]
    )
    tracks = {}
    for artist, album, title in spec:
        item = MediaItem(
            library_root_id=root.id,
            kind=MediaKind.track,
            relative_path=f"{title}.mp3",
            title=title,
            artist=artist,
            album=album,
            genre="Rock",
        )
        db_session.add(item)
        tracks[title] = item
    db_session.commit()
    db_session.add_all(
        [
            CollectionPreference(user_id=user.id, kind="artist", artist="bad", state="disliked"),
            CollectionPreference(
                user_id=user.id, kind="album", artist="Bad", album="Rescued LP", state="liked"
            ),
            CollectionPreference(
                user_id=user.id, kind="album", artist="Mixed", album="hated lp", state="disliked"
            ),
            Favorite(user_id=user.id, media_item_id=tracks["Bad but liked"].id),
            TrackDislike(user_id=user.id, media_item_id=tracks["Solo 0"].id),
        ]
    )
    db_session.commit()
    excluded = {
        tracks[t].id
        for t in [*(f"Bad {i}" for i in range(4)), *(f"Hated {i}" for i in range(5)), "Solo 0"]
    }
    kept = {tracks[t].id for t in ["Bad but liked", "Rescued 0", "Rescued 1"]}
    return excluded, kept, tracks


def test_dislikes_are_left_out_of_new_and_specified_mixes(db_session):
    user = db_session.query(User).one()
    excluded, kept, _tracks = _dislike_library(db_session, user)

    # 31 eligible tracks, under the 40-track limit, so every one is picked.
    mix = refresh_all_genre_mixes(db_session, user)[0]
    ids = {item.media_item_id for item in mix.items}
    assert ids.isdisjoint(excluded)
    assert kept <= ids

    specified = add_specified_mix_playlist(db_session, user, "genre", "Rock")
    ids = {item.media_item_id for item in specified.items}
    assert ids.isdisjoint(excluded)
    assert kept <= ids


def test_a_genre_only_qualifies_for_a_mix_on_tracks_that_are_not_disliked(db_session):
    from app.models import CollectionPreference

    user = db_session.query(User).one()
    root = LibraryRoot(path="/hated-genre", display_name="Hated genre")
    db_session.add(root)
    db_session.commit()
    _make_genre_tracks(db_session, root.id, "Rock", 20)
    for item in _make_genre_tracks(db_session, root.id, "Hated", 20):
        item.artist = "Hated band"
    db_session.add(
        CollectionPreference(user_id=user.id, kind="artist", artist="Hated band", state="disliked")
    )
    db_session.commit()

    assert [mix.mix_genre for mix in refresh_all_genre_mixes(db_session, user)] == ["Rock"]


def test_song_mix_keeps_a_disliked_seed_but_no_other_dislikes(db_session):
    user = db_session.query(User).one()
    excluded, _kept, tracks = _dislike_library(db_session, user)
    seed = tracks["Solo 0"]  # disliked itself

    for _ in range(5):
        result = generate_playlist_from_track(db_session, user, seed.id)
        playlist = db_session.get(Playlist, result["playlist_id"])
        db_session.refresh(playlist)
        ids = [item.media_item_id for item in playlist.items]
        assert ids[0] == seed.id
        assert set(ids[1:]).isdisjoint(excluded)


def test_add_more_leaves_out_dislikes_for_every_kind(db_session):
    user = db_session.query(User).one()
    excluded, _kept, tracks = _dislike_library(db_session, user)
    mix = Playlist(
        user_id=user.id, name="Rock Mix", is_auto_generated=True,
        auto_kind="mix", mix_genre="Rock",
    )
    discovery = Playlist(
        user_id=user.id, name="Discovery", is_auto_generated=True, auto_kind="discovery",
    )
    generated = Playlist(
        user_id=user.id, name="Song Mix", is_auto_generated=True, auto_kind="generated",
    )
    db_session.add_all([mix, discovery, generated])
    db_session.commit()
    db_session.add_all(
        [
            PlaylistItem(playlist_id=generated.id, media_item_id=tracks["Seed song"].id, position=0),
            DiscoveryState(
                user_id=user.id,
                discovery_similar_artists_json=["Good", "Bad", "Mixed", "Solo"],
            ),
        ]
    )
    db_session.commit()

    for playlist in (mix, discovery, generated):
        assert add_more_tracks_to_auto_playlist(db_session, user, playlist) > 0
        db_session.refresh(playlist)
        added = {item.media_item_id for item in playlist.items if item.position > 0}
        assert added.isdisjoint(excluded), playlist.auto_kind


def test_discovery_never_seeds_from_or_recommends_a_disliked_artist(db_session):
    user = db_session.query(User).one()
    excluded, _kept, tracks = _dislike_library(db_session, user)
    _play(db_session, user, tracks["Bad 0"], times=5)
    _play(db_session, user, tracks["Seed song"], times=1)

    with patch("app.discovery.similar._similar_artist_names", return_value=set()):
        result = refresh_discovery_playlist(db_session, user)

    assert result["seed_artists"] == ["Seed"]
    assert "Bad" not in result["similar_artists"]
    playlist = db_session.get(Playlist, result["playlist_id"])
    ids = {item.media_item_id for item in playlist.items}
    assert ids.isdisjoint(excluded)
    # Discovery picks artists first, so even a liked song by a disliked
    # artist stays out.
    assert all(item.media_item.artist != "Bad" for item in playlist.items)


def test_explore_skips_disliked_seeds_and_album_artist_only_names(db_session, monkeypatch):
    from app.models import CollectionPreference

    user = db_session.query(User).one()
    _excluded, _kept, tracks = _dislike_library(db_session, user)
    compilation = _make_track(db_session, tracks["Seed song"].library_root_id, "Comp", "Guest")
    compilation.album_artist = "Label Only"
    db_session.add(
        CollectionPreference(user_id=user.id, kind="artist", artist="Label Only", state="disliked")
    )
    db_session.commit()
    _play(db_session, user, tracks["Bad 0"], times=5)
    _play(db_session, user, tracks["Seed song"], times=1)
    monkeypatch.setattr("app.discovery.explore.random.shuffle", lambda values: None)

    seeds = []
    with (
        patch(
            "app.discovery.explore._similar_artist_names",
            side_effect=lambda db, top, *a, **kw: seeds.extend(top) or {"Label Only", "Fresh"},
        ),
        patch(
            "app.discovery.explore.deezer.search_artist_track",
            side_effect=lambda name: {"artist": name, "preview_url": f"p:{name}"},
        ),
        patch("app.discovery.explore.time.sleep"),
    ):
        result = explore_new_artists(db_session, user)

    assert seeds == ["Seed"]
    assert [track["artist"] for track in result["results"]] == ["Fresh"]


def test_existing_mix_hides_new_dislikes_everywhere_without_topping_up(client, db_session):
    user = db_session.query(User).one()
    root = LibraryRoot(path="/existing", display_name="Existing")
    db_session.add(root)
    db_session.commit()
    keep, drop, seed = (
        _make_track(db_session, root.id, title, "Artist") for title in ("Keep", "Drop", "Seed")
    )
    mix = Playlist(
        user_id=user.id, name="Mix", is_auto_generated=True, auto_kind="mix", mix_genre="Rock"
    )
    song_mix = Playlist(
        user_id=user.id, name="Song Mix", is_auto_generated=True, auto_kind="generated"
    )
    mine = Playlist(user_id=user.id, name="Mine")
    db_session.add_all([mix, song_mix, mine])
    db_session.commit()
    for playlist, items in ((mix, [keep, drop]), (song_mix, [seed, keep, drop]), (mine, [keep, drop])):
        for position, item in enumerate(items):
            db_session.add(
                PlaylistItem(playlist_id=playlist.id, media_item_id=item.id, position=position)
            )
    db_session.commit()

    client.put(f"/api/media/{drop.id}/preference", json={"state": "disliked"})
    client.put(f"/api/media/{seed.id}/preference", json={"state": "disliked"})

    def titles(playlist_id):
        return [t["title"] for t in client.get(f"/api/playlists/{playlist_id}").json()["items"]]

    assert titles(mix.id) == ["Keep"]
    assert titles(song_mix.id) == ["Seed", "Keep"]  # the seed stays
    assert titles(mine.id) == ["Keep", "Drop"]  # user playlists never filter
    export = client.get(f"/api/playlists/{mix.id}/export.m3u").text
    assert "Keep" in export and "Drop" not in export
    counts = {p["id"]: p["item_count"] for p in client.get("/api/playlists").json()}
    assert counts[mix.id] == 1
    assert counts[song_mix.id] == 2
    assert counts[mine.id] == 2

    copy = save_auto_playlist_as_new(db_session, user, mix.id, "Saved")
    assert [item.media_item_id for item in copy.items] == [keep.id]
    # Not topped up: the stored mix still holds just its two rows.
    db_session.refresh(mix)
    assert len(mix.items) == 2


def test_like_boost_gives_about_one_and_a_half_times_the_share():
    import random
    from collections import Counter

    from app.discovery.sampling import _balanced_track_sample

    items = [
        MediaItem(id=artist * 100 + track, artist=f"Artist {artist}")
        for artist in range(11)
        for track in range(20)
    ]
    liked_artist = "Artist 10"
    stacked = {  # every like level at once on one artist - still one boost
        item.id: ("song", "album", "artist")[item.id % 3]
        for item in items
        if item.artist == liked_artist
    }
    for liked in (
        *({item.id: level for item in items if item.artist == liked_artist}
          for level in ("artist", "album", "song")),
        stacked,
    ):
        random.seed(20260924)
        counts = Counter()
        for _ in range(2000):
            counts.update(item.artist for item in _balanced_track_sample(items, 40, liked))
        ordinary = sum(n for artist, n in counts.items() if artist != liked_artist) / 10
        assert 1.35 <= counts[liked_artist] / ordinary <= 1.65


def test_liked_tracks_go_first_within_an_artist():
    import random

    from app.discovery.sampling import _balanced_track_sample

    items = [MediaItem(id=i, artist="Only") for i in range(10)]
    liked = {3: "song", 7: "album"}
    random.seed(1)
    for _ in range(50):
        assert [item.id for item in _balanced_track_sample(items, 2, liked)] == [7, 3]


def test_song_mix_and_autoplay_put_a_liked_song_first_within_its_artist(db_session):
    """Song Mix does not draw each liked track about LIKE_BOOST times as
    often in a flat per-track sample. Every mix kind shares the
    artist-balanced sampler, where a liked song leads its artist's turn (and
    a liked artist gets ~LIKE_BOOST times the share, see
    test_like_boost_gives_about_one_and_a_half_times_the_share). Autoplay
    reads likes like Song Mix."""
    import random

    from app.discovery import similar_tracks_for_seed
    from app.models import Favorite
    from app.playback import _continuation_tracks

    user = db_session.query(User).one()
    root = LibraryRoot(path="/liked", display_name="Liked")
    db_session.add(root)
    db_session.commit()
    seed = _make_track(db_session, root.id, "Seed", "Seed Artist")
    seed.genre = "Shoegaze"
    others = []
    for n in range(12):
        item = _make_track(db_session, root.id, f"Other {n}", "Other Artist")
        item.genre = "Shoegaze"
        others.append(item)
    db_session.add(Favorite(user_id=user.id, media_item_id=others[7].id))
    db_session.commit()

    rng = random.Random(5)  # noqa: S311 - reproducible test draws
    for _ in range(20):
        ids = similar_tracks_for_seed(db_session, user, seed.id, boost_likes=True, rng=rng)
        assert ids[0] == seed.id and others[7].id in ids
        autoplay = _continuation_tracks(db_session, user, seed.id)
        assert others[7].id in autoplay


def test_likes_never_pull_in_off_genre_tracks(db_session):
    from app.models import CollectionPreference

    user = db_session.query(User).one()
    root = LibraryRoot(path="/off-genre", display_name="Off genre")
    db_session.add(root)
    db_session.commit()
    rock = _make_genre_tracks(db_session, root.id, "Rock", 20)
    jazz = _make_genre_tracks(db_session, root.id, "Jazz", 5)
    for item in [*rock[:5], *jazz]:
        item.artist = "Loved"
    db_session.add(
        CollectionPreference(user_id=user.id, kind="artist", artist="Loved", state="liked")
    )
    db_session.commit()

    jazz_ids = {item.id for item in jazz}
    for _ in range(5):
        mix = refresh_all_genre_mixes(db_session, user)[0]
        assert {item.media_item_id for item in mix.items}.isdisjoint(jazz_ids)
    specified = add_specified_mix_playlist(db_session, user, "genre", "Rock")
    assert {item.media_item_id for item in specified.items}.isdisjoint(jazz_ids)


def test_discovery_counts_only_whole_artist_likes(db_session):
    """Discovery is for music the user has not heard much, so it counts
    only an artist liked as a whole (that artist's share is boosted like
    everywhere else); a liked song or album gives no boost there."""
    from app.discovery import discovery_playlist
    from app.models import CollectionPreference, Favorite

    user = db_session.query(User).one()
    root = LibraryRoot(path="/disc-likes", display_name="Discovery likes")
    db_session.add(root)
    db_session.commit()
    seed = _make_track(db_session, root.id, "Seed", "Seed Artist")
    seed.genre = "Rock"
    loved = [_make_track(db_session, root.id, f"Loved {n}", "Loved Artist") for n in range(3)]
    plain = [_make_track(db_session, root.id, f"Plain {n}", "Plain Artist") for n in range(3)]
    for item in [*loved, *plain]:
        item.genre = "Rock"
    db_session.add_all([
        CollectionPreference(user_id=user.id, kind="artist", artist="Loved Artist", state="liked"),
        Favorite(user_id=user.id, media_item_id=plain[0].id),
    ])
    db_session.commit()
    _play(db_session, user, seed)

    seen = []
    real = discovery_playlist._balanced_track_sample

    def spy(pool, limit, liked, *args, **kwargs):
        seen.append(dict(liked))
        return real(pool, limit, liked, *args, **kwargs)

    with (
        patch("app.discovery.discovery_playlist._balanced_track_sample", side_effect=spy),
        patch("app.discovery.similar._similar_artist_names", return_value=set()),
    ):
        assert refresh_discovery_playlist(db_session, user)["tracks_added"] == 6
    assert seen == [{item.id: "artist" for item in loved}]


def test_song_mix_matches_on_the_songs_tags_and_every_provider_source(db_session):
    from app.discovery import similar_tracks_for_seed
    from app.metadata_taxonomy import (
        replace_item_listenbrainz_tags,
        replace_item_musicbrainz_tags,
    )

    user = db_session.query(User).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()

    seed = _make_track(db_session, root.id, "Seed", "Seed Artist")
    replace_item_genres(db_session, seed, ["Rock", "Glam"], "embedded")
    replace_item_listenbrainz_tags(db_session, seed, [("Epic", 90)])
    by_genre = _make_track(db_session, root.id, "A", "Genre Artist")
    replace_item_genres(db_session, by_genre, ["glam"], "musicbrainz")  # case differs
    by_artist_tag = _make_track(db_session, root.id, "B", "Tag Artist")
    replace_item_musicbrainz_tags(db_session, by_artist_tag, [("epic", 3)])
    unrelated = _make_track(db_session, root.id, "C", "Other Artist")
    replace_item_genres(db_session, unrelated, ["Jazz"], "embedded")
    db_session.commit()

    ids = similar_tracks_for_seed(db_session, user, seed.id)

    assert ids[0] == seed.id
    assert set(ids[1:]) == {by_genre.id, by_artist_tag.id}


# --- performance --------------------------------------------------------------


def test_song_mix_works_past_sqlites_parameter_limit(db_session, session_factory):
    """Artists do not go into one IN list per label source: past SQLite's
    host-parameter limit (999 here, 32,766 in current builds - a library
    with ~8,000 artists and their "feat." variants) Song Mix and autoplay
    would fail with "too many SQL variables"."""
    import sqlite3

    from sqlalchemy import event

    from app.discovery import similar_tracks_for_seed

    user = db_session.query(User).one()
    root = LibraryRoot(path="/many", display_name="Many")
    db_session.add(root)
    db_session.commit()
    genre = Genre(name="Rock", normalized_name="rock")
    db_session.add(genre)
    items = [
        MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path=f"{n}.mp3", title=f"T{n}", artist=f"Artist {n}")
        for n in range(300)
    ]
    db_session.add_all(items)
    db_session.commit()
    db_session.add_all(MediaItemGenre(media_item_id=i.id, genre_id=genre.id, source="embedded") for i in items)
    db_session.commit()
    seed_id = items[0].id
    engine = session_factory.kw["bind"]
    event.listen(engine, "connect", lambda conn, _r: conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999))
    engine.dispose()
    session = session_factory()
    try:
        ids = similar_tracks_for_seed(session, session.get(User, user.id), seed_id)
    finally:
        session.close()
    # The seed, then one track from each of the 20 best-matching artists.
    assert ids is not None and ids[0] == seed_id and len(ids) == 21


def test_similar_artist_candidates_are_cached_until_the_library_changes(db_session):
    from app.discovery import similar
    from app.library_cache import bump_library_version

    root = LibraryRoot(path="/cache", display_name="Cache")
    db_session.add(root)
    db_session.commit()
    seed = _make_track(db_session, root.id, "Seed", "Seed Artist")
    other = _make_track(db_session, root.id, "Other", "Other Artist")
    replace_item_genres(db_session, seed, ["Shoegaze"], "embedded")
    replace_item_genres(db_session, other, ["Shoegaze"], "embedded")
    db_session.commit()

    with patch.object(similar, "_label_overlap_by_artist", wraps=similar._label_overlap_by_artist) as overlap:
        first = similar._artists_similar_by_genre(db_session, {root.id}, ["Seed Artist"], allow_fetch=False)
        again = similar._artists_similar_by_genre(db_session, {root.id}, ["Seed Artist"], allow_fetch=False)
    assert first == again == {"Other Artist"}
    # One label index for the library version and one score entry for the
    # seed.
    assert sorted(key[0] for key in similar._overlap_cache) == ["index", "scores"]

    late = _make_track(db_session, root.id, "Late", "Late Artist")
    replace_item_genres(db_session, late, ["Shoegaze"], "embedded")
    db_session.commit()
    # Unchanged until the library version moves, as a scan's does.
    assert similar._artists_similar_by_genre(db_session, {root.id}, ["Seed Artist"], allow_fetch=False) == {"Other Artist"}
    bump_library_version()
    assert similar._artists_similar_by_genre(db_session, {root.id}, ["Seed Artist"], allow_fetch=False) == {
        "Other Artist",
        "Late Artist",
    }
    assert overlap.call_count == 2


def test_ignoring_or_restoring_a_folder_moves_the_library_version(client, db_session, library_dir):
    from app.library_cache import library_version

    (library_dir / "Holiday").mkdir()
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    before = library_version()
    assert client.post(f"/api/library/roots/{root.id}/ignored-folders", json={"path": "Holiday"}).status_code == 200
    ignored = library_version()
    assert ignored > before
    assert client.request("DELETE", f"/api/library/roots/{root.id}/ignored-folders", json={"path": "Holiday"}).status_code == 200
    assert library_version() > ignored


def test_umbrella_labels_come_from_pairwise_overlap_counts():
    from app.discovery.pools import _umbrella_labels

    qualifying = [("genre", "Rock", 100), ("genre", "Shoegaze", 20), ("tag", "rock", 50), ("tag", "Loud", 30)]
    overlap = {
        frozenset({("genre", "Rock"), ("genre", "Shoegaze")}): 16,  # 80% of shoegaze: umbrella
        frozenset({("genre", "Rock"), ("tag", "rock")}): 50,  # same name: never an umbrella
        frozenset({("tag", "rock"), ("tag", "Loud")}): 23,  # under 80% of 30
    }
    assert _umbrella_labels(qualifying, overlap) == {("genre", "Rock")}


def test_daily_refresh_catches_up_once_its_hour_has_passed(db_session):
    user = db_session.query(User).one()
    update_discovery_settings(db_session, user, enabled=True, hour=6)
    with (
        patch("app.discovery.scheduler.refresh_discovery_playlist") as refresh,
        patch("app.discovery.scheduler.explore_new_artists"),
        patch("app.discovery.scheduler.refresh_all_genre_mixes"),
    ):
        # A tick that overran 6 o'clock, a restart, a DST gap: the next tick
        # that day still runs it, once.
        run_due_auto_refreshes(db_session, current_hour=9)
        run_due_auto_refreshes(db_session, current_hour=10)
    assert refresh.call_count == 1
    assert db_session.get(DiscoveryState, user.id).last_auto_refresh_date == date.today().isoformat()


def test_explore_stops_at_its_time_budget(db_session, monkeypatch):
    from app import discovery

    user = db_session.query(User).one()
    clock = iter(range(0, 1000, 10))  # every look at the clock is 10 s later
    monkeypatch.setattr("app.discovery.explore.time.monotonic", lambda: float(next(clock)))
    monkeypatch.setattr("app.discovery.explore.time.sleep", lambda _s: None)
    calls: list[str] = []

    def search(name):
        calls.append(name)
        return None  # Deezer down: every lookup comes back empty

    with (
        patch("app.discovery.explore._top_played_artists", return_value=["Seed"]),
        patch("app.discovery.explore._similar_artist_names", return_value={f"A{n}" for n in range(60)}),
        patch("app.discovery.explore.deezer.search_artist_track", side_effect=search),
    ):
        result = explore_new_artists(db_session, user)
    assert result["results"] == []
    assert len(calls) == discovery.EXPLORE_TIME_BUDGET_SECONDS // 10 - 1


def test_explore_stops_after_three_deezer_failures_in_a_row(db_session, monkeypatch):
    import httpx

    from app.discovery import EXPLORE_FAILURE_LIMIT

    user = db_session.query(User).one()
    monkeypatch.setattr("app.discovery.explore.time.sleep", lambda _s: None)
    with (
        patch("app.discovery.explore._top_played_artists", return_value=["Seed"]),
        patch("app.discovery.explore._similar_artist_names", return_value={f"A{n}" for n in range(60)}),
        patch(
            "app.metadata_providers.deezer.httpx.get", side_effect=httpx.ConnectError("down")
        ) as get,
    ):
        result = explore_new_artists(db_session, user)
    assert result["results"] == []
    assert get.call_count == EXPLORE_FAILURE_LIMIT == 3
    assert get_discovery_state(db_session, user)["explore_results"] == []


def test_explore_failure_streak_is_reset_by_a_miss_or_a_preview(db_session, monkeypatch):
    from app.metadata_providers.result import note_failure

    user = db_session.query(User).one()
    names = sorted(f"A{n:02d}" for n in range(12))
    # Two failures, a miss (no preview), two failures, a preview, two
    # failures, then three: only the last run of three stops it.
    plan = ["fail", "fail", "miss", "fail", "fail", "track", "fail", "fail", "fail", "track"]
    calls: list[str] = []

    def search(name):
        outcome = plan[len(calls)]
        calls.append(name)
        if outcome == "fail":
            note_failure("Deezer HTTP 503")
            return None
        if outcome == "miss":
            return None
        return {"artist": name, "title": "T", "preview_url": f"p:{name}"}

    monkeypatch.setattr("app.discovery.explore.random.shuffle", lambda values: None)
    monkeypatch.setattr("app.discovery.explore.time.sleep", lambda _s: None)
    with (
        patch("app.discovery.explore._top_played_artists", return_value=["Seed"]),
        patch("app.discovery.explore._similar_artist_names", return_value=set(names)),
        patch("app.discovery.explore.deezer.search_artist_track", side_effect=search),
    ):
        result = explore_new_artists(db_session, user)
    assert len(calls) == 9
    assert [t["artist"] for t in result["results"]] == [names[5]]


def test_discovery_cache_survives_evictions_from_many_threads(monkeypatch):
    """The cache is shared by request threads and the scheduler: a hit
    evicted by another thread before it is marked recently used must not
    raise KeyError, and dropping stale keys while another thread stores must
    not raise "mutated during iteration"."""
    import sys
    import threading

    from app import library_cache
    from app.discovery import eligibility

    monkeypatch.setattr(eligibility, "_CACHE_SIZE", 4)
    errors: list[BaseException] = []
    start = threading.Barrier(8)
    stop_at = time.monotonic() + 1.0

    def hammer(offset: int) -> None:
        try:
            start.wait(5)
            n = offset
            while time.monotonic() < stop_at:
                n += 1
                version = library_cache.library_version()
                key = ("hammer", version, n % 16)
                assert eligibility._cached(key, lambda key=key: {"key": key}) == {"key": key}
                if n % 50 == 0:
                    library_cache.bump_library_version()  # entries go stale mid-flight
        except BaseException as exc:  # noqa: BLE001 - reported by the main thread
            errors.append(exc)

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # switch threads between the cache's steps
    try:
        threads = [threading.Thread(target=hammer, args=(i * 3,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
    finally:
        sys.setswitchinterval(interval)
    assert errors == []
    assert len(eligibility._overlap_cache) <= 4
    assert eligibility._key_locks == {}  # no per-key lock is left behind
    version = library_cache.library_version()
    assert all(key[1] == version for key in eligibility._overlap_cache)


def test_discovery_cache_computes_a_missing_key_once_for_concurrent_callers():
    """Two requests after a library change build the label index once."""
    import threading
    from collections import Counter

    from app import library_cache
    from app.discovery import eligibility, similar

    assert similar._overlap_cache is eligibility._overlap_cache  # what tests clear
    version = library_cache.library_version()
    computed: Counter[int] = Counter()
    counting = threading.Lock()
    start = threading.Barrier(24)
    results: list[tuple[int, object]] = []

    def compute(n: int) -> dict:
        with counting:
            computed[n] += 1
        time.sleep(0.05)  # long enough for every caller to arrive
        return {"n": n}

    def ask(n: int) -> None:
        start.wait(5)
        results.append((n, eligibility._cached(("single", version, n), lambda: compute(n))))

    threads = [threading.Thread(target=ask, args=(i % 4,)) for i in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert dict(computed) == {0: 1, 1: 1, 2: 1, 3: 1}
    assert len(results) == 24 and all(value == {"n": n} for n, value in results)
    # Every caller of a key got the one computed object.
    assert len({id(value) for _n, value in results}) == 4


def test_discovery_cache_lets_the_next_caller_compute_after_a_failure():
    from app import library_cache
    from app.discovery import eligibility

    key = ("failing", library_cache.library_version(), 1)

    def boom():
        raise RuntimeError("database is locked")

    with pytest.raises(RuntimeError):
        eligibility._cached(key, boom)
    assert eligibility._key_locks == {} and key not in eligibility._overlap_cache
    assert eligibility._cached(key, lambda: {"ok": True}) == {"ok": True}
    # A value computed across a library change is returned but not kept.
    stale_key = ("stale", library_cache.library_version(), 1)

    def changed_meanwhile():
        library_cache.bump_library_version()
        return {"old": True}

    assert eligibility._cached(stale_key, changed_meanwhile) == {"old": True}
    assert stale_key not in eligibility._overlap_cache and key not in eligibility._overlap_cache
