from __future__ import annotations

from sqlalchemy import text

from app.config import settings
from app.metadata_taxonomy import (
    clean_names,
    replace_item_genres,
    replace_item_musicbrainz_tags,
)
from app.models import (
    IgnoredLibraryFolder,
    LibraryRoot,
    MediaItem,
    Playlist,
    PlaylistItem,
    PlaySourceHistory,
    Tag,
)
from app.scanner import scan_library_root
from tests.conftest import make_audio_fixture, played_now, wait_until
from tests.test_admin import _settled_last_task


def _scan_sample_library(db_session, library_dir):
    make_audio_fixture(
        library_dir / "a.wav", title="Alpha", artist="Band One", album="LP", genre="Rock"
    )
    make_audio_fixture(
        library_dir / "b.wav", title="Beta", artist="Band Two", album="EP", genre="Jazz"
    )
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    return root


def test_list_tracks_returns_all(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    resp = client.get("/api/library/tracks")
    assert resp.status_code == 200
    titles = {t["title"] for t in resp.json()}
    assert titles == {"Alpha", "Beta"}


def test_search_filters_by_query(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    resp = client.get("/api/library/tracks", params={"q": "Alpha"})
    titles = [t["title"] for t in resp.json()]
    assert titles == ["Alpha"]


def test_filter_by_genre(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    resp = client.get("/api/library/tracks", params={"genre": "Jazz"})
    titles = [t["title"] for t in resp.json()]
    assert titles == ["Beta"]


def test_list_tracks_paginates(client, db_session, library_dir):
    for i, title in enumerate(["Alpha", "Bravo", "Charlie", "Delta", "Echo"], start=1):
        make_audio_fixture(
            library_dir / f"t{i}.wav",
            title=title,
            artist="Same Artist",
            album="Same Album",
            genre="Rock",
            track_number=i,
        )
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)

    page1 = client.get("/api/library/tracks", params={"limit": 2, "offset": 0}).json()
    page2 = client.get("/api/library/tracks", params={"limit": 2, "offset": 2}).json()
    page3 = client.get("/api/library/tracks", params={"limit": 2, "offset": 4}).json()

    assert [t["title"] for t in page1] == ["Alpha", "Bravo"]
    assert [t["title"] for t in page2] == ["Charlie", "Delta"]
    assert [t["title"] for t in page3] == ["Echo"]

    # Unpaginated (default limit) still returns everything, in the same order.
    all_tracks = client.get("/api/library/tracks").json()
    assert [t["title"] for t in all_tracks] == ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]


def test_list_genres_is_sorted_and_distinct(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    resp = client.get("/api/library/genres")
    assert resp.json() == ["Jazz", "Rock"]


def test_mix_options_report_genre_track_counts(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    resp = client.get("/api/library/mixes/options")
    assert resp.status_code == 200
    by_genre = {row["name"]: row["track_count"] for row in resp.json() if row["kind"] == "genre"}
    assert by_genre == {"Jazz": 1, "Rock": 1}


def test_mix_options_and_specified_mix_include_genres_and_tags(client, db_session, library_dir, monkeypatch):
    # The sample files are one second long; mixes leave out tracks shorter
    # than mix_min_track_seconds, which is not what this test is about.
    monkeypatch.setattr(settings, "mix_min_track_seconds", 0)
    _scan_sample_library(db_session, library_dir)
    rock = db_session.query(MediaItem).filter(MediaItem.genre == "Rock").one()
    tag = Tag(name="Workout")
    tag.media_items.append(rock)
    db_session.add(tag)
    db_session.commit()

    options = client.get("/api/library/mixes/options")
    assert options.status_code == 200
    assert {(row["kind"], row["name"]) for row in options.json()} == {
        ("genre", "Rock"), ("genre", "Jazz"), ("tag", "Workout")
    }

    specified = client.post(
        "/api/library/mixes/specified", json={"kind": "tag", "name": "Workout"}
    )
    assert specified.status_code == 200
    assert specified.json()[-1]["genre"] == "Workout"
    assert specified.json()[-1]["name"] == "Workout Mix"
    # Home needs to know this one is the user's temporary extra, not one of
    # the featured mixes it caps at 13.
    assert specified.json()[-1]["is_specified"] is True

    invalid = client.post(
        "/api/library/mixes/specified", json={"kind": "genre", "name": "Missing"}
    )
    assert invalid.status_code == 422


def test_list_mixes_includes_preview_ids(client, db_session, library_dir):
    """GET /api/library/mixes serves the materialized collage snapshot so
    Home never needs a per-mix playlist fetch."""
    _scan_sample_library(db_session, library_dir)
    tracks = db_session.query(MediaItem).filter(MediaItem.kind == "track").all()
    mix = Playlist(
        user_id=1,
        name="Rock Mix",
        is_auto_generated=True,
        auto_kind="mix",
        mix_genre="Rock",
        preview_media_item_ids_json=[tracks[0].id, tracks[1].id],
    )
    # A mix that predates the snapshot (or an empty one) is legal: it must
    # come back with an empty preview list, not a 500.
    legacy = Playlist(
        user_id=1,
        name="Jazz Mix",
        is_auto_generated=True,
        auto_kind="mix",
        mix_genre="Jazz",
        preview_media_item_ids_json=None,
    )
    # Corrupt JSON must degrade gracefully to [] as well.
    corrupt = Playlist(
        user_id=1,
        name="Corrupt Mix",
        is_auto_generated=True,
        auto_kind="mix",
        mix_genre="specified:Corrupt",
    )
    # Valid JSON that isn't a list of ints must degrade the same way.
    wrong_shape = Playlist(
        user_id=1,
        name="Wrong Shape Mix",
        is_auto_generated=True,
        auto_kind="mix",
        mix_genre="specified:Wrong Shape",
        preview_media_item_ids_json=[tracks[0].id, "not-an-int"],
    )
    db_session.add_all([mix, legacy, corrupt, wrong_shape])
    db_session.commit()
    # Stored text the column cannot decode (written behind the ORM's back).
    db_session.execute(
        text("UPDATE playlists SET preview_media_item_ids_json = 'not-json{' WHERE id = :id"),
        {"id": corrupt.id},
    )
    db_session.commit()

    resp = client.get("/api/library/mixes")
    assert resp.status_code == 200
    by_genre = {row["genre"]: row for row in resp.json()}
    assert by_genre["Rock"]["preview_media_item_ids"] == [tracks[0].id, tracks[1].id]
    assert by_genre["Jazz"]["preview_media_item_ids"] == []
    assert by_genre["Corrupt"]["preview_media_item_ids"] == []
    assert by_genre["Wrong Shape"]["preview_media_item_ids"] == []


def test_list_mixes_flags_only_specified_mixes(client, db_session, library_dir):
    """Home tells the featured mixes apart from the temporary ones the user
    asked for by name - only the latter may exceed its 13-item rail cap."""
    _scan_sample_library(db_session, library_dir)
    featured = Playlist(
        user_id=1,
        name="Rock Mix",
        is_auto_generated=True,
        auto_kind="mix",
        mix_genre="Rock",
    )
    specified = Playlist(
        user_id=1,
        name="Workout Mix",
        is_auto_generated=True,
        auto_kind="mix",
        mix_genre="specified:tag:Workout",
    )
    db_session.add_all([featured, specified])
    db_session.commit()

    resp = client.get("/api/library/mixes")
    assert resp.status_code == 200
    rows = {row["genre"]: row for row in resp.json()}
    assert rows["Rock"]["is_specified"] is False
    assert rows["Workout"]["is_specified"] is True
    # The specified mix sorts after the featured one, which is what lets Home
    # keep its layout while rendering it on top of the cap.
    assert resp.json()[-1]["genre"] == "Workout"


def test_mix_options_and_specified_mix_include_all_musicbrainz_genres_and_tags(
    client, db_session, library_dir, monkeypatch
):
    # The sample files are one second long; mixes leave out tracks shorter
    # than mix_min_track_seconds, which is not what this test is about.
    monkeypatch.setattr(settings, "mix_min_track_seconds", 0)
    root = _scan_sample_library(db_session, library_dir)
    item = MediaItem(
        library_root_id=root.id,
        relative_path="multi.mp3",
        title="Multi-genre track",
        kind="track",
        genre="Alternative",
    )
    db_session.add(item)
    db_session.flush()
    replace_item_genres(db_session, item, ["Alternative", "Rock"], "musicbrainz")
    replace_item_musicbrainz_tags(db_session, item, [("Driving", 9)])
    db_session.commit()

    options = client.get("/api/library/mixes/options").json()
    counts = {(row["kind"], row["name"]): row["track_count"] for row in options}
    assert counts[("genre", "Rock")] == 2
    assert counts[("genre", "Alternative")] == 1
    assert counts[("tag", "Driving")] == 1

    # The secondary genre is also usable when a track has no MusicBrainz
    # result.  These endpoints inspect more than MediaItem.genre, which
    # holds only the first embedded/provider genre.
    filtered = client.get("/api/library/tracks", params={"genre": "Rock"}).json()
    assert {track["title"] for track in filtered} == {"Alpha", "Multi-genre track"}

    mixes = client.post(
        "/api/library/mixes/specified", json={"kind": "genre", "name": "Rock"}
    ).json()
    playlist = client.get(f"/api/playlists/{mixes[-1]['playlist_id']}").json()
    assert {track["title"] for track in playlist["items"]} == {"Alpha", "Multi-genre track"}


def test_ignored_folder_is_hidden_from_mix_options_but_tracks_remain_listenable(
    client, db_session, library_dir
):
    root = _scan_sample_library(db_session, library_dir)
    custom_dir = library_dir / "Custom"
    custom_dir.mkdir()
    make_audio_fixture(
        custom_dir / "local.wav",
        title="Custom recording",
        artist="Local artist",
        album="Local album",
        genre="Uncatalogued",
    )
    scan_library_root(db_session, root)
    db_session.add(IgnoredLibraryFolder(library_root_id=root.id, relative_path="Custom"))
    db_session.commit()

    assert client.get("/api/library/genres").json() == ["Jazz", "Rock"]
    assert {
        item["name"]
        for item in client.get("/api/library/mixes/options").json()
        if item["kind"] == "genre"
    } == {"Jazz", "Rock"}
    assert all(
        option["name"] != "Uncatalogued"
        for option in client.get("/api/library/mixes/options").json()
    )
    tracks = client.get("/api/library/tracks").json()
    assert "Custom recording" in {item["title"] for item in tracks}


def test_clean_names_discards_non_strings_blanks_and_duplicates():
    assert clean_names([" Rock ", None, 3, "", "rock", "Alternative"]) == [
        ("Rock", "rock"),
        ("Alternative", "alternative"),
    ]


def test_recently_added_orders_newest_first(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    resp = client.get("/api/library/recently-added")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_play_source_history_upserts_and_orders_by_recency(client):
    resp = client.post(
        "/api/library/play-source",
        json={"kind": "album", "label": "A Night at the Opera", "payload": {"artist": "Queen", "album": "A Night at the Opera"}},
    )
    assert resp.status_code == 204
    resp = client.post(
        "/api/library/play-source",
        json={"kind": "radio", "label": "SomaFM", "payload": {"station_id": 1}},
    )
    assert resp.status_code == 204

    rows = client.get("/api/library/recently-played-sources").json()
    assert [r["kind"] for r in rows] == ["radio", "album"]  # most recent first

    # Playing the same album again updates the existing row (and its label)
    # rather than creating a second one.
    resp = client.post(
        "/api/library/play-source",
        json={"kind": "album", "label": "A Night at the Opera (Remaster)", "payload": {"artist": "Queen", "album": "A Night at the Opera"}},
    )
    assert resp.status_code == 204
    rows = client.get("/api/library/recently-played-sources").json()
    assert len(rows) == 2
    assert rows[0]["kind"] == "album"
    assert rows[0]["label"] == "A Night at the Opera (Remaster)"


def test_play_source_history_keeps_playlist_art_after_the_playlist_is_removed(
    client, db_session, library_dir
):
    root = _scan_sample_library(db_session, library_dir)
    track = db_session.query(MediaItem).filter_by(
        library_root_id=root.id, title="Alpha"
    ).one()
    playlist = Playlist(user_id=1, name="Temporary Mix", is_auto_generated=True)
    db_session.add(playlist)
    db_session.commit()
    db_session.add(PlaylistItem(playlist_id=playlist.id, media_item_id=track.id, position=0))
    db_session.commit()

    response = client.post(
        "/api/library/play-source",
        json={
            "kind": "playlist",
            "label": playlist.name,
            "payload": {"playlist_id": playlist.id},
        },
    )
    assert response.status_code == 204
    db_session.expire_all()
    assert db_session.query(PlaySourceHistory).one().art_media_item_id == track.id

    # Replaying an existing source refreshes its cached art while the mix
    # still exists, rather than only caching artwork on initial creation.
    response = client.post(
        "/api/library/play-source",
        json={
            "kind": "playlist",
            "label": playlist.name,
            "payload": {"playlist_id": playlist.id},
        },
    )
    assert response.status_code == 204

    db_session.delete(playlist)
    db_session.commit()

    rows = client.get("/api/library/recently-played-sources").json()
    assert rows[0]["art_media_item_id"] == track.id


def test_recent_sources_removes_missing_auto_generated_mix_but_keeps_generic_playlist(
    client, db_session
):
    mix = Playlist(user_id=1, name="Mix", is_auto_generated=True, auto_kind="mix")
    db_session.add(mix)
    db_session.commit()
    client.post("/api/library/play-source", json={"kind": "playlist", "label": "Mix", "payload": {"playlist_id": mix.id}})
    db_session.add(PlaySourceHistory(user_id=1, kind="playlist", label="My playlist", payload_json={"playlist_id": 901}))
    db_session.commit()
    db_session.delete(mix)
    db_session.commit()
    rows = client.get("/api/library/recently-played-sources").json()
    assert [row["label"] for row in rows] == ["My playlist"]


def test_library_play_source_shows_a_collage_of_recently_played_matching_tracks(
    client, db_session, library_dir
):
    _scan_sample_library(db_session, library_dir)
    tracks = {t["title"]: t["id"] for t in client.get("/api/library/tracks").json()}

    # Play both tracks so they land in PlayHistory, then record a "library"
    # (whole Songs tab) play source with no filters.
    client.post(f"/api/stream/{tracks['Alpha']}/played", json=played_now())
    client.post(f"/api/stream/{tracks['Beta']}/played", json=played_now())
    resp = client.post(
        "/api/library/play-source",
        json={"kind": "library", "label": "Songs", "payload": {"filters": {}}},
    )
    assert resp.status_code == 204

    rows = client.get("/api/library/recently-played-sources").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "library"
    assert row["art_media_item_id"] is None  # no single cover for a whole-library shuffle
    # Most recently played first, one per distinct artist.
    assert row["preview_media_item_ids"] == [tracks["Beta"], tracks["Alpha"]]


def test_library_play_source_collage_respects_the_recorded_filters(
    client, db_session, library_dir
):
    _scan_sample_library(db_session, library_dir)
    tracks = {t["title"]: t["id"] for t in client.get("/api/library/tracks").json()}

    client.post(f"/api/stream/{tracks['Alpha']}/played", json=played_now())
    client.post(f"/api/stream/{tracks['Beta']}/played", json=played_now())
    resp = client.post(
        "/api/library/play-source",
        json={"kind": "library", "label": "Rock", "payload": {"filters": {"genre": "Rock"}}},
    )
    assert resp.status_code == 204

    rows = client.get("/api/library/recently-played-sources").json()
    # Beta is Jazz, so only the Rock track (Alpha) shows up in the collage.
    assert rows[0]["preview_media_item_ids"] == [tracks["Alpha"]]


def test_library_play_source_returns_stored_preview_after_history_changes(
    client, db_session, library_dir
):
    _scan_sample_library(db_session, library_dir)
    tracks = {t["title"]: t["id"] for t in client.get("/api/library/tracks").json()}

    client.post(f"/api/stream/{tracks['Alpha']}/played", json=played_now())
    client.post(
        "/api/library/play-source",
        json={"kind": "library", "label": "Songs", "payload": {"filters": {}}},
    )
    # A later play must not change the materialized preview of the earlier
    # source until that source is explicitly recorded again.
    client.post(f"/api/stream/{tracks['Beta']}/played", json=played_now())

    row = client.get("/api/library/recently-played-sources").json()[0]
    assert row["preview_media_item_ids"] == [tracks["Alpha"]]


def test_library_play_source_upsert_refreshes_stored_preview(
    client, db_session, library_dir
):
    _scan_sample_library(db_session, library_dir)
    tracks = {t["title"]: t["id"] for t in client.get("/api/library/tracks").json()}

    client.post(f"/api/stream/{tracks['Alpha']}/played", json=played_now())
    payload = {"kind": "library", "label": "Songs", "payload": {"filters": {}}}
    client.post("/api/library/play-source", json=payload)
    client.post(f"/api/stream/{tracks['Beta']}/played", json=played_now())
    client.post("/api/library/play-source", json=payload)

    history = db_session.query(PlaySourceHistory).one()
    assert history.preview_media_item_ids_json == [tracks["Beta"], tracks["Alpha"]]
    assert client.get("/api/library/recently-played-sources").json()[0]["preview_media_item_ids"] == [
        tracks["Beta"], tracks["Alpha"]
    ]


def test_recent_sources_uses_empty_preview_for_malformed_stored_json(
    client, db_session
):
    db_session.add(
        PlaySourceHistory(
            user_id=1,
            kind="library",
            label="Songs",
            payload_json={"filters": {}},
        )
    )
    db_session.commit()
    db_session.execute(text("UPDATE play_source_history SET preview_media_item_ids_json = 'not-json'"))
    db_session.commit()

    assert client.get("/api/library/recently-played-sources").json()[0][
        "preview_media_item_ids"
    ] == []


def test_recent_sources_removes_malformed_stored_payload_json(client, db_session):
    history = PlaySourceHistory(
        user_id=1,
        kind="library",
        label="Broken",
        payload_json={"filters": {}},
    )
    db_session.add(history)
    db_session.commit()
    history_id = history.id
    db_session.execute(
        text("UPDATE play_source_history SET payload_json = 'not-json' WHERE id = :id"), {"id": history_id}
    )
    db_session.commit()

    assert client.get("/api/library/recently-played-sources").json() == []
    db_session.expire_all()
    assert db_session.get(PlaySourceHistory, history_id) is None


def test_recent_sources_uses_empty_preview_for_non_integer_preview_ids(client, db_session):
    db_session.add(
        PlaySourceHistory(
            user_id=1,
            kind="library",
            label="Songs",
            payload_json={"filters": {}},
            preview_media_item_ids_json=[1, "bad"],
        )
    )
    db_session.commit()

    assert client.get("/api/library/recently-played-sources").json()[0][
        "preview_media_item_ids"
    ] == []


def test_play_source_history_rejects_unknown_kind(client):
    resp = client.post(
        "/api/library/play-source", json={"kind": "bogus", "label": "X", "payload": {}}
    )
    assert resp.status_code == 422


def test_play_source_history_is_trimmed_to_max(client):
    for i in range(35):
        resp = client.post(
            "/api/library/play-source",
            json={"kind": "mix", "label": f"Mix {i}", "payload": {"genre": f"g{i}"}},
        )
        assert resp.status_code == 204
    rows = client.get("/api/library/recently-played-sources?limit=30").json()
    assert len(rows) == 30
    assert rows[0]["label"] == "Mix 34"


def test_scan_endpoint_triggers_rescan(client, db_session, library_dir, session_factory, monkeypatch):
    monkeypatch.setattr("app.library_tasks.SessionLocal", session_factory)
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)

    make_audio_fixture(
        library_dir / "a.wav", title="Alpha", artist="A", album="X", genre="Rock"
    )
    resp = client.post(f"/api/admin/roots/{root.id}/scan")
    assert resp.status_code == 202
    task = wait_until(lambda: _settled_last_task(client, root.id), what="the scan task")
    assert task["status"] == "completed"
    assert {key: task[key] for key in ("added", "updated", "removed")} == {
        "added": 1,
        "updated": 0,
        "removed": 0,
    }


def test_paging_through_tracks_that_tie_on_every_sort_key_shows_each_once(client, db_session):
    """Tracks with the same artist, album and track number (or none of them)
    come back in one order, not whatever order SQLite chooses per page."""
    from app.models import LibraryRoot, MediaItem, MediaKind

    root = LibraryRoot(path="/ties", display_name="Ties")
    db_session.add(root)
    db_session.flush()
    tied = [
        MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path=f"{i}.flac", title=f"T{i}",
                  artist=None if i % 2 else "Same", album=None if i % 2 else "Same")
        for i in range(12)
    ]
    db_session.add_all(tied)
    db_session.commit()

    from sqlalchemy import event

    paged: list[str] = []

    def record(_conn, _cursor, statement, *_args):
        if "LIMIT" in statement and "FROM media_items" in statement:
            paged.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        seen = []
        for offset in range(12):
            page = client.get("/api/library/tracks", params={"limit": 1, "offset": offset}).json()
            seen += [t["id"] for t in page]
        client.get("/api/library/videos", params={"limit": 1})
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert sorted(seen) == sorted(t.id for t in tied)
    # SQLite happens to return ties in a stable order here; what makes it
    # guaranteed is the id as the last sort key of every paged query.
    assert paged and all(
        statement.split("ORDER BY")[1].split("LIMIT")[0].strip().endswith("media_items.id")
        for statement in paged
    )
    videos = [
        MediaItem(library_root_id=root.id, kind=MediaKind.video, relative_path=f"v{i}.mkv", title="Same")
        for i in range(6)
    ]
    db_session.add_all(videos)
    db_session.commit()
    seen = [
        t["id"]
        for offset in range(6)
        for t in client.get("/api/library/videos", params={"limit": 1, "offset": offset}).json()
    ]
    assert sorted(seen) == sorted(v.id for v in videos)


def test_play_source_stores_nothing_about_another_users_playlist(client, db_session):
    from app.models import User
    from app.security import hash_password

    other = User(username="someone", password_hash=hash_password("pw"))
    db_session.add(other)
    db_session.commit()
    theirs = Playlist(user_id=other.id, name="Their mix", is_auto_generated=True, auto_kind="mix")
    db_session.add(theirs)
    db_session.commit()
    resp = client.post(
        "/api/library/play-source",
        json={"kind": "playlist", "label": "Guess", "payload": {"playlist_id": theirs.id}},
    )
    assert resp.status_code == 204
    row = db_session.query(PlaySourceHistory).one()
    # Neither that it is a mix nor a cover from it.
    assert row.payload_json == {"playlist_id": theirs.id}
    assert row.art_media_item_id is None


def test_specified_mix_says_how_many_tracks_match_every_include(client, db_session, library_dir, monkeypatch):
    """Tracks carrying all the chosen labels go in first and the rest of the
    mix is filled from those carrying only some; the response says how many
    matched them all, so the app can tell the user when that fell short."""
    monkeypatch.setattr(settings, "mix_min_track_seconds", 0)  # one-second sample files
    for name, genre in (("r1", "Rock"), ("r2", "Rock"), ("j1", "Jazz"), ("both", "Rock")):
        make_audio_fixture(library_dir / f"{name}.wav", title=name, artist=f"Artist {name}", album="LP", genre=genre)
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    scan_library_root(db_session, root)
    both = db_session.query(MediaItem).filter_by(title="both").one()
    replace_item_genres(db_session, both, ["Rock", "Jazz"], "musicbrainz")
    db_session.commit()

    def created(body: dict) -> dict:
        resp = client.post("/api/library/mixes/specified", json=body)
        assert resp.status_code == 200, resp.text
        mixes = resp.json()
        (new,) = [row for row in mixes if row["full_match_count"] is not None]
        return new

    partial = created({"include": [{"kind": "genre", "name": "Rock"}, {"kind": "genre", "name": "Jazz"}]})
    assert (partial["track_count"], partial["full_match_count"]) == (4, 1)

    single = created({"include": [{"kind": "genre", "name": "Rock"}]})
    assert (single["track_count"], single["full_match_count"]) == (3, 3)

    # Only the mix just built carries the count; a plain listing never does.
    assert all(row["full_match_count"] is None for row in client.get("/api/library/mixes").json())
