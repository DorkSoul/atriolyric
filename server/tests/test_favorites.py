from __future__ import annotations

from app.models import Favorite, LibraryRoot, MediaItem, Playlist, TrackDislike
from app.scanner import scan_library_root
from tests.conftest import make_audio_fixture


def _scan_one_track(db_session, library_dir) -> int:
    make_audio_fixture(
        library_dir / "track1.wav", title="Song", artist="A", album="X", genre="Rock"
    )
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    return db_session.query(MediaItem).one().id


def _set(client, media_id: int, state: str):
    return client.put(f"/api/media/{media_id}/preference", json={"state": state})


def _favourites_ids(db_session) -> list[int]:
    playlist = (
        db_session.query(Playlist).filter(Playlist.auto_kind == "favorites").one()
    )
    db_session.refresh(playlist)
    return [item.media_item_id for item in playlist.items]


def test_heart_cycles_none_liked_disliked_none(client, db_session, library_dir):
    media_id = _scan_one_track(db_session, library_dir)

    def track():
        return client.get("/api/library/tracks").json()[0]

    assert (track()["is_favorite"], track()["is_disliked"]) == (False, False)

    assert _set(client, media_id, "liked").json() == {"state": "liked"}
    assert (track()["is_favorite"], track()["is_disliked"]) == (True, False)
    assert _favourites_ids(db_session) == [media_id]

    assert _set(client, media_id, "disliked").json() == {"state": "disliked"}
    assert (track()["is_favorite"], track()["is_disliked"]) == (False, True)
    assert _favourites_ids(db_session) == []

    assert _set(client, media_id, "none").json() == {"state": "none"}
    assert (track()["is_favorite"], track()["is_disliked"]) == (False, False)
    # Clearing a dislike never re-adds the song to Favourites.
    assert _favourites_ids(db_session) == []
    assert db_session.query(Favorite).count() == 0
    assert db_session.query(TrackDislike).count() == 0


def test_liking_is_idempotent(client, db_session, library_dir):
    media_id = _scan_one_track(db_session, library_dir)
    _set(client, media_id, "liked")
    assert _set(client, media_id, "liked").status_code == 200
    assert _favourites_ids(db_session) == [media_id]


def test_liking_a_disliked_song_clears_the_dislike(client, db_session, library_dir):
    media_id = _scan_one_track(db_session, library_dir)
    _set(client, media_id, "disliked")
    _set(client, media_id, "liked")
    assert db_session.query(TrackDislike).count() == 0
    assert _favourites_ids(db_session) == [media_id]


def test_invalid_state_is_rejected(client, db_session, library_dir):
    media_id = _scan_one_track(db_session, library_dir)
    assert _set(client, media_id, "loved").status_code == 422


def test_preference_missing_media_item_404s(client):
    assert _set(client, 999999, "liked").status_code == 404


def test_star_ratings_and_old_favorite_endpoints_are_gone(client, db_session, library_dir):
    media_id = _scan_one_track(db_session, library_dir)
    assert client.put(
        f"/api/media/{media_id}/rating", json={"stars": 4}
    ).status_code in (404, 405)
    assert client.put(f"/api/media/{media_id}/favorite").status_code in (404, 405)
    assert "rating" not in client.get("/api/library/tracks").json()[0]


def test_liking_adds_to_favourites_playlist(client, db_session, library_dir):
    media_id = _scan_one_track(db_session, library_dir)

    _set(client, media_id, "liked")

    playlist = db_session.query(Playlist).filter(Playlist.auto_kind == "favorites").one()
    assert playlist.name == "Favourites"
    assert [item.media_item_id for item in playlist.items] == [media_id]

    playlists = client.get("/api/playlists").json()
    favorites_summary = next(p for p in playlists if p["auto_kind"] == "favorites")
    assert favorites_summary["item_count"] == 1


