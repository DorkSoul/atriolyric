from __future__ import annotations

from mutagen.id3 import TPE1, TPE2, TXXX
from mutagen.wave import WAVE

from app.artist_credits import (
    DEFAULT_EXCEPTIONS,
    DEFAULT_SEPARATORS,
    ROLE_ALBUM_ARTIST,
    ROLE_FEATURED,
    ROLE_MAIN,
    SplitRules,
    compute_credits,
    featured_from_title,
    normalize_artist_name,
    rebuild_all_credits,
    split_artist_string,
)
from app.models import (
    Artist,
    CollectionPreference,
    LibraryRoot,
    MediaItem,
    TrackArtist,
    User,
)
from app.scanner import scan_library_root
from tests.conftest import DEFAULT_USERNAME, make_audio_fixture

RULES = SplitRules(tuple(DEFAULT_SEPARATORS), tuple(DEFAULT_EXCEPTIONS))


def _credits(**kwargs):
    defaults = dict(
        title="Song", artist=None, album_artist=None, artists_multi=None, album_artists_multi=None
    )
    return compute_credits(**{**defaults, **kwargs}, rules=RULES)


def test_split_on_default_separators_and_feat_markers():
    assert split_artist_string("A; B", RULES) == (["A", "B"], [])
    assert split_artist_string("A / B", RULES) == (["A", "B"], [])
    assert split_artist_string("Drake feat. Rihanna & Future", RULES) == (
        ["Drake"],
        ["Rihanna", "Future"],
    )
    assert split_artist_string("A ft. B, C", RULES) == (["A"], ["B", "C"])
    assert split_artist_string("A Featuring B", RULES) == (["A"], ["B"])
    # "&" and "," alone never split the main artists by default.
    assert split_artist_string("Simon & Garfunkel", RULES) == (["Simon & Garfunkel"], [])
    assert split_artist_string("Defeater", RULES) == (["Defeater"], [])


def test_exceptions_are_never_split():
    assert split_artist_string("AC/DC", RULES) == (["AC/DC"], [])
    assert split_artist_string("AC/DC; Tyler, The Creator", RULES) == (
        ["AC/DC", "Tyler, The Creator"],
        [],
    )
    assert split_artist_string("Kanye West feat. Tyler, The Creator", RULES) == (
        ["Kanye West"],
        ["Tyler, The Creator"],
    )


def test_featured_artists_from_title():
    assert featured_from_title("Song (feat. X)", RULES) == ["X"]
    assert featured_from_title("Song [ft. X & Y]", RULES) == ["X", "Y"]
    assert featured_from_title("Featherweight", RULES) == []


def test_credits_roles_and_album_artist_fallback():
    assert _credits(artist="A feat. B", title="Song (feat. C)") == [
        ("A", ROLE_MAIN),
        ("B", ROLE_FEATURED),
        ("C", ROLE_FEATURED),
        ("A", ROLE_ALBUM_ARTIST),
    ]
    assert _credits(artist="A", album_artist="Various Artists") == [
        ("A", ROLE_MAIN),
        ("Various Artists", ROLE_ALBUM_ARTIST),
    ]
    # A guest named in both the artist tag and the title is credited once.
    assert _credits(artist="A feat. B", title="Song (feat. B)") == [
        ("A", ROLE_MAIN),
        ("B", ROLE_FEATURED),
        ("A", ROLE_ALBUM_ARTIST),
    ]


def test_multi_value_tags_use_display_string_for_guests():
    assert _credits(artist="A feat. B", artists_multi=["A", "B"]) == [
        ("A", ROLE_MAIN),
        ("B", ROLE_FEATURED),
        ("A", ROLE_ALBUM_ARTIST),
    ]
    assert _credits(artist="A & B", artists_multi=["A", "B"]) == [
        ("A", ROLE_MAIN),
        ("B", ROLE_MAIN),
        ("A", ROLE_ALBUM_ARTIST),
        ("B", ROLE_ALBUM_ARTIST),
    ]
    assert _credits(artist="A", album_artists_multi=["A", "B"]) == [
        ("A", ROLE_MAIN),
        ("A", ROLE_ALBUM_ARTIST),
        ("B", ROLE_ALBUM_ARTIST),
    ]


def test_normalized_names_ignore_case_and_accents():
    assert normalize_artist_name("Beyoncé ") == normalize_artist_name("beyonce")


def _scan(db_session, library_dir):
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    scan_library_root(db_session, root)
    return root


def _links(db_session, title: str) -> list[tuple[str, str]]:
    item = db_session.query(MediaItem).filter_by(title=title).one()
    rows = (
        db_session.query(Artist.name, TrackArtist.role)
        .join(TrackArtist, TrackArtist.artist_id == Artist.id)
        .filter(TrackArtist.media_item_id == item.id)
        .order_by(TrackArtist.position)
        .all()
    )
    return [tuple(row) for row in rows]


