from __future__ import annotations

import itertools

from sqlalchemy import text

from app.artist_credits import rebuild_all_credits
from app.db import engine, fold_text
from app.models import (
    LibraryRoot,
    MediaItem,
    MediaKind,
    Playlist,
    User,
    UserLibraryAccess,
)
from app.scanner import scan_library_root
from app.security import hash_password
from tests.conftest import DEFAULT_USERNAME, login_as, make_audio_fixture

_paths = itertools.count()


def _root(db_session, path: str = "/music", *, is_shared: bool = True) -> LibraryRoot:
    root = LibraryRoot(path=path, display_name=path, is_shared=is_shared)
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    return root


def _item(
    db_session,
    root: LibraryRoot,
    title: str,
    *,
    artist: str | None = None,
    album: str | None = None,
    album_artist: str | None = None,
    kind: MediaKind = MediaKind.track,
) -> MediaItem:
    item = MediaItem(
        library_root_id=root.id,
        kind=kind,
        relative_path=f"{next(_paths)}.mp3",
        title=title,
        artist=artist,
        album=album,
        album_artist=album_artist,
    )
    db_session.add(item)
    db_session.commit()
    rebuild_all_credits(db_session, only_missing=True)
    return item


def _user_id(db_session, username: str = DEFAULT_USERNAME) -> int:
    return db_session.query(User).filter(User.username == username).one().id


def _search(client, q: str) -> dict:
    resp = client.get("/api/library/search", params={"q": q})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _titles(body: dict) -> list[str]:
    return [song["title"] for song in body["songs"]]


def _artists(body: dict) -> list[str]:
    return [artist["artist"] for artist in body["artists"]]


def _albums(body: dict) -> list[str]:
    return [album["album"] for album in body["albums"]]


def _playlists(body: dict) -> list[str]:
    return [playlist["name"] for playlist in body["playlists"]]


def test_fold_text_ignores_case_accents_and_compatibility_forms():
    assert fold_text(None) is None
    assert fold_text("Beyoncé") == "beyonce"
    assert fold_text("ß") == "ss"
    assert fold_text("Ｆｕｌｌ") == "full"


def test_atrio_fold_is_registered_on_the_app_engine():
    with engine.connect() as conn:
        assert conn.execute(text("SELECT atrio_fold('BEYONCÉ')")).scalar() == "beyonce"


def test_accents_and_case_are_ignored_in_every_section(client, db_session, library_dir):
    make_audio_fixture(
        library_dir / "halo.wav",
        title="Beyoncé Medley",
        artist="Beyoncé",
        album="Beyoncé",
        genre="Pop",
    )
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)

    for q in ("beyonce", "BEYONCÉ"):
        body = _search(client, q)
        assert _artists(body) == ["Beyoncé"]
        assert _albums(body) == ["Beyoncé"]
        assert body["albums"][0]["artist"] == "Beyoncé"
        assert _titles(body) == ["Beyoncé Medley"]


def test_every_word_must_appear_in_the_sections_own_field(client, db_session):
    root = _root(db_session)
    _item(db_session, root, "Time", artist="Pink Floyd", album="The Dark Side of the Moon")
    _item(db_session, root, "Yesterday", artist="The Beatles", album="Help!")

    assert _albums(_search(client, "dark side")) == ["The Dark Side of the Moon"]
    assert _albums(_search(client, "side dark")) == ["The Dark Side of the Moon"]
    assert _titles(_search(client, "yesterday")) == ["Yesterday"]
    assert _search(client, "yesterday beatles")["songs"] == []


def test_songs_match_on_title_only(client, db_session):
    root = _root(db_session)
    _item(db_session, root, "Other", artist="Match", album="Match")

    body = _search(client, "match")
    assert body["songs"] == []
    assert _artists(body) == ["Match"]
    assert _albums(body) == ["Match"]


def test_an_artist_credited_only_as_album_artist_is_found(client, db_session):
    root = _root(db_session)
    _item(db_session, root, "Track", artist="Various", album="Hits", album_artist="Compilers")

    body = _search(client, "compilers")
    assert _artists(body) == ["Compilers"]
    assert body["artists"][0]["album_count"] == 1
    assert body["artists"][0]["track_count"] == 1


def test_results_are_limited_to_roots_the_user_can_access(
    session_factory, client, db_session
):
    shared = _root(db_session, "/shared")
    private = _root(db_session, "/private", is_shared=False)
    _item(db_session, shared, "Shared Match", artist="Shared Match", album="Shared Match")
    _item(db_session, private, "Private Match", artist="Private Match", album="Private Match")
    db_session.add(User(username="plain", password_hash=hash_password("plainpass")))
    db_session.commit()
    plain = login_as(session_factory, client, "plain", "plainpass")

    body = _search(plain, "match")
    assert _titles(body) == ["Shared Match"]
    assert _artists(body) == ["Shared Match"]
    assert _albums(body) == ["Shared Match"]

    # Admins are scoped like everyone else until they grant themselves the root.
    assert _titles(_search(client, "match")) == ["Shared Match"]
    db_session.add(
        UserLibraryAccess(user_id=_user_id(db_session), library_root_id=private.id)
    )
    db_session.commit()
    assert _titles(_search(client, "match")) == ["Private Match", "Shared Match"]


