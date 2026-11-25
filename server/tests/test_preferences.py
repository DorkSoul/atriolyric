"""The dislike/like rule (app.preferences) and its API: precedence, how
albums and artists match, the per-user overlay on listings, and the
Disliked list."""

from __future__ import annotations

from app.models import (
    CollectionPreference,
    Favorite,
    LibraryRoot,
    MediaItem,
    MediaKind,
    TrackDislike,
    User,
    UserLibraryAccess,
)
from app.preferences import (
    album_owner,
    disliked_ids,
    fold_name,
    liked_levels,
    not_disliked,
    preference_overlay,
)
from app.security import hash_password
from tests.conftest import DEFAULT_PASSWORD, DEFAULT_USERNAME, login_as


def _root(db_session, *, shared: bool = True) -> LibraryRoot:
    root = LibraryRoot(path=f"/prefs-{shared}", display_name="Prefs", is_shared=shared)
    db_session.add(root)
    db_session.commit()
    return root


def _track(db_session, root, title, artist, album=None, album_artist=None) -> MediaItem:
    item = MediaItem(
        library_root_id=root.id,
        kind=MediaKind.track,
        relative_path=f"{title}.mp3",
        title=title,
        artist=artist,
        album=album,
        album_artist=album_artist,
    )
    db_session.add(item)
    db_session.commit()
    return item


def _pref(db_session, user, kind, artist, state, album=None) -> None:
    db_session.add(
        CollectionPreference(
            user_id=user.id, kind=kind, artist=artist, album=album, state=state
        )
    )
    db_session.commit()


def _excluded(db_session, user) -> set[str]:
    kept = {
        item.title
        for item in db_session.query(MediaItem).filter(not_disliked(user.id))
    }
    return {item.title for item in db_session.query(MediaItem)} - kept


def test_song_state_beats_album_which_beats_artist(db_session):
    user = db_session.query(User).one()
    root = _root(db_session)
    liked_song = _track(db_session, root, "Liked song", "Bad", "Record")
    _track(db_session, root, "Other song", "Bad", "Record")
    _track(db_session, root, "Liked album song", "Bad", "Good LP")
    _track(db_session, root, "Disliked song", "Fine", "Fine LP")
    _pref(db_session, user, "artist", "Bad", "disliked")
    _pref(db_session, user, "album", "Bad", "liked", album="Good LP")
    db_session.add_all(
        [
            Favorite(user_id=user.id, media_item_id=liked_song.id),
            TrackDislike(
                user_id=user.id,
                media_item_id=db_session.query(MediaItem)
                .filter(MediaItem.title == "Disliked song")
                .one()
                .id,
            ),
        ]
    )
    db_session.commit()

    assert _excluded(db_session, user) == {"Other song", "Disliked song"}


def test_disliked_album_beats_liked_artist(db_session):
    user = db_session.query(User).one()
    root = _root(db_session)
    _track(db_session, root, "On bad album", "Loved", "Bad LP")
    _track(db_session, root, "Elsewhere", "Loved", "Good LP")
    _pref(db_session, user, "artist", "Loved", "liked")
    _pref(db_session, user, "album", "Loved", "disliked", album="Bad LP")

    assert _excluded(db_session, user) == {"On bad album"}


def test_track_artist_state_wins_over_album_artist_state(db_session):
    user = db_session.query(User).one()
    root = _root(db_session)
    _track(db_session, root, "Liked guest", "Guest", "Comp", album_artist="Host")
    _track(db_session, root, "Disliked guest", "Nope", "Comp", album_artist="Fan")
    _track(db_session, root, "Plain guest", "Someone", "Comp", album_artist="Host")
    _pref(db_session, user, "artist", "Guest", "liked")
    _pref(db_session, user, "artist", "Host", "disliked")
    _pref(db_session, user, "artist", "Nope", "disliked")
    _pref(db_session, user, "artist", "Fan", "liked")

    # "Plain guest" only matches Host (as album artist), which is disliked.
    assert _excluded(db_session, user) == {"Disliked guest", "Plain guest"}
    by_title = {item.title: item.id for item in db_session.query(MediaItem)}
    overlay = preference_overlay(db_session, user, by_title.values())
    assert overlay[by_title["Plain guest"]] == (False, False, "artist")
    assert overlay[by_title["Disliked guest"]] == (False, False, "artist")
    assert overlay[by_title["Liked guest"]] == (False, False, None)


def test_album_match_uses_owner_and_title_ignoring_case(db_session):
    user = db_session.query(User).one()
    root = _root(db_session)
    _track(db_session, root, "A hit", "Artist A", "Greatest Hits")
    _track(db_session, root, "B hit", "Artist B", "Greatest Hits")
    _track(db_session, root, "Comp track", "Guest", "Various Hits", album_artist="Label")
    _track(db_session, root, "No album artist", "Solo", "Solo LP", album_artist="")
    _pref(db_session, user, "album", "artist a", "disliked", album="GREATEST HITS")
    _pref(db_session, user, "album", "label", "disliked", album="various hits")
    # Empty album_artist falls back to the track artist as the owner.
    _pref(db_session, user, "album", "Solo", "disliked", album="Solo LP")

    assert _excluded(db_session, user) == {"A hit", "Comp track", "No album artist"}