def test_scanner_reads_multi_value_artists_tag(db_session, library_dir):
    path = library_dir / "collab.wav"
    make_audio_fixture(path, title="Collab", artist="A feat. B", album="LP", genre=None)
    audio = WAVE(str(path))
    audio.tags.add(TXXX(encoding=3, desc="ARTISTS", text=["A", "B"]))
    audio.tags.add(TPE2(encoding=3, text=["A", "C"]))
    audio.save()
    _scan(db_session, library_dir)
    assert _links(db_session, "Collab") == [
        ("A", ROLE_MAIN),
        ("B", ROLE_FEATURED),
        ("A", ROLE_ALBUM_ARTIST),
        ("C", ROLE_ALBUM_ARTIST),
    ]
    item = db_session.query(MediaItem).filter_by(title="Collab").one()
    assert item.album_artist == "A, C"
    assert item.artists_scanned_at is not None


def test_scanner_joins_a_multi_valued_artist_frame(db_session, library_dir):
    path = library_dir / "duet.wav"
    make_audio_fixture(path, title="Duet", artist="X", album="LP", genre=None)
    audio = WAVE(str(path))
    audio.tags.setall("TPE1", [TPE1(encoding=3, text=["X", "Y"])])
    audio.save()
    _scan(db_session, library_dir)
    item = db_session.query(MediaItem).filter_by(title="Duet").one()
    assert item.artist == "X, Y"
    assert [name for name, role in _links(db_session, "Duet") if role == ROLE_MAIN] == ["X", "Y"]


def _sample(db_session, library_dir):
    make_audio_fixture(library_dir / "1.wav", title="One", artist="Host", album="Host LP", genre=None)
    make_audio_fixture(
        library_dir / "2.wav", title="Two (feat. Guest)", artist="Host", album="Host LP", genre=None
    )
    make_audio_fixture(library_dir / "3.wav", title="Three", artist="Guest", album="Guest EP", genre=None)
    make_audio_fixture(
        library_dir / "4.wav", title="Four", artist="Only Guest feat. Visitor", album="Other", genre=None
    )
    return _scan(db_session, library_dir)


def test_featured_only_artist_is_listed_with_appears_on(client, db_session, library_dir):
    _sample(db_session, library_dir)
    artists = {a["artist"]: a for a in client.get("/api/library/artists").json()}
    assert artists["Visitor"]["track_count"] == 1
    assert artists["Visitor"]["album_count"] == 0
    assert artists["Guest"]["track_count"] == 2
    assert artists["Guest"]["album_count"] == 1
    assert "Only Guest feat. Visitor" not in artists

    own = client.get("/api/library/albums", params={"artist": "Guest"}).json()
    assert [(a["artist"], a["album"]) for a in own] == [("Guest", "Guest EP")]
    appears = client.get("/api/library/albums", params={"artist": "Guest", "appears_on": 1}).json()
    assert [(a["artist"], a["album"]) for a in appears] == [("Host", "Host LP")]

    # Play all on an artist's page plays their own songs, not guest spots.
    songs = client.get("/api/library/tracks", params={"artist": "Guest"}).json()
    assert {s["title"] for s in songs} == {"Three"}


def test_tracks_carry_their_artist_credits(client, db_session, library_dir):
    _sample(db_session, library_dir)
    songs = {s["title"]: s for s in client.get("/api/library/tracks").json()}
    assert songs["Two (feat. Guest)"]["artists"] == [
        {"name": "Host", "role": "main"},
        {"name": "Guest", "role": "featured"},
    ]
    assert songs["Four"]["artist"] == "Only Guest feat. Visitor"
    albums = {a["album"]: a for a in client.get("/api/library/albums").json()}
    assert albums["Host LP"]["artists"] == [{"name": "Host", "role": "album_artist"}]


def test_disliking_a_main_artist_but_not_a_guest(client, db_session, library_dir):
    _sample(db_session, library_dir)
    user = db_session.query(User).filter_by(username=DEFAULT_USERNAME).one()
    db_session.add(
        CollectionPreference(user_id=user.id, kind="artist", artist="Visitor", state="disliked")
    )
    db_session.add(
        CollectionPreference(user_id=user.id, kind="artist", artist="Only Guest", state="disliked")
    )
    db_session.commit()
    songs = {s["title"]: s for s in client.get("/api/library/tracks").json()}
    # "Only Guest" is the main artist of "Four" (split from its tag).
    assert songs["Four"]["excluded_by"] == "artist"
    assert songs["Two (feat. Guest)"]["excluded_by"] is None