def test_playlists_are_the_users_own_including_mixes_and_discovery(
    session_factory, client, db_session
):
    root = _root(db_session)
    item = _item(db_session, root, "Song")
    other = User(username="other", password_hash=hash_password("otherpass"))
    db_session.add(other)
    db_session.commit()
    me = _user_id(db_session)
    db_session.add_all(
        [
            Playlist(user_id=me, name="Road Trip"),
            Playlist(
                user_id=me, name="Rock Mix", is_auto_generated=True, auto_kind="mix", mix_genre="Rock"
            ),
            Playlist(user_id=me, name="Discovery", is_auto_generated=True, auto_kind="discovery"),
            Playlist(user_id=other.id, name="Other Rock Mix"),
        ]
    )
    db_session.commit()
    road_trip = db_session.query(Playlist).filter(Playlist.name == "Road Trip").one()
    client.post(f"/api/playlists/{road_trip.id}/items", json={"media_item_ids": [item.id]})

    assert _playlists(_search(client, "mix")) == ["Rock Mix"]
    assert _playlists(_search(client, "discovery")) == ["Discovery"]
    road = _search(client, "road")["playlists"]
    assert road[0]["item_count"] == 1
    assert road[0]["preview_media_item_ids"] == [item.id]

    other_client = login_as(session_factory, client, "other", "otherpass")
    assert _playlists(_search(other_client, "mix")) == ["Other Rock Mix"]


def test_starts_with_matches_come_first_each_group_a_to_z_ignoring_accents(
    client, db_session
):
    root = _root(db_session)
    for title in ("Zebra éclair", "eclipse", "Dan's eclair", "Éclair", "Émile's Eclair"):
        _item(db_session, root, title)

    assert _titles(_search(client, "ecl")) == [
        "Éclair",
        "eclipse",
        "Dan's eclair",
        "Émile's Eclair",
        "Zebra éclair",
    ]


def test_each_section_is_capped_at_13(client, db_session):
    root = _root(db_session)
    me = _user_id(db_session)
    for n in range(20):
        _item(db_session, root, f"Cap Song {n:02}", artist=f"Cap Artist {n:02}", album=f"Cap Album {n:02}")
        db_session.add(Playlist(user_id=me, name=f"Cap Playlist {n:02}"))
    db_session.commit()

    body = _search(client, "cap")
    assert len(body["artists"]) == 13
    assert len(body["albums"]) == 13
    assert len(body["songs"]) == 13
    assert len(body["playlists"]) == 13
    assert _titles(body)[0] == "Cap Song 00"


def test_like_metacharacters_match_literally(client, db_session):
    root = _root(db_session)
    for title in ("100% Pure", "1000 Days", "a_b side", "axb side", "back\\slash"):
        _item(db_session, root, title)

    assert _titles(_search(client, "100%")) == ["100% Pure"]
    assert _titles(_search(client, "a_b")) == ["a_b side"]
    assert _titles(_search(client, "k\\s")) == ["back\\slash"]


def test_videos_and_music_videos_are_excluded(client, db_session):
    root = _root(db_session)
    _item(db_session, root, "Clip", artist="Clip", album="Clip", kind=MediaKind.video)
    _item(db_session, root, "Clip", artist="Clip", album="Clip", kind=MediaKind.music_video)

    assert _search(client, "clip") == {"artists": [], "albums": [], "songs": [], "playlists": []}


def test_a_blank_query_returns_four_empty_lists(client, db_session):
    root = _root(db_session)
    _item(db_session, root, "Anything", artist="Anyone", album="Anywhere")
    empty = {"artists": [], "albums": [], "songs": [], "playlists": []}

    assert client.get("/api/library/search").json() == empty
    assert _search(client, "") == empty
    assert _search(client, "   ") == empty
    # A lone combining accent folds to nothing, so it's no words at all.
    assert _search(client, "́") == empty


def test_song_results_carry_the_users_favorite_state(client, db_session):
    root = _root(db_session)
    item = _item(db_session, root, "Loved")
    assert client.put(f"/api/media/{item.id}/preference", json={"state": "liked"}).status_code == 200

    assert _search(client, "loved")["songs"][0]["is_favorite"] is True


def test_search_requires_login(raw_client):
    assert raw_client.get("/api/library/search", params={"q": "x"}).status_code == 401
