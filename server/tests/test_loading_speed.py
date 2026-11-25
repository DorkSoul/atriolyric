"""The app-wide loading pass: light routes for what a page needs first
(nav counts, one album's header), reads that answer from stored state and do
slow work in the background (want-list matching, provider look-ups), and
lists whose query count does not grow with their rows."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import event, select

from app.artist_credits import rebuild_all_credits
from app.library_cache import bump_library_version
from app.models import (
    LibraryRoot,
    MediaItem,
    MediaKind,
    PlayHistory,
    Playlist,
    PlaySourceHistory,
    RadioStation,
    RadioStationFavorite,
    User,
)
from app.scanner import scan_library_root
from app.security import hash_password
from app.want_list import match_owned, wait_for_rematches
from tests.conftest import DEFAULT_USERNAME, login_as, make_audio_fixture

# --- helpers ---------------------------------------------------------------------


def _me(db_session) -> User:
    return db_session.query(User).filter_by(username=DEFAULT_USERNAME).one()


def _statements(db_session):
    engine = db_session.get_bind()
    seen: list[str] = []

    def record(_conn, _cursor, statement, *_args):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    return seen, lambda: event.remove(engine, "before_cursor_execute", record)


def _two_roots(db_session, tmp_path):
    """A shared root (album X by A) and a private one (album Y by B, and an
    album with no artist), scanned."""
    shared_dir, private_dir = tmp_path / "shared", tmp_path / "private"
    shared_dir.mkdir()
    private_dir.mkdir()
    make_audio_fixture(shared_dir / "s1.wav", title="S1", artist="A", album="X", genre="Rock", date="1999")
    make_audio_fixture(shared_dir / "s2.wav", title="S2", artist="A", album="X", genre="Rock", track_number=2)
    make_audio_fixture(shared_dir / "n.wav", title="N", artist=None, album="Nameless", genre=None)
    make_audio_fixture(private_dir / "p.wav", title="P", artist="B", album="Y", genre="Jazz")
    roots = {}
    for path, name, shared in ((shared_dir, "Shared", True), (private_dir, "Private", False)):
        root = LibraryRoot(path=str(path), display_name=name, is_shared=shared)
        db_session.add(root)
        db_session.commit()
        scan_library_root(db_session, root)
        roots[name] = root
    return roots


def _plain(db_session, session_factory, client, username="plain"):
    user = User(username=username, password_hash=hash_password("plainpass"))
    db_session.add(user)
    db_session.commit()
    return user, login_as(session_factory, client, username, "plainpass")


# --- 1. nav counts ------------------------------------------------------------------


def test_nav_counts_match_the_stats_and_respect_a_private_root(client, session_factory, db_session, tmp_path):
    _two_roots(db_session, tmp_path)
    mine = client.get("/api/library/stats/nav-counts")
    assert mine.status_code == 200
    assert mine.json() == client.get("/api/library/stats").json()["nav_counts"]
    assert set(mine.json()) == {"artists", "albums", "songs", "videos", "playlists", "radio_stations", "want_list"}
    assert mine.json()["songs"] == 3  # a private root is the admin's only once granted

    plain_user, plain = _plain(db_session, session_factory, client)
    theirs = plain.get("/api/library/stats/nav-counts").json()
    assert (theirs["songs"], theirs["albums"]) == (3, 2)
    assert theirs == plain.get("/api/library/stats").json()["nav_counts"]
    private_id = db_session.scalars(select(LibraryRoot.id).where(LibraryRoot.display_name == "Private")).one()
    assert client.post(f"/api/admin/roots/{private_id}/access/{plain_user.id}").status_code == 200
    assert plain.get("/api/library/stats/nav-counts").json()["songs"] == 4


def test_nav_counts_compute_nothing_else(client, db_session, tmp_path):
    _two_roots(db_session, tmp_path)
    seen, stop = _statements(db_session)
    try:
        client.get("/api/library/stats/nav-counts")
    finally:
        stop()
    # Four library counts and three per-user ones, plus the session/user
    # look-ups: no genre, format, decade or listening aggregates.
    assert not any("genre" in s.lower() for s in seen)
    assert not any("listen_events" in s for s in seen)


# --- 2. album summaries ----------------------------------------------------------


def test_album_summary_is_the_list_item(client, db_session, tmp_path):
    _two_roots(db_session, tmp_path)
    listed = {(a["artist"], a["album"]): a for a in client.get("/api/library/albums", params={"full": 1}).json()}
    for (artist, album), item in listed.items():
        params = {"album": album} if artist is None else {"album": album, "artist": artist}
        response = client.get("/api/library/album-summaries", params=params)
        assert response.status_code == 200
        assert response.json() == [item]
    assert ("A", "X") in listed and (None, "Nameless") in listed
    assert listed[("A", "X")]["track_count"] == 2
    assert client.get("/api/library/album-summaries", params={"album": "X", "artist": "Nobody"}).json() == []
    assert client.get("/api/library/album-summaries", params={"album": "X"}).json() == []
    assert client.get("/api/library/album-summaries").status_code == 422


def test_album_summary_respects_root_access(client, session_factory, db_session, tmp_path):
    _two_roots(db_session, tmp_path)
    plain_user, plain = _plain(db_session, session_factory, client)
    assert plain.get("/api/library/album-summaries", params={"album": "Y", "artist": "B"}).json() == []
    private_id = db_session.scalars(select(LibraryRoot.id).where(LibraryRoot.display_name == "Private")).one()
    assert client.post(f"/api/admin/roots/{private_id}/access/{plain_user.id}").status_code == 200
    granted = plain.get("/api/library/album-summaries", params={"album": "Y", "artist": "B"}).json()
    assert [(a["artist"], a["album"], a["track_count"]) for a in granted] == [("B", "Y", 1)]
    assert plain.get("/api/library/album-summaries", params={"album": "X", "artist": "A"}).json()[0]["album"] == "X"


# --- 3. want list: background re-match -----------------------------------------------


def _want_root(db_session) -> LibraryRoot:
    root = LibraryRoot(path="/lib", display_name="lib")
    db_session.add(root)
    db_session.commit()
    return root


def _track(db_session, root, artist, title) -> MediaItem:
    item = MediaItem(
        library_root_id=root.id, kind=MediaKind.track, relative_path=f"{artist}-{title}.mp3", title=title, artist=artist
    )
    db_session.add(item)
    db_session.commit()
    return item


def test_want_list_pages_rematch_in_the_background_and_converge(client, db_session):
    root = _want_root(db_session)
    client.post("/api/radio/want-list", json={"artist": "Kalabi", "title": "Inside The Sun"})
    _track(db_session, root, "Kalabi", "Inside The Sun")
    bump_library_version()
    threads: list[str] = []
    gate = threading.Event()

    def record_thread(*args, **kwargs):
        threads.append(threading.current_thread().name)
        gate.wait(5)  # held until the reads below have answered
        return match_owned(*args, **kwargs)

    with patch("app.want_list.match_owned", side_effect=record_thread):
        first = client.get("/api/want-list/artists")
        assert first.headers.get("X-Want-List-Matching") == "1"
        assert first.json()[0]["owned_count"] == 0  # as last matched
        detail = client.get("/api/want-list/artist", params={"name": "Kalabi"}).json()
        assert detail["matching"] is True and detail["pending"] is False
        gate.set()
        wait_for_rematches()
    assert threads == ["want-list-match"]  # once, and never on the request thread
    settled = client.get("/api/want-list/artists")
    assert "X-Want-List-Matching" not in settled.headers
    assert settled.json()[0]["owned_count"] == 1
    assert client.get("/api/want-list/artist", params={"name": "Kalabi"}).json()["matching"] is False
    assert "X-Want-List-Matching" not in client.get("/api/want-list/entries").headers


def test_match_status_reports_a_running_rematch(client, db_session):
    root = _want_root(db_session)
    client.post("/api/radio/want-list", json={"artist": "Kalabi", "title": "Inside The Sun"})
    assert client.get("/api/want-list/match-status").json() == {"refreshing": False}
    _track(db_session, root, "Kalabi", "Inside The Sun")
    bump_library_version()
    gate = threading.Event()

    def held(*args, **kwargs):
        gate.wait(5)
        return match_owned(*args, **kwargs)

    with patch("app.want_list.match_owned", side_effect=held):
        assert client.get("/api/want-list/entries").headers.get("X-Want-List-Matching") == "1"
        assert client.get("/api/want-list/match-status").json() == {"refreshing": True}
        assert client.get("/api/want-list/match-status", params={"all_users": True}).json() == {"refreshing": True}
        gate.set()
        wait_for_rematches()
    assert client.get("/api/want-list/match-status").json() == {"refreshing": False}
    assert client.get("/api/want-list/entries").json()[0]["media_item_id"] is not None


def test_want_artist_summaries_query_count_does_not_grow_with_rows(client, db_session):
    root = _want_root(db_session)

    def count_for(artists: list[str]) -> int:
        for artist in artists:
            _track(db_session, root, artist, "Owned")
            client.post("/api/radio/want-list", json={"artist": artist, "title": "Wanted"})
        rebuild_all_credits(db_session)
        client.get("/api/want-list/artists")
        wait_for_rematches()
        seen, stop = _statements(db_session)
        try:
            rows = client.get("/api/want-list/artists").json()
        finally:
            stop()
        assert all(row["in_library"] and row["representative_media_item_id"] for row in rows)
        return len(seen)

    few = count_for(["A1", "A2"])
    many = count_for(["B1", "B2", "B3", "B4", "B5", "B6"])
    assert many == few


def test_want_artist_summary_keeps_library_artist_rules(client, session_factory, db_session):
    """An artist only in a root the user cannot see is not "in library";
    the representative is the lowest accessible media id."""
    shared = _want_root(db_session)
    private = LibraryRoot(path="/private", display_name="private", is_shared=False)
    db_session.add(private)
    db_session.commit()
    _track(db_session, private, "Hidden", "Song")
    first = _track(db_session, shared, "Seen", "One")
    _track(db_session, shared, "Seen", "Two")
    rebuild_all_credits(db_session)
    _user, plain = _plain(db_session, session_factory, client)
    plain.post("/api/radio/want-list", json={"artist": "Hidden", "title": "Other"})
    plain.post("/api/radio/want-list", json={"artist": "Seen", "title": "Other"})
    rows = {row["artist"]: row for row in plain.get("/api/want-list/artists").json()}
    assert rows["Hidden"]["in_library"] is False and rows["Hidden"]["representative_media_item_id"] is None
    assert rows["Seen"]["in_library"] is True and rows["Seen"]["representative_media_item_id"] == first.id
    assert rows["Seen"]["library_name"] == "Seen"


# --- 4. outside services: answer from the store, fetch in the background --------------


class _Result:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error


def _no_network(*_args, **_kwargs):
    raise AssertionError("an outside service was called on the request thread")


@pytest.fixture()
def queued(monkeypatch):
    """The jobs app.deferred queues, by key, so a test can run them."""
    jobs: dict[str, object] = {}

    def submit(key, job, **_):
        jobs[key] = job
        return True

    monkeypatch.setattr("app.provider_queue.submit_interactive", submit)
    return jobs


def _library(db_session):
    root = LibraryRoot(path="/m", display_name="m")
    db_session.add(root)
    db_session.commit()
    item = MediaItem(
        library_root_id=root.id, relative_path="0.mp3", title="Seed Song", artist="Seed Band", album="Seed LP"
    )
    db_session.add(item)
    db_session.commit()
    rebuild_all_credits(db_session)
    return item


@pytest.mark.deferred_fetches
def test_artist_info_answers_from_the_store_and_fills_it_in_the_background(client, db_session, queued):
    _library(db_session)
    with (
        patch("app.metadata_info.musicbrainz.search_artists", side_effect=_no_network),
        patch("app.metadata_info.musicbrainz.lookup_artist", side_effect=_no_network),
        patch("app.listenbrainz_data.listenbrainz.artist_popularity", side_effect=_no_network),
        patch("app.metadata_info.wikipedia.summary_from_links", side_effect=_no_network),
    ):
        body = client.get("/api/library/artist-info", params={"name": "Seed Band"}).json()
    assert body["pending"] is True and body["name"] == "Seed Band" and body["bio"] is None
    (key,) = queued
    search = {"artists": [{"id": "00000004-0000-4000-8000-000000000004", "name": "Seed Band", "score": 100}]}
    mb = {"id": "00000004-0000-4000-8000-000000000004", "type": "Group", "annotation": "Long bio"}
    with (
        patch("app.metadata_info.musicbrainz.search_artists", return_value=_Result(search)),
        patch("app.metadata_info.musicbrainz.lookup_artist", return_value=_Result(mb)),
        patch(
            "app.listenbrainz_data.listenbrainz.artist_popularity",
            return_value={"00000004-0000-4000-8000-000000000004": {"listeners": 7, "playcount": 9}},
        ),
    ):
        queued.pop(key)()  # the background job, on its own session
    with (
        patch("app.metadata_info.musicbrainz.search_artists", side_effect=_no_network),
        patch("app.metadata_info.musicbrainz.lookup_artist", side_effect=_no_network),
        patch("app.listenbrainz_data.listenbrainz.artist_popularity", side_effect=_no_network),
    ):
        again = client.get("/api/library/artist-info", params={"name": "Seed Band"}).json()
    assert again["pending"] is False
    assert again["bio"]["text"] == "Long bio" and again["type"] == "Group" and again["listeners"] == 7
    assert not queued


@pytest.mark.deferred_fetches
def test_a_failed_background_lookup_stops_the_pending_answer(client, db_session, queued):
    _library(db_session)
    with patch("app.metadata_info.musicbrainz.search_artists", side_effect=_no_network):
        assert client.get("/api/library/artist-info", params={"name": "Seed Band"}).json()["pending"] is True
    (key,) = queued
    with patch("app.metadata_info.musicbrainz.search_artists", return_value=_Result(error="timeout")):
        queued.pop(key)()
    with patch("app.metadata_info.musicbrainz.search_artists", side_effect=_no_network):
        body = client.get("/api/library/artist-info", params={"name": "Seed Band"}).json()
    assert body["pending"] is False  # tried just now: not queued again
    assert not queued


@pytest.mark.deferred_fetches
def test_album_info_and_top_tracks_answer_from_the_store(client, db_session, queued):
    from app.models import Album, Artist

    _library(db_session)
    from app.albums import rebuild_albums

    rebuild_albums(db_session)
    album_row = db_session.query(Album).one()
    album_row.release_group_mbid = "00000008-0000-4000-8000-000000000008"
    db_session.query(Artist).filter_by(name="Seed Band").one().mbid = "00000004-0000-4000-8000-000000000004"
    db_session.commit()
    # Top recordings are asked only with a linked ListenBrainz token.
    from app import scrobbling

    with patch("app.scrobbling.listenbrainz.validate_token", return_value="me"):
        assert scrobbling.link_listenbrainz(db_session, _me(db_session), "TOKEN") is None
    with (
        patch("app.listenbrainz_data.listenbrainz.release_group_popularity", side_effect=_no_network),
        patch("app.metadata_info.musicbrainz.lookup_release_group", side_effect=_no_network),
        patch("app.listenbrainz_data.listenbrainz.top_recordings_for_artist", side_effect=_no_network),
    ):
        album = client.get("/api/library/album-info", params={"album": "Seed LP", "artist": "Seed Band"}).json()
        top = client.get("/api/library/artist-top-tracks", params={"name": "Seed Band"}).json()
    assert album["pending"] is True and album["listeners"] is None
    assert top == {"available": True, "pending": True, "tracks": []}
    assert len(queued) == 2
    top_rows = [{"title": "Seed Song", "mbid": None, "playcount": 900, "listeners": 50, "rank": 1}]
    with (
        patch(
            "app.listenbrainz_data.listenbrainz.release_group_popularity",
            return_value={"00000008-0000-4000-8000-000000000008": {"listeners": 33, "playcount": 99}},
        ),
        patch("app.metadata_info.musicbrainz.lookup_release_group", return_value=_Result({"id": "00000008-0000-4000-8000-000000000008"})),
        patch("app.listenbrainz_data.listenbrainz.top_recordings_for_artist", return_value=top_rows),
    ):
        for key in list(queued):
            queued.pop(key)()
    with (
        patch("app.listenbrainz_data.listenbrainz.release_group_popularity", side_effect=_no_network),
        patch("app.metadata_info.musicbrainz.lookup_release_group", side_effect=_no_network),
        patch("app.listenbrainz_data.listenbrainz.top_recordings_for_artist", side_effect=_no_network),
    ):
        album = client.get("/api/library/album-info", params={"album": "Seed LP", "artist": "Seed Band"}).json()
        top = client.get("/api/library/artist-top-tracks", params={"name": "Seed Band"}).json()
    assert album["pending"] is False and album["listeners"] == 33
    assert top["pending"] is False and top["tracks"][0]["media_item"]["title"] == "Seed Song"


@pytest.mark.deferred_fetches
def test_track_info_answers_from_the_store_and_fills_counts_in_the_background(client, db_session, queued):
    item = _library(db_session)
    item.musicbrainz_recording_mbid = "00000001-0000-4000-8000-000000000001"
    db_session.commit()
    with patch("app.listenbrainz_data.listenbrainz.recording_popularity", side_effect=_no_network):
        first = client.get(f"/api/library/media/{item.id}/info").json()
    assert first["pending"] is True and first["listeners"] is None
    (key,) = queued
    with patch(
        "app.listenbrainz_data.listenbrainz.recording_popularity",
        return_value={"00000001-0000-4000-8000-000000000001": {"listeners": 5, "playcount": 6}},
    ):
        queued.pop(key)()
    with patch("app.listenbrainz_data.listenbrainz.recording_popularity", side_effect=_no_network):
        again = client.get(f"/api/library/media/{item.id}/info").json()
    assert again["pending"] is False and (again["listeners"], again["playcount"]) == (5, 6)
    # The old `light` flag is accepted and ignored.
    light = client.get(f"/api/library/media/{item.id}/info", params={"light": True}).json()
    assert light["listeners"] == 5


@pytest.mark.deferred_fetches
def test_lyrics_first_lookup_never_waits_on_lrclib(client, db_session, library_dir, queued):
    make_audio_fixture(library_dir / "song.wav", title="Song", artist="Band", album=None, genre=None)
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    scan_library_root(db_session, root)
    item = db_session.query(MediaItem).one()
    with patch("app.enrichment.lyrics.fetch_lrclib_lyrics", side_effect=_no_network):
        first = client.get(f"/api/library/media/{item.id}/lyrics").json()
    assert first == {"plain": None, "synced": None, "source": None, "pending": True}
    (key,) = queued
    with patch("app.enrichment.lyrics.fetch_lrclib_lyrics", return_value={"plain": "Words", "synced": None}):
        queued.pop(key)()
    with patch("app.enrichment.lyrics.fetch_lrclib_lyrics", side_effect=_no_network):
        again = client.get(f"/api/library/media/{item.id}/lyrics").json()
    assert again == {"plain": "Words", "synced": None, "source": "cached", "pending": False}


CATALOG_ARTIST = "00000000-0000-4000-8000-0000000000c1"
CATALOG_GROUP = "00000000-0000-4000-8000-0000000000c2"


@pytest.mark.deferred_fetches
def test_a_first_catalog_look_answers_at_once_and_fills_in(client, db_session, queued):
    with (
        patch("app.catalog.musicbrainz.browse_release_groups", side_effect=_no_network),
        patch("app.metadata_info.musicbrainz.lookup_release_group", side_effect=_no_network),
    ):
        artist = client.get("/api/catalog/artist", params={"mbid": CATALOG_ARTIST, "name": "Far Band"})
        album = client.get(f"/api/catalog/release-group/{CATALOG_GROUP}", params={"artist": "Far Band"})
    assert artist.status_code == 200 and album.status_code == 200
    artist_body, album_body = artist.json(), album.json()
    assert artist_body["pending"] is True and artist_body["refreshing"] is True
    assert artist_body["release_groups"] == [] and artist_body["artist_name"] == "Far Band"
    assert album_body["pending"] is True and album_body["tracks"] == [] and album_body["artist"] == "Far Band"
    # The page's existing poll (refresh-status) sees the job's key.
    assert set(queued) == {f"refresh-artist:{CATALOG_ARTIST}", f"catalog-album:{CATALOG_GROUP}"}
    groups = {"release-groups": [{"id": CATALOG_GROUP, "title": "Debut", "primary-type": "Album"}]}
    group = {"id": CATALOG_GROUP, "title": "Debut", "primary-type": "Album", "releases": []}
    with (
        patch("app.catalog.musicbrainz.browse_release_groups", return_value=_Result(groups)),
        patch("app.metadata_info.musicbrainz.lookup_release_group", return_value=_Result(group)),
    ):
        for key in list(queued):
            queued.pop(key)()
    with (
        patch("app.catalog.musicbrainz.browse_release_groups", side_effect=_no_network),
        patch("app.metadata_info.musicbrainz.lookup_release_group", side_effect=_no_network),
        patch("app.want_metadata.provider_queue.submit", return_value=True),
    ):
        artist_body = client.get("/api/catalog/artist", params={"mbid": CATALOG_ARTIST, "name": "Far Band"}).json()
        album_body = client.get(f"/api/catalog/release-group/{CATALOG_GROUP}", params={"artist": "Far Band"}).json()
    assert artist_body["pending"] is False and [g["title"] for g in artist_body["release_groups"]] == ["Debut"]
    assert album_body["pending"] is False and album_body["title"] == "Debut"


@pytest.mark.deferred_fetches
def test_a_catalog_lookup_that_failed_still_says_so(client, db_session, queued, monkeypatch):
    with patch("app.catalog.musicbrainz.browse_release_groups", side_effect=_no_network):
        client.get("/api/catalog/artist", params={"mbid": CATALOG_ARTIST})
    with patch("app.catalog.musicbrainz.browse_release_groups", return_value=_Result(error="down")):
        queued.pop(f"refresh-artist:{CATALOG_ARTIST}")()
    response = client.get("/api/catalog/artist", params={"mbid": CATALOG_ARTIST})
    assert response.status_code == 502


@pytest.mark.deferred_fetches
def test_deferred_jobs_go_on_the_interactive_lane(client, db_session, monkeypatch):
    _library(db_session)
    lanes: list[str] = []
    monkeypatch.setattr("app.provider_queue.submit_interactive", lambda key, job, **_: lanes.append(key) or True)
    monkeypatch.setattr("app.provider_queue.submit", lambda key, job, **_: pytest.fail("bulk lane used"))
    with patch("app.metadata_info.musicbrainz.search_artists", side_effect=_no_network):
        client.get("/api/library/artist-info", params={"name": "Seed Band"})
        client.get("/api/library/artist-info", params={"name": "Seed Band"})
    assert len(lanes) == 2  # the no-op submit keeps no key: queued each time
    assert lanes[0].startswith("artist-info:")


# --- 5. radio stations ----------------------------------------------------------------


def test_radio_station_list_reads_favourites_in_one_query(client, db_session):
    user = _me(db_session)
    db_session.add(RadioStation(name="First", stream_url="https://radio.example/first", is_shared=True))
    db_session.commit()

    def count() -> int:
        seen, stop = _statements(db_session)
        try:
            stations = client.get("/api/radio/stations").json()
        finally:
            stop()
        assert stations
        return len(seen)

    before = count()
    for n in range(8):
        station = RadioStation(name=f"S{n}", stream_url=f"https://radio.example/{n}", is_shared=True)
        db_session.add(station)
        db_session.commit()
        if n % 2:
            db_session.add(RadioStationFavorite(user_id=user.id, station_id=station.id))
            db_session.commit()
    assert count() == before
    stations = {s["name"]: s for s in client.get("/api/radio/stations").json()}
    assert [stations[f"S{n}"]["is_favorite"] for n in range(8)] == [n % 2 == 1 for n in range(8)]


# --- 6. recently played -----------------------------------------------------------------


def test_recently_played_matches_grouping_the_whole_history(client, session_factory, db_session, monkeypatch):
    from app.routers.library import history

    # Tiny windows, so the scan crosses several of them.
    monkeypatch.setattr(history, "RECENT_PLAYS_MIN_WINDOW", 3)
    monkeypatch.setattr(history, "RECENT_PLAYS_WINDOW_FACTOR", 1)
    shared = _want_root(db_session)
    private = LibraryRoot(path="/private", display_name="private", is_shared=False)
    db_session.add(private)
    db_session.commit()
    items = [_track(db_session, shared, "A", f"T{n}") for n in range(12)]
    hidden = _track(db_session, private, "B", "Hidden")
    video = MediaItem(library_root_id=shared.id, kind=MediaKind.video, relative_path="v.mp4", title="V")
    db_session.add(video)
    db_session.commit()
    user, plain = _plain(db_session, session_factory, client)
    base = datetime(2026, 1, 1)
    plays = [(items[n % 12], base + timedelta(minutes=(n * 37) % 401)) for n in range(60)]
    plays += [(hidden, base + timedelta(days=1)), (video, base + timedelta(days=2))]
    for item, when in plays:
        db_session.add(PlayHistory(user_id=user.id, media_item_id=item.id, played_at=when))
    db_session.commit()
    last: dict[int, datetime] = {}
    for item, when in plays:
        if item.kind == MediaKind.track and item.library_root_id == shared.id:
            last[item.id] = max(last.get(item.id, when), when)
    expected = sorted(last, key=lambda media_id: last[media_id], reverse=True)
    for limit in (1, 5, 12, 25):
        got = [row["id"] for row in plain.get("/api/library/recently-played", params={"limit": limit}).json()]
        assert got == expected[:limit]


def test_recently_played_sources_look_up_mixes_in_one_query(client, db_session):
    user = _me(db_session)

    def add_sources(count: int) -> None:
        for n in range(count):
            mix = Playlist(user_id=user.id, name=f"Mix {n}", auto_kind="mix")
            db_session.add(mix)
            db_session.commit()
            db_session.add(
                PlaySourceHistory(
                    user_id=user.id, kind="playlist", label=f"Mix {n}",
                    payload_json={"playlist_id": mix.id, "auto_kind": "mix"}, updated_at=datetime(2026, 1, 1, 0, n),
                )
            )
            db_session.commit()

    def count() -> int:
        seen, stop = _statements(db_session)
        try:
            client.get("/api/library/recently-played-sources")
        finally:
            stop()
        return len(seen)

    add_sources(2)
    few = count()
    add_sources(6)
    assert count() == few
    # A card whose mix is gone is still dropped.
    gone = db_session.scalars(select(Playlist).where(Playlist.name == "Mix 0")).first()
    db_session.delete(gone)
    db_session.commit()
    labels = [row["label"] for row in client.get("/api/library/recently-played-sources").json()]
    assert len(labels) == 7 and labels.count("Mix 0") == 1  # the other "Mix 0" (second batch) stays


# --- 7. playlist paging -------------------------------------------------------------------


def test_playlist_detail_pages_on_request_and_counts_everything(client, db_session):
    root = _want_root(db_session)
    ids = [_track(db_session, root, "A", f"T{n}").id for n in range(7)]
    playlist_id = client.post("/api/playlists", json={"name": "Big"}).json()["id"]
    client.post(f"/api/playlists/{playlist_id}/items", json={"media_item_ids": ids})
    whole = client.get(f"/api/playlists/{playlist_id}").json()
    assert [item["id"] for item in whole["items"]] == ids and whole["total_count"] == 7
    page = client.get(f"/api/playlists/{playlist_id}", params={"limit": 3, "offset": 2}).json()
    assert [item["id"] for item in page["items"]] == ids[2:5] and page["total_count"] == 7
    assert [i["playlist_item_id"] for i in page["items"]] == [i["playlist_item_id"] for i in whole["items"][2:5]]
    tail = client.get(f"/api/playlists/{playlist_id}", params={"offset": 5}).json()
    assert [item["id"] for item in tail["items"]] == ids[5:]
    assert client.get(f"/api/playlists/{playlist_id}", params={"limit": 0}).status_code == 422


# --- the in-memory stores stay bounded ------------------------------------------------


def test_recently_tried_keys_are_bounded(monkeypatch):
    from app import deferred

    monkeypatch.setattr(deferred, "MAX_REMEMBERED", 3)
    for n in range(5):
        deferred._mark_tried(f"k{n}")
    assert list(deferred._tried) == ["k2", "k3", "k4"]
    assert deferred._recently_tried("k4") and not deferred._recently_tried("k0")
    monkeypatch.setattr(deferred, "RETRY_AFTER", 0.0)
    assert not deferred._recently_tried("k4")


def test_answer_from_store_without_deferring_computes_inline():
    from app import deferred

    # Tests run with DEFER off (tests/conftest.py): the computation fetches.
    assert deferred.DEFER is False
    answer, pending = deferred.answer_from_store("k", lambda: {"x": 1}, lambda: None)
    assert (answer, pending) == ({"x": 1}, False)
    assert deferred.defer_fetch() is False  # outside a deferring request


def test_a_refused_deferred_job_answers_from_the_store_and_is_not_pending(monkeypatch):
    """The interactive lane is full (or the user has too many waiting): the
    stored answer comes back, not pending, so the client does not poll; the
    next ask tries to queue again."""
    from app import deferred

    monkeypatch.setattr(deferred, "DEFER", True)
    asked: list[tuple[str, int | None]] = []

    def refuse(key, job, *, user_id=None):
        asked.append((key, user_id))
        return False

    monkeypatch.setattr("app.provider_queue.submit_interactive", refuse)

    def compute():
        deferred.defer_fetch()
        return {"stored": 1}

    assert deferred.answer_from_store("refused-key", compute, lambda: None, user_id=5) == ({"stored": 1}, False)
    assert deferred.answer_with_pending("refused-key", compute, lambda: None, user_id=5) == {
        "stored": 1,
        "pending": False,
    }
    assert asked == [("refused-key", 5), ("refused-key", 5)]  # not remembered as tried


def test_a_deferred_job_queued_meanwhile_under_the_same_key_is_pending(monkeypatch):
    from app import deferred

    monkeypatch.setattr(deferred, "DEFER", True)
    states = iter([False, True])  # not pending when checked, pending once submit refused it
    monkeypatch.setattr("app.provider_queue.pending", lambda key: next(states))
    monkeypatch.setattr("app.provider_queue.submit_interactive", lambda key, job, **_: False)

    def compute():
        deferred.defer_fetch()
        return {}

    assert deferred.answer_from_store("raced-key", compute, lambda: None) == ({}, True)


@pytest.mark.deferred_fetches
def test_deferred_jobs_carry_the_asking_user(client, db_session, monkeypatch):
    _library(db_session)
    users: list[int | None] = []
    monkeypatch.setattr(
        "app.provider_queue.submit_interactive", lambda key, job, *, user_id=None: users.append(user_id) or True
    )
    with patch("app.metadata_info.musicbrainz.search_artists", side_effect=_no_network):
        client.get("/api/library/artist-info", params={"name": "Seed Band"})
        client.get("/api/catalog/artist", params={"mbid": CATALOG_ARTIST})
    assert users == [_me(db_session).id] * 2


@pytest.mark.deferred_fetches
def test_a_full_lookup_queue_degrades_without_a_server_error(client, db_session, monkeypatch):
    """Refused look-ups: stored-only answers with `pending` false; a catalog
    page with nothing stored gets the same 502 as a provider outage."""
    _library(db_session)
    monkeypatch.setattr("app.provider_queue.submit_interactive", lambda key, job, **_: False)
    with (
        patch("app.metadata_info.musicbrainz.search_artists", side_effect=_no_network),
        patch("app.catalog.musicbrainz.browse_release_groups", side_effect=_no_network),
    ):
        info = client.get("/api/library/artist-info", params={"name": "Seed Band"})
        catalog = client.get("/api/catalog/artist", params={"mbid": CATALOG_ARTIST})
    assert info.status_code == 200 and info.json()["pending"] is False
    assert catalog.status_code == 502