def test_artist_match_is_exact_on_artist_or_album_artist(db_session):
    user = db_session.query(User).one()
    root = _root(db_session)
    _track(db_session, root, "Own track", "The Band", "LP")
    _track(db_session, root, "As album artist", "Session", "Their LP", album_artist="the band")
    _track(db_session, root, "Featuring", "The Band feat. Guest", "Single")
    _track(db_session, root, "Unrelated", "Band", "Other")
    _pref(db_session, user, "artist", "THE BAND", "disliked")

    assert _excluded(db_session, user) == {"Own track", "As album artist"}


def test_likes_do_not_stack_and_report_the_most_specific_level(db_session):
    user = db_session.query(User).one()
    root = _root(db_session)
    song = _track(db_session, root, "Song", "Liked", "Liked LP")
    album = _track(db_session, root, "Album", "Liked", "Liked LP")
    artist = _track(db_session, root, "Artist", "Liked", "Other LP")
    via_album_artist = _track(
        db_session, root, "Via album artist", "Guest", "Comp", album_artist="Liked"
    )
    plain = _track(db_session, root, "Plain", "Nobody", "Plain LP")
    _pref(db_session, user, "artist", "Liked", "liked")
    _pref(db_session, user, "album", "Liked", "liked", album="Liked LP")
    db_session.add(Favorite(user_id=user.id, media_item_id=song.id))
    db_session.commit()

    ids = [song.id, album.id, artist.id, via_album_artist.id, plain.id]
    assert liked_levels(db_session, user, ids) == {
        song.id: "song",
        album.id: "album",
        artist.id: "artist",
        via_album_artist.id: "artist",
    }
    assert liked_levels(db_session, user, []) == {}
    assert disliked_ids(db_session, user, []) == set()


def test_rule_is_per_user(db_session):
    user = db_session.query(User).one()
    other = User(username="other", password_hash="unused")
    db_session.add(other)
    db_session.commit()
    root = _root(db_session)
    track = _track(db_session, root, "Track", "Artist", "LP")
    _pref(db_session, other, "artist", "Artist", "disliked")
    db_session.add(TrackDislike(user_id=other.id, media_item_id=track.id))
    db_session.commit()

    assert _excluded(db_session, user) == set()
    assert _excluded(db_session, other) == {"Track"}


def test_fold_name_matches_sqlite_lower():
    assert fold_name("ÉCOLE Band") == "École band"


def test_listings_carry_excluded_by_and_keep_the_songs_own_heart(client, db_session):
    user = db_session.query(User).one()
    root = _root(db_session)
    liked = _track(db_session, root, "Liked", "Gone", "Gone LP")
    _track(db_session, root, "By artist", "Gone", "Gone LP")
    _track(db_session, root, "By album", "Kept", "Bad LP")
    _track(db_session, root, "Fine", "Kept", "Good LP")
    _pref(db_session, user, "artist", "Gone", "disliked")
    _pref(db_session, user, "album", "Kept", "disliked", album="Bad LP")
    client.put(f"/api/media/{liked.id}/preference", json={"state": "liked"})

    by_title = {t["title"]: t for t in client.get("/api/library/tracks").json()}
    assert by_title["Liked"]["excluded_by"] is None
    assert by_title["Liked"]["is_favorite"] is True
    assert by_title["By artist"]["excluded_by"] == "artist"
    assert by_title["By artist"]["is_disliked"] is False
    assert by_title["By album"]["excluded_by"] == "album"
    assert by_title["Fine"]["excluded_by"] is None


def test_album_preference_resolves_the_owner_from_the_album_page(client, db_session):
    root = _root(db_session)
    # A compilation opened from the Albums grid under one track artist.
    _track(db_session, root, "Guest one", "Guest", "Comp", album_artist="Label")
    _track(db_session, root, "Guest two", "Other", "Comp", album_artist="Label")
    _track(db_session, root, "Solo", "Solo", "Solo LP")

    params = {"artist": "Guest", "album": "Comp"}
    assert client.get("/api/preferences/album", params=params).json() == {
        "kind": "album",
        "artist": "Label",
        "album": "Comp",
        "state": "none",
    }
    resp = client.put("/api/preferences/album", params=params, json={"state": "disliked"})
    assert resp.json()["state"] == "disliked"
    # The whole compilation is disliked, whichever track artist opened it.
    other_page = client.get(
        "/api/preferences/album", params={"artist": "Other", "album": "Comp"}
    ).json()
    assert other_page["state"] == "disliked"
    by_title = {t["title"]: t for t in client.get("/api/library/tracks").json()}
    assert by_title["Guest two"]["excluded_by"] == "album"
    assert by_title["Solo"]["excluded_by"] is None

    client.put("/api/preferences/album", params=params, json={"state": "liked"})
    assert client.get("/api/preferences/album", params=params).json()["state"] == "liked"
    assert client.get("/api/library/tracks").json()[0]["is_favorite"] is False
    client.put("/api/preferences/album", params=params, json={"state": "none"})
    assert client.get("/api/preferences/album", params=params).json()["state"] == "none"


