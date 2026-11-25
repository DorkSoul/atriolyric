from __future__ import annotations

import pytest

from app.collage import collage_media_item_ids
from app.models import LibraryRoot, MediaItem, Playlist, User
from app.scanner import scan_library_root
from tests.conftest import make_audio_fixture


def _scan_three_tracks(db_session, library_dir):
    for i, title in enumerate(["First", "Second", "Third"], start=1):
        make_audio_fixture(
            library_dir / f"track{i}.wav",
            title=title,
            artist="Artist",
            album="Album",
            genre="Rock",
            track_number=i,
        )
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    items = db_session.query(MediaItem).order_by(MediaItem.track_number).all()
    return {item.title: item.id for item in items}


def test_collage_helper_skips_duplicate_artists_and_stops_at_limit():
    from types import SimpleNamespace

    items = [
        # An orphaned row (its media item was deleted) must be skipped
        # rather than crash a refresh or the startup backfill.
        SimpleNamespace(media_item=None),
        *[
            SimpleNamespace(media_item=SimpleNamespace(id=index, artist=artist))
            for index, artist in enumerate(["A", "A", "B", "C", "D", "E"], start=1)
        ],
    ]
    assert collage_media_item_ids(items) == [1, 3, 4, 5]


def test_create_and_list_playlist(client):
    resp = client.post("/api/playlists", json={"name": "My Mix"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "My Mix"
    assert resp.json()["is_auto_generated"] is False

    resp = client.get("/api/playlists")
    assert len(resp.json()) == 1
    assert resp.json()[0]["is_auto_generated"] is False


def test_list_playlists_flags_auto_generated_ones(client, db_session):
    from app.models import Playlist

    db_session.add(Playlist(user_id=1, name="Discovery", is_auto_generated=True))
    db_session.commit()

    resp = client.get("/api/playlists")
    by_name = {p["name"]: p for p in resp.json()}
    assert by_name["Discovery"]["is_auto_generated"] is True


def test_save_copy_endpoint_copies_auto_playlist(client, db_session, library_dir):
    from app.models import Playlist, PlaylistItem

    ids = _scan_three_tracks(db_session, library_dir)
    auto = Playlist(
        user_id=1, name="Discovery", is_auto_generated=True, auto_kind="discovery"
    )
    db_session.add(auto)
    db_session.commit()
    db_session.add(
        PlaylistItem(playlist_id=auto.id, media_item_id=ids["First"], position=0)
    )
    db_session.commit()

    resp = client.post(
        f"/api/playlists/{auto.id}/save-copy", json={"name": "Saved Mix"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Saved Mix"
    assert body["is_auto_generated"] is False
    assert body["item_count"] == 1

    playlists = client.get("/api/playlists").json()
    names = [p["name"] for p in playlists]
    assert "Discovery" in names and "Saved Mix" in names


def test_save_copy_rejects_ordinary_playlist(client):
    playlist_id = client.post("/api/playlists", json={"name": "Mine"}).json()["id"]
    resp = client.post(f"/api/playlists/{playlist_id}/save-copy", json={})
    assert resp.status_code == 404


def test_add_items_appends_by_default(client, db_session, library_dir):
    ids = _scan_three_tracks(db_session, library_dir)
    playlist_id = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]

    client.post(
        f"/api/playlists/{playlist_id}/items", json={"media_item_ids": [ids["First"]]}
    )
    client.post(
        f"/api/playlists/{playlist_id}/items", json={"media_item_ids": [ids["Second"]]}
    )

    detail = client.get(f"/api/playlists/{playlist_id}").json()
    assert [t["title"] for t in detail["items"]] == ["First", "Second"]


def test_append_after_removal_stays_at_the_end(client, db_session, library_dir):
    ids = _scan_three_tracks(db_session, library_dir)
    playlist_id = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    detail = client.post(
        f"/api/playlists/{playlist_id}/items",
        json={"media_item_ids": list(ids.values())},
    ).json()
    client.delete(
        f"/api/playlists/{playlist_id}/items/{detail['items'][1]['playlist_item_id']}"
    )
    client.post(
        f"/api/playlists/{playlist_id}/items", json={"media_item_ids": [ids["Second"]]}
    )
    detail = client.get(f"/api/playlists/{playlist_id}").json()
    assert [item["title"] for item in detail["items"]] == ["First", "Third", "Second"]


def test_favorites_playlist_cannot_be_deleted_or_manually_changed(
    client, db_session, library_dir
):
    ids = _scan_three_tracks(db_session, library_dir)
    client.put(f"/api/media/{ids['First']}/preference", json={"state": "liked"})
    playlist = (
        db_session.query(Playlist).filter(Playlist.auto_kind == "favorites").one()
    )
    assert client.delete(f"/api/playlists/{playlist.id}").status_code == 409
    assert (
        client.post(
            f"/api/playlists/{playlist.id}/items",
            json={"media_item_ids": [ids["Second"]]},
        ).status_code
        == 409
    )
    favorite_item_id = playlist.items[0].id
    assert (
        client.delete(
            f"/api/playlists/{playlist.id}/items/{favorite_item_id}"
        ).status_code
        == 409
    )
    assert (
        client.put(
            f"/api/playlists/{playlist.id}/reorder",
            json={"ordered_item_ids": [favorite_item_id]},
        ).status_code
        == 409
    )


def test_playlist_detail_reflects_like_and_dislike_state(
    client, db_session, library_dir
):
    # Playlist items go through the per-user like/dislike attachment every
    # other listing endpoint goes through, so a favorited track's heart
    # shows as filled when viewed inside a playlist (most visibly the
    # Favourites playlist itself).
    ids = _scan_three_tracks(db_session, library_dir)
    playlist_id = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(
        f"/api/playlists/{playlist_id}/items",
        json={"media_item_ids": [ids["First"], ids["Second"]]},
    )

    client.put(f"/api/media/{ids['First']}/preference", json={"state": "liked"})
    client.put(f"/api/media/{ids['Second']}/preference", json={"state": "disliked"})

    detail = client.get(f"/api/playlists/{playlist_id}").json()
    by_title = {t["title"]: t for t in detail["items"]}
    assert by_title["First"]["is_favorite"] is True
    assert by_title["Second"]["is_favorite"] is False
    assert by_title["Second"]["is_disliked"] is True
    # A user playlist is never filtered, so the disliked song still shows.
    assert set(by_title) == {"First", "Second"}


def test_list_playlists_includes_collage_preview_ids(client, db_session, library_dir):
    for i, artist in enumerate(["A", "B", "C", "D", "E"], start=1):
        make_audio_fixture(
            library_dir / f"track{i}.wav",
            title=f"T{i}",
            artist=artist,
            album="X",
            genre="Rock",
        )
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    ids = {item.title: item.id for item in db_session.query(MediaItem).all()}

    playlist_id = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(
        f"/api/playlists/{playlist_id}/items",
        json={
            "media_item_ids": [ids["T1"], ids["T2"], ids["T3"], ids["T4"], ids["T5"]]
        },
    )

    playlists = client.get("/api/playlists").json()
    mix = next(p for p in playlists if p["name"] == "Mix")
    # Capped at 4 even though 5 distinct-artist tracks are in the playlist.
    assert mix["preview_media_item_ids"] == [ids["T1"], ids["T2"], ids["T3"], ids["T4"]]


def test_collage_preview_ids_skip_repeated_artists(client, db_session, library_dir):
    for i in range(1, 4):
        make_audio_fixture(
            library_dir / f"track{i}.wav",
            title=f"T{i}",
            artist="Same Artist",
            album="X",
            genre="Rock",
        )
    make_audio_fixture(
        library_dir / "track4.wav",
        title="T4",
        artist="Other Artist",
        album="X",
        genre="Rock",
    )
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    ids = {item.title: item.id for item in db_session.query(MediaItem).all()}

    playlist_id = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(
        f"/api/playlists/{playlist_id}/items",
        json={"media_item_ids": [ids["T1"], ids["T2"], ids["T3"], ids["T4"]]},
    )

    playlists = client.get("/api/playlists").json()
    mix = next(p for p in playlists if p["name"] == "Mix")
    assert mix["preview_media_item_ids"] == [ids["T1"], ids["T4"]]


def test_play_next_inserts_at_head(client, db_session, library_dir):
    ids = _scan_three_tracks(db_session, library_dir)
    playlist_id = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]

    client.post(
        f"/api/playlists/{playlist_id}/items", json={"media_item_ids": [ids["First"]]}
    )
    client.post(
        f"/api/playlists/{playlist_id}/items", json={"media_item_ids": [ids["Second"]]}
    )
    # "Play next": explicit position 0 jumps ahead of everything already queued.
    client.post(
        f"/api/playlists/{playlist_id}/items",
        json={"media_item_ids": [ids["Third"]], "position": 0},
    )

    detail = client.get(f"/api/playlists/{playlist_id}").json()
    assert [t["title"] for t in detail["items"]] == ["Third", "First", "Second"]


def test_reorder_playlist(client, db_session, library_dir):
    ids = _scan_three_tracks(db_session, library_dir)
    playlist_id = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(
        f"/api/playlists/{playlist_id}/items",
        json={"media_item_ids": [ids["First"], ids["Second"], ids["Third"]]},
    )
    # Fetch the raw playlist-item ids (not media_item ids) via the DB directly,
    # since reorder operates on playlist_item.id.
    from app.models import PlaylistItem

    pitems = (
        db_session.query(PlaylistItem)
        .filter(PlaylistItem.playlist_id == playlist_id)
        .order_by(PlaylistItem.position)
        .all()
    )
    ordered = [pitems[2].id, pitems[0].id, pitems[1].id]

    resp = client.put(
        f"/api/playlists/{playlist_id}/reorder", json={"ordered_item_ids": ordered}
    )
    assert resp.status_code == 200
    assert [t["title"] for t in resp.json()["items"]] == ["Third", "First", "Second"]


def test_reorder_rejects_duplicate_item_ids(client, db_session, library_dir):
    ids = _scan_three_tracks(db_session, library_dir)
    playlist_id = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(
        f"/api/playlists/{playlist_id}/items",
        json={"media_item_ids": [ids["First"], ids["Second"]]},
    )
    from app.models import PlaylistItem

    item_ids = [
        row.id
        for row in db_session.query(PlaylistItem)
        .filter(PlaylistItem.playlist_id == playlist_id)
        .all()
    ]
    resp = client.put(
        f"/api/playlists/{playlist_id}/reorder",
        json={"ordered_item_ids": [item_ids[0], item_ids[0], item_ids[1]]},
    )
    assert resp.status_code == 400


def test_playlist_detail_items_expose_their_own_playlist_item_id(
    client, db_session, library_dir
):
    # The frontend's "Remove from playlist" action targets this specific
    # occurrence (not the track itself), so a track appearing twice in the
    # same playlist can have just one occurrence removed.
    ids = _scan_three_tracks(db_session, library_dir)
    playlist_id = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(
        f"/api/playlists/{playlist_id}/items",
        json={"media_item_ids": [ids["First"], ids["First"]]},
    )
    from app.models import PlaylistItem

    real_ids = [
        row.id
        for row in db_session.query(PlaylistItem)
        .filter(PlaylistItem.playlist_id == playlist_id)
        .order_by(PlaylistItem.position)
        .all()
    ]
    assert len(real_ids) == 2
    assert real_ids[0] != real_ids[1]

    detail = client.get(f"/api/playlists/{playlist_id}").json()
    assert [item["playlist_item_id"] for item in detail["items"]] == real_ids


def test_remove_item(client, db_session, library_dir):
    ids = _scan_three_tracks(db_session, library_dir)
    playlist_id = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(
        f"/api/playlists/{playlist_id}/items",
        json={"media_item_ids": [ids["First"], ids["Second"]]},
    )
    from app.models import PlaylistItem

    first_playlist_item = (
        db_session.query(PlaylistItem)
        .filter(PlaylistItem.playlist_id == playlist_id)
        .first()
    )

    resp = client.delete(f"/api/playlists/{playlist_id}/items/{first_playlist_item.id}")
    assert resp.status_code == 200

    detail = client.get(f"/api/playlists/{playlist_id}").json()
    assert len(detail["items"]) == 1


def test_m3u_export_lists_each_track_under_its_root_name(client, db_session, library_dir):
    ids = _scan_three_tracks(db_session, library_dir)
    playlist_id = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(
        f"/api/playlists/{playlist_id}/items",
        json={"media_item_ids": [ids["First"], ids["Second"]]},
    )

    exported = client.get(f"/api/playlists/{playlist_id}/export.m3u")
    assert exported.status_code == 200
    assert "#EXTM3U" in exported.text
    assert "Test Library/track1.wav" in exported.text


def test_delete_playlist(client):
    playlist_id = client.post("/api/playlists", json={"name": "Temp"}).json()["id"]
    resp = client.delete(f"/api/playlists/{playlist_id}")
    assert resp.status_code == 200
    assert client.get(f"/api/playlists/{playlist_id}").status_code == 404


def test_add_more_endpoint_rejects_ordinary_playlist(client):
    playlist_id = client.post("/api/playlists", json={"name": "Ordinary"}).json()["id"]
    resp = client.post(f"/api/playlists/{playlist_id}/add-more")
    assert resp.status_code == 422


def test_add_more_endpoint_returns_updated_auto_playlist(client, db_session):
    auto = Playlist(
        user_id=1,
        name="Rock Mix",
        is_auto_generated=True,
        auto_kind="mix",
        mix_genre="Rock",
    )
    db_session.add(auto)
    db_session.commit()

    resp = client.post(f"/api/playlists/{auto.id}/add-more")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Rock Mix"


def test_rename_playlist_and_set_then_clear_description(client):
    playlist_id = client.post("/api/playlists", json={"name": "Draft"}).json()["id"]

    resp = client.patch(
        f"/api/playlists/{playlist_id}",
        json={"name": "  Road Trip  ", "description": "  Songs for the M50  "},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Road Trip"
    assert resp.json()["description"] == "Songs for the M50"
    listed = client.get("/api/playlists").json()[0]
    assert (listed["name"], listed["description"]) == ("Road Trip", "Songs for the M50")

    # Leaving a field out keeps it; an empty description clears it.
    resp = client.patch(f"/api/playlists/{playlist_id}", json={"description": "   "})
    assert resp.json()["name"] == "Road Trip"
    assert resp.json()["description"] is None
    resp = client.patch(f"/api/playlists/{playlist_id}", json={"description": None})
    assert resp.json()["description"] is None


def test_rename_playlist_rejects_blank_or_overlong_fields(client):
    playlist_id = client.post("/api/playlists", json={"name": "Keep"}).json()["id"]
    assert client.patch(f"/api/playlists/{playlist_id}", json={"name": "   "}).status_code == 422
    assert client.patch(f"/api/playlists/{playlist_id}", json={"name": ""}).status_code == 422
    assert client.patch(f"/api/playlists/{playlist_id}", json={"description": "x" * 1001}).status_code == 422
    assert client.get(f"/api/playlists/{playlist_id}").json()["name"] == "Keep"


@pytest.mark.parametrize("auto_kind", ["favorites", "mix", "generated", "discovery"])
def test_auto_managed_playlists_cannot_be_edited(client, db_session, auto_kind):
    auto = Playlist(user_id=1, name="Managed", is_auto_generated=True, auto_kind=auto_kind)
    db_session.add(auto)
    db_session.commit()
    resp = client.patch(f"/api/playlists/{auto.id}", json={"name": "Mine now"})
    assert resp.status_code == 409


def test_cannot_edit_another_users_playlist(client, db_session):
    other = User(username="someone", password_hash="x")
    db_session.add(other)
    db_session.commit()
    theirs = Playlist(user_id=other.id, name="Theirs")
    db_session.add(theirs)
    db_session.commit()
    assert client.patch(f"/api/playlists/{theirs.id}", json={"name": "Stolen"}).status_code == 404


def test_add_more_response_includes_new_tracks_and_refreshes_preview(client, db_session):
    root = LibraryRoot(path="/add-more-root", display_name="Add more")
    db_session.add(root)
    db_session.commit()
    for i in range(3):
        db_session.add(
            MediaItem(
                library_root_id=root.id,
                kind="track",
                relative_path=f"r{i}.mp3",
                title=f"Rock {i}",
                artist=f"Artist {i}",
                genre="Rock",
            )
        )
    auto = Playlist(
        user_id=1,
        name="Rock Mix",
        is_auto_generated=True,
        auto_kind="mix",
        mix_genre="Rock",
    )
    db_session.add(auto)
    db_session.commit()
    assert auto.items == []  # load the (soon stale) relationship

    resp = client.post(f"/api/playlists/{auto.id}/add-more")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 3
    db_session.refresh(auto)
    assert len(auto.preview_media_item_ids_json or []) == 3


def test_reorder_moves_the_visible_items_and_keeps_hidden_ones_in_place(
    client, db_session, library_dir
):
    """The detail hides songs disliked since a generated playlist was built
    (and songs on roots the user cannot see), so the client can only send
    the ids it was shown."""
    from app.models import PlaylistItem

    ids = _scan_three_tracks(db_session, library_dir)
    user = db_session.query(User).one()
    playlist = Playlist(user_id=user.id, name="Rock mix", is_auto_generated=True, auto_kind="mix")
    db_session.add(playlist)
    db_session.flush()
    rows = [
        PlaylistItem(playlist_id=playlist.id, media_item_id=ids[title], position=i)
        for i, title in enumerate(["First", "Second", "Third"])
    ]
    db_session.add_all(rows)
    db_session.commit()
    first, second, third = (row.id for row in rows)
    assert client.put(f"/api/media/{ids['Second']}/preference", json={"state": "disliked"}).status_code == 200
    shown = client.get(f"/api/playlists/{playlist.id}").json()["items"]
    assert [t["playlist_item_id"] for t in shown] == [first, third]

    resp = client.put(f"/api/playlists/{playlist.id}/reorder", json={"ordered_item_ids": [third, first]})

    assert resp.status_code == 200
    assert [t["title"] for t in resp.json()["items"]] == ["Third", "First"]
    db_session.expire_all()
    stored = db_session.query(PlaylistItem).filter_by(playlist_id=playlist.id).order_by(PlaylistItem.position)
    assert [(row.id, row.position) for row in stored] == [(third, 0), (second, 1), (first, 2)]
    # Sending every id (the hidden one too) still works; anything else does not.
    assert client.put(
        f"/api/playlists/{playlist.id}/reorder", json={"ordered_item_ids": [first, second, third]}
    ).status_code == 200
    assert client.put(
        f"/api/playlists/{playlist.id}/reorder", json={"ordered_item_ids": [first]}
    ).status_code == 400


def test_playlist_detail_query_count_does_not_grow_with_its_length(
    client, db_session, library_dir, session_factory
):
    from sqlalchemy import event

    from app.models import MediaKind

    _scan_three_tracks(db_session, library_dir)
    root = db_session.query(LibraryRoot).one()
    songs = [
        MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path=f"s{i}.wav", title=f"S{i}")
        for i in range(30)
    ]
    db_session.add_all(songs)
    db_session.commit()
    ids = [song.id for song in songs]

    def detail_queries(length: int) -> int:
        playlist_id = client.post("/api/playlists", json={"name": f"P{length}"}).json()["id"]
        client.post(f"/api/playlists/{playlist_id}/items", json={"media_item_ids": ids[:length]})
        statements: list[str] = []
        engine = session_factory.kw["bind"]

        def count(_conn, _cursor, statement, *_args):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", count)
        try:
            assert client.get(f"/api/playlists/{playlist_id}").status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", count)
        return len(statements)

    assert detail_queries(3) == detail_queries(30)
