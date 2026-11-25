from __future__ import annotations

from datetime import timedelta

from app.artist_credits import rebuild_all_credits
from app.models import LibraryRoot, MediaItem, MediaKind, PlayHistory, User, utcnow
from app.scanner import scan_library_root
from app.security import hash_password
from tests.conftest import DEFAULT_USERNAME, login_as, make_audio_fixture


def _scan_sample_library(db_session, library_dir):
    make_audio_fixture(
        library_dir / "a1.wav", title="A1", artist="Band One", album="LP", genre="Rock", track_number=1
    )
    make_audio_fixture(
        library_dir / "a2.wav", title="A2", artist="Band One", album="LP", genre="Rock", track_number=2
    )
    make_audio_fixture(
        library_dir / "b1.wav", title="B1", artist="Band One", album="EP", genre="Rock", track_number=1
    )
    make_audio_fixture(
        library_dir / "c1.wav", title="C1", artist="Band Two", album="Solo", genre="Jazz", track_number=1
    )
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    return root


def test_list_albums_groups_by_artist_and_album(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    resp = client.get("/api/library/albums")
    assert resp.status_code == 200
    albums = {(a["artist"], a["album"]): a["track_count"] for a in resp.json()}
    assert albums == {
        ("Band One", "LP"): 2,
        ("Band One", "EP"): 1,
        ("Band Two", "Solo"): 1,
    }


def test_albums_support_limit_offset_with_stable_order(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    all_albums = client.get("/api/library/albums").json()
    page = client.get("/api/library/albums", params={"limit": 1, "offset": 1}).json()
    assert page == all_albums[1:2]


def test_artists_support_limit_offset_with_stable_order(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    all_artists = client.get("/api/library/artists").json()
    page = client.get("/api/library/artists", params={"limit": 1, "offset": 1}).json()
    assert page == all_artists[1:2]


def test_list_albums_has_representative_track_id(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    resp = client.get("/api/library/albums")
    for album in resp.json():
        assert isinstance(album["representative_media_item_id"], int)


def test_album_and_artist_browse_results_are_privately_cacheable(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)

    for path in ("/api/library/albums", "/api/library/artists"):
        response = client.get(path)
        assert response.headers["cache-control"] == "private, max-age=60, stale-while-revalidate=120"
        # Starlette's CORS middleware adds "Origin" to Vary on every response.
        assert "Cookie" in {v.strip() for v in response.headers["vary"].split(",")}


def test_list_artists_groups_and_counts_albums(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    resp = client.get("/api/library/artists")
    assert resp.status_code == 200
    by_name = {a["artist"]: a for a in resp.json()}
    assert by_name["Band One"]["track_count"] == 3
    assert by_name["Band One"]["album_count"] == 2
    assert by_name["Band Two"]["track_count"] == 1
    assert by_name["Band Two"]["album_count"] == 1


def test_list_artists_includes_album_artist_and_artist_track_filter(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    track = db_session.query(MediaItem).filter_by(title="A1").one()
    track.album_artist = "Various Artists"
    db_session.commit()
    rebuild_all_credits(db_session)

    response = client.get("/api/library/artists")
    assert response.status_code == 200
    by_name = {artist["artist"]: artist for artist in response.json()}
    assert by_name["Various Artists"]["track_count"] == 1
    assert by_name["Band One"]["track_count"] == 3

    songs = client.get("/api/library/tracks", params={"artist": "Various Artists"})
    assert songs.status_code == 200
    assert {song["title"] for song in songs.json()} == {"A1"}


def test_albums_and_artists_scoped_to_accessible_roots(
    session_factory, client, db_session, library_dir, tmp_path
):
    _scan_sample_library(db_session, library_dir)

    private_dir = tmp_path / "private"
    private_dir.mkdir()
    make_audio_fixture(
        private_dir / "d1.wav", title="D1", artist="Hidden", album="Secret", genre="Rock"
    )
    private_root = LibraryRoot(path=str(private_dir), display_name="Private", is_shared=False)
    db_session.add(private_root)
    db_session.commit()
    db_session.refresh(private_root)
    scan_library_root(db_session, private_root)

    other = User(username="scoping-other", password_hash=hash_password("pw"))
    db_session.add(other)
    db_session.commit()
    other_client = login_as(session_factory, client, "scoping-other", "pw")

    albums = {(a["artist"], a["album"]) for a in other_client.get("/api/library/albums").json()}
    artists = {a["artist"] for a in other_client.get("/api/library/artists").json()}
    assert ("Hidden", "Secret") not in albums
    assert "Hidden" not in artists
    for sort in ("most_played", "last_played"):
        for direction in ("asc", "desc"):
            params = {"sort": sort, "direction": direction}
            assert "Secret" not in {a["album"] for a in other_client.get("/api/library/albums", params=params).json()}
            assert "Hidden" not in {a["artist"] for a in other_client.get("/api/library/artists", params=params).json()}


def test_list_albums_search_matches_album_or_artist_name(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)

    resp = client.get("/api/library/albums", params={"q": "lp"})
    assert {(a["artist"], a["album"]) for a in resp.json()} == {("Band One", "LP")}

    resp = client.get("/api/library/albums", params={"q": "band two"})
    assert {(a["artist"], a["album"]) for a in resp.json()} == {("Band Two", "Solo")}

    resp = client.get("/api/library/albums", params={"q": "nonexistent"})
    assert resp.json() == []


def test_list_artists_search_matches_artist_name(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)

    resp = client.get("/api/library/artists", params={"q": "two"})
    assert {a["artist"] for a in resp.json()} == {"Band Two"}


def test_list_albums_filters_by_genre(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)

    resp = client.get("/api/library/albums", params={"genre": "Jazz"})
    assert {(a["artist"], a["album"]) for a in resp.json()} == {("Band Two", "Solo")}

    resp = client.get("/api/library/albums", params={"genre": "Rock"})
    assert {(a["artist"], a["album"]) for a in resp.json()} == {
        ("Band One", "LP"),
        ("Band One", "EP"),
    }


def test_list_artists_filters_by_genre(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)

    resp = client.get("/api/library/artists", params={"genre": "Jazz"})
    assert {a["artist"] for a in resp.json()} == {"Band Two"}


def test_list_albums_sort_alphabetical_is_default(client, db_session, library_dir):
    # An artist that sorts first with an album title that sorts last, so
    # title order and artist order disagree.
    make_audio_fixture(
        library_dir / "z1.wav", title="Z1", artist="Aardvark", album="Zoo", genre="Rock", track_number=1
    )
    _scan_sample_library(db_session, library_dir)
    names = [(a["artist"], a["album"]) for a in client.get("/api/library/albums").json()]
    assert names == sorted(names, key=lambda t: (t[1].lower(), (t[0] or "").lower()))
    assert names[-1] == ("Aardvark", "Zoo")


def test_list_albums_sort_by_date_added_newest_first(client, db_session, library_dir):
    make_audio_fixture(
        library_dir / "old.wav", title="Old", artist="Old Artist", album="Old Album", genre="Rock"
    )
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    # The old track was added a day earlier (rather than waiting for the
    # clock to move on between the two scans).
    db_session.query(MediaItem).update({MediaItem.created_at: utcnow() - timedelta(days=1)})
    db_session.commit()
    make_audio_fixture(
        library_dir / "new.wav", title="New", artist="New Artist", album="New Album", genre="Rock"
    )
    scan_library_root(db_session, root)

    albums = [a["album"] for a in client.get("/api/library/albums", params={"sort": "date_added"}).json()]
    assert albums.index("New Album") < albums.index("Old Album")


def test_list_artists_sort_by_date_added_newest_first(client, db_session, library_dir):
    make_audio_fixture(
        library_dir / "old.wav", title="Old", artist="Old Artist", album="Old Album", genre="Rock"
    )
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    # The old track was added a day earlier (rather than waiting for the
    # clock to move on between the two scans).
    db_session.query(MediaItem).update({MediaItem.created_at: utcnow() - timedelta(days=1)})
    db_session.commit()
    make_audio_fixture(
        library_dir / "new.wav", title="New", artist="New Artist", album="New Album", genre="Rock"
    )
    scan_library_root(db_session, root)

    artists = [a["artist"] for a in client.get("/api/library/artists", params={"sort": "date_added"}).json()]
    assert artists.index("New Artist") < artists.index("Old Artist")


def test_list_albums_sort_by_date_published(client, db_session, library_dir):
    make_audio_fixture(
        library_dir / "old.wav", title="Old", artist="A", album="Old Album", genre="Rock", date="1990-06-01"
    )
    # Two releases in the *same* year - only correct with full-date
    # precision, not just a bare year.
    make_audio_fixture(
        library_dir / "early.wav",
        title="Early",
        artist="B",
        album="Early 2020 Single",
        genre="Rock",
        date="2020-01-15",
    )
    make_audio_fixture(
        library_dir / "late.wav",
        title="Late",
        artist="B",
        album="Late 2020 Single",
        genre="Rock",
        date="2020-11-30",
    )
    make_audio_fixture(
        library_dir / "unknown.wav", title="Unknown", artist="C", album="No Date Album", genre="Rock"
    )
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)

    albums = [
        a["album"] for a in client.get("/api/library/albums", params={"sort": "date_published"}).json()
    ]
    assert (
        albums.index("Late 2020 Single")
        < albums.index("Early 2020 Single")
        < albums.index("Old Album")
        < albums.index("No Date Album")
    )


def test_list_albums_sort_by_most_played(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    user = db_session.query(User).filter_by(username=DEFAULT_USERNAME).one()
    solo_track = db_session.query(MediaItem).filter_by(album="Solo").one()
    for _ in range(3):
        db_session.add(PlayHistory(user_id=user.id, media_item_id=solo_track.id))
    db_session.commit()

    albums = [
        a["album"] for a in client.get("/api/library/albums", params={"sort": "most_played"}).json()
    ]
    assert albums[0] == "Solo"


def test_list_albums_sort_by_last_played(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    user = db_session.query(User).filter_by(username=DEFAULT_USERNAME).one()
    lp_track = db_session.query(MediaItem).filter_by(album="LP").first()
    solo_track = db_session.query(MediaItem).filter_by(album="Solo").one()
    db_session.add(
        PlayHistory(user_id=user.id, media_item_id=lp_track.id, played_at=utcnow() - timedelta(days=5))
    )
    db_session.add(PlayHistory(user_id=user.id, media_item_id=solo_track.id, played_at=utcnow()))
    db_session.commit()

    albums = [
        a["album"] for a in client.get("/api/library/albums", params={"sort": "last_played"}).json()
    ]
    assert albums.index("Solo") < albums.index("LP") < albums.index("EP")


def test_list_artists_sort_by_most_played(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    user = db_session.query(User).filter_by(username=DEFAULT_USERNAME).one()
    two_track = db_session.query(MediaItem).filter_by(artist="Band Two").one()
    for _ in range(5):
        db_session.add(PlayHistory(user_id=user.id, media_item_id=two_track.id))
    db_session.commit()

    artists = [
        a["artist"] for a in client.get("/api/library/artists", params={"sort": "most_played"}).json()
    ]
    assert artists[0] == "Band Two"


# artist, album, plays by the default user, days since their last play, plays
# by a second user. B and C tie on plays; D was never played by the default
# user (only the second user plays it, and a lot).
_PLAYED = [
    ("Aa", "Alpha", 3, 5, 0),
    ("Bb", "Bravo", 1, 9, 0),
    ("Cc", "Charlie", 1, 1, 0),
    ("Dd", "Delta", 0, 0, 7),
    ("Ee", "Echo", 2, 3, 0),
    ("Ff", "Foxtrot", 0, 0, 0),
]


def _played_library(db_session, session_factory, client):
    """One track per album and artist with the default user's own play
    history, plus a second user whose plays must not count. Returns that
    user's client."""
    root = LibraryRoot(path="/played", display_name="Played")
    db_session.add(root)
    db_session.commit()
    me = db_session.query(User).filter_by(username=DEFAULT_USERNAME).one()
    other = User(username="played-other", password_hash=hash_password("pw"))
    db_session.add(other)
    db_session.commit()
    now = utcnow()
    for artist, album, plays, days, other_plays in _PLAYED:
        item = MediaItem(
            library_root_id=root.id, kind=MediaKind.track, relative_path=f"{artist}/{album}.mp3",
            title=f"{album} song", artist=artist, album=album,
        )
        db_session.add(item)
        db_session.commit()
        for n in range(plays):
            db_session.add(PlayHistory(user_id=me.id, media_item_id=item.id, played_at=now - timedelta(days=days, hours=n)))
        for n in range(other_plays):
            db_session.add(PlayHistory(user_id=other.id, media_item_id=item.id, played_at=now - timedelta(minutes=n)))
    db_session.commit()
    rebuild_all_credits(db_session)
    other_client = login_as(session_factory, client, "played-other", "pw")
    return other_client


def _names(client, kind, **params):
    key = "album" if kind == "albums" else "artist"
    return [row[key] for row in client.get(f"/api/library/{kind}", params=params).json()]


# Most played: Alpha 3, Echo 2, then Bravo / Charlie (1 each, tie broken by
# name), then the never-played Delta and Foxtrot - last in both directions.
_MOST = ["Alpha", "Echo", "Bravo", "Charlie", "Delta", "Foxtrot"]
_MOST_ASC = ["Bravo", "Charlie", "Echo", "Alpha", "Delta", "Foxtrot"]
# Last played: Charlie (1 day), Echo, Alpha, Bravo (9 days), then never.
_LAST = ["Charlie", "Echo", "Alpha", "Bravo", "Delta", "Foxtrot"]
_LAST_ASC = ["Bravo", "Alpha", "Echo", "Charlie", "Delta", "Foxtrot"]
_ARTIST_OF = {"Alpha": "Aa", "Bravo": "Bb", "Charlie": "Cc", "Delta": "Dd", "Echo": "Ee", "Foxtrot": "Ff"}


def test_played_sorts_desc_means_most_first_and_it_is_the_default(client, db_session, session_factory):
    _played_library(db_session, session_factory, client)
    for kind in ("albums", "artists"):
        convert = (lambda names: names) if kind == "albums" else (lambda names: [_ARTIST_OF[n] for n in names])
        for sort, desc, asc in (("most_played", _MOST, _MOST_ASC), ("last_played", _LAST, _LAST_ASC)):
            assert _names(client, kind, sort=sort) == convert(desc), (kind, sort, "default")
            assert _names(client, kind, sort=sort, direction="desc") == convert(desc), (kind, sort, "desc")
            assert _names(client, kind, sort=sort, direction="asc") == convert(asc), (kind, sort, "asc")


def test_played_sorts_count_only_the_signed_in_users_plays(client, db_session, session_factory):
    other_client = _played_library(db_session, session_factory, client)
    # The second user played only Delta, but a lot: it leads their lists and
    # the default user's never-played albums stay last in theirs.
    for kind, name in (("albums", "Delta"), ("artists", "Dd")):
        for sort in ("most_played", "last_played"):
            for direction in ("asc", "desc"):
                theirs = _names(other_client, kind, sort=sort, direction=direction)
                assert theirs[0] == name, (kind, sort, direction)
                assert theirs[-1] != name


def test_played_sorts_page_without_repeating_or_skipping(client, db_session, session_factory):
    _played_library(db_session, session_factory, client)
    for kind in ("albums", "artists"):
        for sort in ("most_played", "last_played"):
            for direction in ("asc", "desc"):
                whole = _names(client, kind, sort=sort, direction=direction)
                pages = [
                    *_names(client, kind, sort=sort, direction=direction, limit=2, offset=0),
                    *_names(client, kind, sort=sort, direction=direction, limit=2, offset=2),
                    *_names(client, kind, sort=sort, direction=direction, limit=2, offset=4),
                ]
                assert pages == whole, (kind, sort, direction)
                assert len(set(whole)) == 6


def test_played_sorts_with_nothing_played_list_everything_by_name(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    for sort in ("most_played", "last_played"):
        for direction in ("asc", "desc"):
            assert _names(client, "albums", sort=sort, direction=direction) == ["EP", "LP", "Solo"]
            assert _names(client, "artists", sort=sort, direction=direction) == ["Band One", "Band Two"]

def _scan_many_albums(db_session, library_dir, count):
    root = LibraryRoot(path=str(library_dir), display_name="Big Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    for i in range(count):
        make_audio_fixture(
            library_dir / f"t{i:04d}.wav",
            title=f"T{i}",
            artist=f"Artist {i:04d}",
            album=f"Album {i:04d}",
            genre="Rock",
            seconds=0.05,
        )
    scan_library_root(db_session, root)
    return root


def test_list_albums_full_returns_all_ignoring_default_limit(client, db_session, library_dir):
    _scan_many_albums(db_session, library_dir, 150)

    resp = client.get("/api/library/albums", params={"full": 1})
    assert resp.status_code == 200
    assert len(resp.json()) == 150

    # Pagination params are ignored entirely when full=1: offset doesn't
    # skip anything and the default limit doesn't truncate.
    paged = client.get("/api/library/albums", params={"full": 1, "limit": 50, "offset": 10})
    assert paged.json() == resp.json()


def test_list_artists_full_returns_all_ignoring_default_limit(client, db_session, library_dir):
    _scan_many_albums(db_session, library_dir, 150)

    resp = client.get("/api/library/artists", params={"full": 1})
    assert resp.status_code == 200
    assert len(resp.json()) == 150

    paged = client.get("/api/library/artists", params={"full": 1, "limit": 50, "offset": 10})
    assert paged.json() == resp.json()


def test_list_albums_full_respects_safety_cap(client, db_session, library_dir, monkeypatch):
    import app.routers.library.albums_artists as library_router

    _scan_many_albums(db_session, library_dir, 12)
    monkeypatch.setattr(library_router, "FULL_LIST_SAFETY_CAP", 10)

    resp = client.get("/api/library/albums", params={"full": 1})
    assert resp.status_code == 200
    assert len(resp.json()) == 10


def test_list_albums_full_is_gzipped(client, db_session, library_dir):
    _scan_many_albums(db_session, library_dir, 40)

    resp = client.get("/api/library/albums", params={"full": 1}, headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    assert len(resp.json()) == 40


def test_list_artists_full_respects_safety_cap(client, db_session, library_dir, monkeypatch):
    import app.routers.library.albums_artists as library_router

    _scan_many_albums(db_session, library_dir, 12)
    monkeypatch.setattr(library_router, "FULL_LIST_SAFETY_CAP", 10)

    resp = client.get("/api/library/artists", params={"full": 1})
    assert resp.status_code == 200
    assert len(resp.json()) == 10


def test_list_albums_full_cache_key_excludes_pagination(client, db_session, library_dir):
    from app import library_cache

    _scan_many_albums(db_session, library_dir, 5)

    first = client.get("/api/library/albums", params={"full": 1, "limit": 2, "offset": 1}).json()
    second = client.get("/api/library/albums", params={"full": 1, "limit": 5, "offset": 0}).json()
    assert first == second
    full_keys = [key for (_, key) in library_cache._store if key[0] == "albums_full"]
    assert len(full_keys) == 1

    # The default (full=False) cache keys stay per-page.
    client.get("/api/library/albums", params={"limit": 2, "offset": 1})
    client.get("/api/library/albums", params={"limit": 2, "offset": 0})
    page_keys = [key for (_, key) in library_cache._store if key[0] == "albums"]
    assert len(page_keys) == 2
    assert all(len(key) == 10 for key in page_keys)


def test_list_artists_full_cache_key_excludes_pagination(client, db_session, library_dir):
    from app import library_cache

    _scan_many_albums(db_session, library_dir, 5)

    first = client.get("/api/library/artists", params={"full": 1, "limit": 2, "offset": 1}).json()
    second = client.get("/api/library/artists", params={"full": 1, "limit": 5, "offset": 0}).json()
    assert first == second
    full_keys = [key for (_, key) in library_cache._store if key[0] == "artists_full"]
    assert len(full_keys) == 1


def test_list_albums_filters_by_artist(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)

    resp = client.get("/api/library/albums", params={"artist": "Band One"})
    assert resp.status_code == 200
    rows = {(a["artist"], a["album"]) for a in resp.json()}
    assert rows == {("Band One", "LP"), ("Band One", "EP")}
    assert all(a["artist"] == "Band One" for a in resp.json())


def test_list_albums_artist_filter_matches_album_artist(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    track = db_session.query(MediaItem).filter_by(title="C1").one()
    track.album_artist = "Band One"
    db_session.commit()
    rebuild_all_credits(db_session)

    band_one = client.get("/api/library/albums", params={"artist": "Band One"}).json()
    assert ("Band One", "Solo") in {(a["artist"], a["album"]) for a in band_one}
    assert all(a["artist"] == "Band One" for a in band_one)

    # Band Two only performs on an album Band One owns: it is listed under
    # Band Two's "Appears on", still opened by its owner.
    band_two = client.get("/api/library/albums", params={"artist": "Band Two"}).json()
    assert ("Band One", "Solo") not in {(a["artist"], a["album"]) for a in band_two}
    appears = client.get(
        "/api/library/albums", params={"artist": "Band Two", "appears_on": 1}
    ).json()
    assert [(a["artist"], a["album"]) for a in appears] == [("Band One", "Solo")]


def test_list_albums_artist_filter_collapses_compilation_by_album(
    client, db_session, library_dir
):
    _scan_sample_library(db_session, library_dir)
    for title in ("A1", "A2"):
        track = db_session.query(MediaItem).filter_by(title=title).one()
        track.album_artist = "Various Artists"
    db_session.query(MediaItem).filter_by(title="A1").one().artist = "Band Three"
    db_session.commit()
    rebuild_all_credits(db_session)

    resp = client.get("/api/library/albums", params={"artist": "Various Artists"})
    assert resp.status_code == 200
    assert [(a["artist"], a["album"], a["track_count"]) for a in resp.json()] == [
        ("Various Artists", "LP", 2)
    ]


def test_list_albums_artist_filter_not_truncated_by_safety_cap(
    client, db_session, library_dir, monkeypatch
):
    import app.routers.library.albums_artists as library_router

    _scan_many_albums(db_session, library_dir, 12)
    monkeypatch.setattr(library_router, "FULL_LIST_SAFETY_CAP", 10)

    resp = client.get("/api/library/albums", params={"full": 1, "artist": "Artist 0011"})
    assert resp.status_code == 200
    assert [(a["artist"], a["album"]) for a in resp.json()] == [("Artist 0011", "Album 0011")]


def test_list_albums_artist_filter_has_its_own_cache_entry(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)

    band_one = client.get("/api/library/albums", params={"full": 1, "artist": "Band One"}).json()
    band_two = client.get("/api/library/albums", params={"full": 1, "artist": "Band Two"}).json()
    assert {a["album"] for a in band_one} == {"LP", "EP"}
    assert {a["album"] for a in band_two} == {"Solo"}


def test_artist_album_counts_match_album_filter(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    track = db_session.query(MediaItem).filter_by(title="C1").one()
    track.album_artist = "Band One"
    db_session.commit()
    rebuild_all_credits(db_session)

    for artist in client.get("/api/library/artists").json():
        albums = client.get(
            "/api/library/albums", params={"full": 1, "artist": artist["artist"]}
        ).json()
        assert artist["album_count"] == len(albums), artist["artist"]


def test_list_albums_artist_filter_sorts_by_album_not_track_artist(
    client, db_session, library_dir
):
    """An album credited to the artist only as album_artist must sort by its
    own title, not by whatever track artist SQLite happens to return for the
    group (Band Two sorts after Band One, which would put Aardvark last
    instead of first)."""
    _scan_sample_library(db_session, library_dir)
    track = db_session.query(MediaItem).filter_by(title="C1").one()
    track.album = "Aardvark"
    track.album_artist = "Band One"
    db_session.commit()
    rebuild_all_credits(db_session)
    rebuild_all_credits(db_session)

    albums = [
        a["album"]
        for a in client.get(
            "/api/library/albums", params={"full": 1, "artist": "Band One"}
        ).json()
    ]
    assert albums == ["Aardvark", "EP", "LP"]