def test_split_rules_endpoint_relinks_without_rescanning(client, db_session, library_dir):
    make_audio_fixture(library_dir / "x.wav", title="X", artist="A & B", album="LP", genre=None)
    _scan(db_session, library_dir)
    assert {a["artist"] for a in client.get("/api/library/artists").json()} == {"A & B"}

    rules = client.get("/api/admin/artist-split-rules").json()
    assert rules["separators"] == DEFAULT_SEPARATORS
    resp = client.put(
        "/api/admin/artist-split-rules",
        json={"separators": [*rules["separators"], " & "], "exceptions": rules["exceptions"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["relinked"] == 1
    assert {a["artist"] for a in client.get("/api/library/artists").json()} == {"A", "B"}
    # The old combined artist is gone once nothing links to it.
    assert db_session.query(Artist).filter_by(name="A & B").count() == 0


def test_rebuild_backfills_rows_without_links(db_session):
    root = LibraryRoot(path="/m", display_name="m")
    db_session.add(root)
    db_session.commit()
    db_session.add(MediaItem(library_root_id=root.id, relative_path="a.mp3", title="T", artist="P; Q"))
    db_session.commit()
    assert rebuild_all_credits(db_session, only_missing=True) == 1
    assert rebuild_all_credits(db_session, only_missing=True) == 0
    names = {a.name for a in db_session.query(Artist)}
    assert names == {"P", "Q"}


def test_comma_display_backfill_rewrites_joined_multi_value_tags(db_session, monkeypatch):
    from unittest.mock import MagicMock

    from app import migrations

    root = LibraryRoot(path="/m", display_name="m")
    db_session.add(root)
    db_session.commit()
    db_session.add_all([
        MediaItem(
            library_root_id=root.id, relative_path="m.mp3", title="T", artist="A; B", album_artist="A; B",
            artist_tags_json={"artists": ["A", "B"], "album_artists": ["A", "B"]},
        ),
        # A single tag that merely contains a semicolon is left as it was.
        MediaItem(library_root_id=root.id, relative_path="s.mp3", title="S", artist="C; D"),
    ])
    db_session.commit()
    session = MagicMock(wraps=db_session)
    session.close = lambda: None
    monkeypatch.setattr(migrations, "SessionLocal", lambda: session)
    migrations.backfill_artist_comma_display()
    db_session.expire_all()
    multi = db_session.query(MediaItem).filter_by(title="T").one()
    assert (multi.artist, multi.album_artist, multi.album_owner) == ("A, B", "A, B", "A, B")
    assert db_session.query(MediaItem).filter_by(title="S").one().artist == "C; D"
    assert _links(db_session, "T") == [
        ("A", ROLE_MAIN), ("B", ROLE_MAIN), ("A", ROLE_ALBUM_ARTIST), ("B", ROLE_ALBUM_ARTIST),
    ]


def test_tag_edit_relinks_credits(client, db_session, library_dir):
    make_audio_fixture(library_dir / "e.wav", title="E", artist="Old", album="LP", genre=None)
    _scan(db_session, library_dir)
    item = db_session.query(MediaItem).filter_by(title="E").one()
    resp = client.patch(f"/api/admin/media/{item.id}/tags", json={"artist": "New feat. Pal"})
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert _links(db_session, "E") == [
        ("New", ROLE_MAIN),
        ("Pal", ROLE_FEATURED),
        ("New", ROLE_ALBUM_ARTIST),
    ]


def test_artist_summaries_for_the_picker(client, db_session, library_dir):
    _sample(db_session, library_dir)
    resp = client.get(
        "/api/library/artist-summaries", params=[("names", "Guest"), ("names", "host"), ("names", "Nobody")]
    )
    assert resp.status_code == 200
    assert [(a["artist"], a["track_count"]) for a in resp.json()] == [("Guest", 2), ("Host", 2)]
    assert all(a["art_key"] for a in resp.json())


def test_guest_spots_dont_split_an_album(client, db_session, library_dir):
    make_audio_fixture(library_dir / "1.wav", title="Solo", artist="Drake", album="Views", genre=None)
    make_audio_fixture(library_dir / "2.wav", title="One Dance", artist="Drake feat. Wizkid", album="Views", genre=None)
    _scan(db_session, library_dir)
    albums = client.get("/api/library/albums").json()
    assert [(a["artist"], a["album"], a["track_count"]) for a in albums] == [("Drake", "Views", 2)]
    tracks = client.get("/api/library/tracks", params={"artist": "Drake", "album": "Views"}).json()
    assert {t["title"] for t in tracks} == {"Solo", "One Dance"}