def test_reliking_does_not_duplicate_playlist_entry(client, db_session, library_dir):
    media_id = _scan_one_track(db_session, library_dir)
    _set(client, media_id, "liked")
    _set(client, media_id, "none")
    _set(client, media_id, "liked")
    _set(client, media_id, "liked")  # idempotent re-like

    assert _favourites_ids(db_session) == [media_id]


def test_likes_and_dislikes_attach_to_the_right_tracks(client, db_session, library_dir):
    """The /tracks list batches like/dislike lookups in one query each
    instead of one query per track - this pins down that the batching maps
    results back onto the correct track and doesn't bleed state between
    tracks that aren't liked/disliked at all."""
    for i, title in enumerate(["First", "Second", "Third"], start=1):
        make_audio_fixture(
            library_dir / f"track{i}.wav", title=title, artist="A", album="X", genre="Rock"
        )
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    ids = {item.title: item.id for item in db_session.query(MediaItem).all()}

    _set(client, ids["First"], "liked")
    _set(client, ids["Third"], "disliked")

    by_title = {t["title"]: t for t in client.get("/api/library/tracks").json()}
    assert (by_title["First"]["is_favorite"], by_title["First"]["is_disliked"]) == (True, False)
    assert (by_title["Second"]["is_favorite"], by_title["Second"]["is_disliked"]) == (False, False)
    assert (by_title["Third"]["is_favorite"], by_title["Third"]["is_disliked"]) == (False, True)
    assert all(t["excluded_by"] is None for t in by_title.values())


# --- the Favourites playlist always matches the likes ----------------------


def test_liking_again_rebuilds_a_favourites_playlist_that_went_missing(
    client, db_session, library_dir
):
    media_id = _scan_one_track(db_session, library_dir)
    assert _set(client, media_id, "liked").status_code == 200
    # A Favourites playlist gone missing; the like itself stays.
    playlist = db_session.query(Playlist).filter(Playlist.auto_kind == "favorites").one()
    db_session.delete(playlist)
    db_session.commit()

    assert _set(client, media_id, "liked").status_code == 200

    assert _favourites_ids(db_session) == [media_id]


def test_only_one_favourites_playlist_per_user(db_session):
    import pytest
    from sqlalchemy.exc import IntegrityError

    from app.models import User

    user = db_session.query(User).one()
    db_session.add(Playlist(user_id=user.id, name="Favourites", is_auto_generated=True, auto_kind="favorites"))
    db_session.commit()
    db_session.add(Playlist(user_id=user.id, name="Favourites", is_auto_generated=True, auto_kind="favorites"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    # Other auto kinds and the user's own playlists are not limited.
    db_session.add_all([Playlist(user_id=user.id, name=f"Mix {i}", auto_kind="mix") for i in range(2)])
    db_session.add_all([Playlist(user_id=user.id, name="Mine") for _ in range(2)])
    db_session.commit()


def test_a_like_that_loses_a_race_converges_instead_of_failing(
    db_session, library_dir, session_factory, monkeypatch
):
    """Two likes of one song (two devices, a double tap): the second insert
    of the same Favorite fails, and the request must end liked, not 500."""
    from app import preferences
    from app.models import PlaylistItem, User

    media_id = _scan_one_track(db_session, library_dir)
    user_id = db_session.query(User).one().id
    other = session_factory()
    preferences.set_track_state(other, other.get(User, user_id), media_id, "liked")
    other.close()
    real = preferences._has_favorite
    stale_reads: list[bool] = []

    def read_before_the_other_commit(db, uid, mid):
        if not stale_reads:
            stale_reads.append(True)
            return False
        return real(db, uid, mid)

    monkeypatch.setattr(preferences, "_has_favorite", read_before_the_other_commit)
    mine = session_factory()
    preferences.set_track_state(mine, mine.get(User, user_id), media_id, "liked")
    mine.close()

    db_session.expire_all()
    assert stale_reads == [True]
    assert db_session.query(Favorite).count() == 1
    assert db_session.query(Playlist).filter(Playlist.auto_kind == "favorites").count() == 1
    assert db_session.query(PlaylistItem).count() == 1