def test_album_owner_picks_the_most_common_owner(db_session):
    user = db_session.query(User).one()
    root = _root(db_session)
    _track(db_session, root, "One", "Band", "Split", album_artist="Band")
    _track(db_session, root, "Two", "Band", "Split", album_artist="Band")
    _track(db_session, root, "Three", "Band", "Split", album_artist="Guest")
    _track(db_session, root, "Untagged", None, "Nameless")

    assert album_owner(db_session, user, "Band", "Split") == "Band"
    assert album_owner(db_session, user, None, "Nameless") is None


def test_unknown_or_inaccessible_albums_and_artists_404(client, db_session, session_factory):
    private = _root(db_session, shared=False)
    _track(db_session, private, "Hidden", "Private", "Private LP")
    db_session.add(
        User(username="viewer", password_hash=hash_password(DEFAULT_PASSWORD))
    )
    db_session.commit()
    viewer = login_as(session_factory, client, "viewer", DEFAULT_PASSWORD)

    album = {"artist": "Private", "album": "Private LP"}
    # Admins are scoped like everyone else until they grant themselves the root.
    assert client.get("/api/preferences/album", params=album).status_code == 404
    db_session.add(
        UserLibraryAccess(
            user_id=db_session.query(User).filter(User.username == DEFAULT_USERNAME).one().id,
            library_root_id=private.id,
        )
    )
    db_session.commit()
    assert client.get("/api/preferences/album", params=album).status_code == 200
    assert viewer.get("/api/preferences/album", params=album).status_code == 404
    assert viewer.put(
        "/api/preferences/album", params=album, json={"state": "disliked"}
    ).status_code == 404
    assert viewer.get(
        "/api/preferences/artist", params={"artist": "Private"}
    ).status_code == 404
    assert viewer.put(
        "/api/preferences/artist", params={"artist": "Private"}, json={"state": "liked"}
    ).status_code == 404
    assert client.get(
        "/api/preferences/album", params={"artist": "Nobody", "album": "Nothing"}
    ).status_code == 404
    # Clearing without an artist has no stored key to fall back on.
    assert client.put(
        "/api/preferences/album", params={"album": "Nothing"}, json={"state": "none"}
    ).status_code == 404


def test_entries_can_be_cleared_after_leaving_the_library(client, db_session):
    user = db_session.query(User).one()
    _pref(db_session, user, "album", "Gone", "disliked", album="Gone LP")
    _pref(db_session, user, "artist", "Gone", "disliked")

    assert client.put(
        "/api/preferences/album",
        params={"artist": "Gone", "album": "Gone LP"},
        json={"state": "none"},
    ).json()["state"] == "none"
    assert client.put(
        "/api/preferences/artist", params={"artist": "Gone"}, json={"state": "none"}
    ).json() == {"kind": "artist", "artist": "Gone", "album": None, "state": "none"}
    assert db_session.query(CollectionPreference).count() == 0


def test_artist_preference_round_trip_is_per_user(client, db_session, session_factory):
    root = _root(db_session)
    song = _track(db_session, root, "Skip me", "Keep", "Keep LP")
    _track(db_session, root, "Theirs", "Nope", "Nope LP", album_artist="Nope")

    artist = {"artist": "nope"}
    assert client.get("/api/preferences/artist", params=artist).json()["state"] == "none"
    client.put("/api/preferences/artist", params=artist, json={"state": "disliked"})
    assert client.get("/api/preferences/artist", params=artist).json()["state"] == "disliked"
    client.put(f"/api/media/{song.id}/preference", json={"state": "disliked"})
    mine = {t["title"]: t for t in client.get("/api/library/tracks").json()}
    assert mine["Theirs"]["excluded_by"] == "artist"
    assert mine["Skip me"]["is_disliked"] is True

    # Per user: another account sees none of it.
    db_session.add(
        User(username="second", password_hash=hash_password(DEFAULT_PASSWORD))
    )
    db_session.commit()
    second = login_as(session_factory, client, "second", DEFAULT_PASSWORD)
    assert second.get("/api/preferences/artist", params=artist).json()["state"] == "none"
    theirs = {t["title"]: t for t in second.get("/api/library/tracks").json()}
    assert theirs["Theirs"]["excluded_by"] is None
    assert theirs["Skip me"]["is_disliked"] is False


def test_large_id_lists_are_looked_up_in_batches(db_session, monkeypatch):
    monkeypatch.setattr("app.preferences._ID_BATCH", 2)
    user = db_session.query(User).one()
    root = _root(db_session)
    items = [_track(db_session, root, f"T{i}", "Band", "LP") for i in range(5)]
    _pref(db_session, user, "artist", "Band", "disliked")
    ids = [item.id for item in items]

    assert disliked_ids(db_session, user, ids) == set(ids)
    assert len(preference_overlay(db_session, user, ids)) == 5
