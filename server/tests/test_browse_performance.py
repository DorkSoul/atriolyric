"""Browse paths do work proportional to the page, not the library:
played-order sorts and folder videos page in SQL, genre lists are shared and
cached, full lists carry an ETag."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import event

from app.artist_credits import rebuild_all_credits
from app.custom_art import album_entity_key
from app.library_cache import bump_library_version
from app.metadata_taxonomy import replace_item_genres
from app.models import CustomArt, LibraryRoot, MediaItem, MediaKind, PlayHistory, User


def _root(db_session, path="/m") -> LibraryRoot:
    root = LibraryRoot(path=path, display_name=path)
    db_session.add(root)
    db_session.commit()
    return root


def _track(db_session, root, title, artist, album=None, **values) -> MediaItem:
    item = MediaItem(
        library_root_id=root.id, kind=MediaKind.track, relative_path=f"{artist}/{album}/{title}.mp3",
        title=title, artist=artist, album=album, **values,
    )
    db_session.add(item)
    db_session.commit()
    return item


def _statements(engine):
    seen: list[str] = []

    def record(_conn, _cursor, statement, *_args):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    return seen, lambda: event.remove(engine, "before_cursor_execute", record)


def test_played_sorts_match_the_old_python_order_with_accents_and_ties(client, db_session):
    root = _root(db_session)
    user = db_session.query(User).one()
    names = [("Ébc", "x"), ("éab", "x"), ("abba", "Gold"), ("ABBA", "gold"), ("Zed", "a"), ("zed", "B"), ("Ödland", "o")]
    items = [_track(db_session, root, f"t{n}", artist, album) for n, (artist, album) in enumerate(names)]
    rebuild_all_credits(db_session)
    base = datetime(2026, 1, 1)
    for item, plays in zip(items, (2, 2, 0, 1, 2, 0, 1), strict=True):
        for n in range(plays):
            db_session.add(PlayHistory(user_id=user.id, media_item_id=item.id, played_at=base + timedelta(hours=item.id * 3 + n)))
    db_session.commit()

    def reference(sort, direction):
        # A plain Python statement of the order: sort the grouped rows.
        rows = sorted((artist, album) for artist, album in names)
        stats = {}
        for (artist, album), plays in zip(names, (2, 2, 0, 1, 2, 0, 1), strict=True):
            item = items[names.index((artist, album))]
            stats[(artist, album)] = (plays, base + timedelta(hours=item.id * 3 + plays - 1) if plays else None)

        def name_key(row):
            return (row[0].lower(), row[1].lower())

        def value(row):
            count, last = stats[row]
            return count if sort == "most_played" else (last.timestamp() if last else 0)

        # `desc` is most played / most recent first, `asc` the reverse; names
        # stay A-Z in both, and never-played albums come last either way.
        played = sorted((r for r in rows if stats[r][0]), key=lambda r: (-value(r) if direction == "desc" else value(r), *name_key(r)))
        unplayed = sorted((r for r in rows if not stats[r][0]), key=name_key)
        return [list(r) for r in played + unplayed]

    for sort in ("most_played", "last_played"):
        for direction in ("asc", "desc"):
            got = client.get("/api/library/albums", params={"sort": sort, "direction": direction}).json()
            assert [[a["artist"], a["album"]] for a in got] == reference(sort, direction), (sort, direction)
            page = client.get(
                "/api/library/albums", params={"sort": sort, "direction": direction, "limit": 2, "offset": 3}
            ).json()
            assert [[a["artist"], a["album"]] for a in page] == reference(sort, direction)[3:5]
    most = client.get("/api/library/artists", params={"sort": "most_played"}).json()
    # Two plays each, then one; Python's lower() puts "é" after "z".
    assert [a["artist"] for a in most] == ["Zed", "éab", "Ébc", "abba", "Ödland"]


def test_folder_videos_are_paged_in_sql(client, db_session):
    root = _root(db_session)
    for path in ("Movies/Action/b.mkv", "Movies/Action/a.mkv", "Movies/Action/Sub/c.mkv", "Movies/x.mkv", "top.mkv"):
        db_session.add(MediaItem(library_root_id=root.id, kind=MediaKind.video, relative_path=path, title=path.rsplit("/", 1)[-1]))
    db_session.commit()

    def titles(**params):
        return [v["title"] for v in client.get("/api/library/videos", params=params).json()]

    assert titles(dir="Movies/Action") == ["a.mkv", "b.mkv"]
    assert titles(dir="Movies/Action", limit=1, offset=1) == ["b.mkv"]
    assert titles(dir="Movies") == ["x.mkv"]
    assert titles(dir="") == ["top.mkv"]


def test_genre_lists_count_a_track_once_and_are_cached(client, db_session, session_factory):
    root = _root(db_session)
    # The legacy column says "rock"; the track's canonical genre is "Rock".
    item = _track(db_session, root, "Song", "A", genre="rock")
    replace_item_genres(db_session, item, ["Rock"], "embedded")
    legacy_only = _track(db_session, root, "Old", "B", genre="Blues")
    db_session.commit()
    assert legacy_only.genres == []

    # /genres and /mixes/options do not list "rock" beside "Rock".
    assert client.get("/api/library/genres").json() == ["Blues", "Rock"]
    seen, stop = _statements(session_factory.kw["bind"])
    try:
        options = client.get("/api/library/mixes/options").json()
    finally:
        stop()
    assert [(o["kind"], o["name"], o["track_count"]) for o in options] == [("genre", "Blues", 1), ("genre", "Rock", 1)]
    assert not any("media_item_genres" in s for s in seen)  # served from the cache

    _track(db_session, root, "New", "C", genre="Jazz")
    bump_library_version()
    assert "Jazz" in client.get("/api/library/genres").json()


def test_album_and_artist_lists_skip_the_art_lookup_when_there_is_no_custom_art(client, db_session, session_factory):
    root = _root(db_session)
    for n in range(3):
        _track(db_session, root, f"t{n}", f"Artist {n}", f"Album {n}")
    rebuild_all_credits(db_session)
    client.get("/api/library/albums", params={"full": 1})
    seen, stop = _statements(session_factory.kw["bind"])
    try:
        client.get("/api/library/albums", params={"full": 1})
        client.get("/api/library/artists", params={"full": 1})
    finally:
        stop()
    art_queries = [s for s in seen if "custom_art" in s]
    assert len(art_queries) == 2 and all("LIMIT" in s for s in art_queries)


def test_full_lists_look_up_custom_art_in_a_constant_number_of_statements(
    client, db_session, session_factory
):
    """With some custom art present, each full album and artist list reads
    the art once (not an IN query per 500 entries on every request, ~64 for
    a large album list), and a change shows at once."""
    root = _root(db_session)
    db_session.add_all(
        MediaItem(
            library_root_id=root.id, kind=MediaKind.track, relative_path=f"{n}/t.mp3",
            title="t", artist=f"Artist {n}", album=f"Album {n}",
        )
        for n in range(1200)
    )
    db_session.commit()
    rebuild_all_credits(db_session)
    album_art = CustomArt(
        entity_type="album", entity_key=album_entity_key("Artist 7", "Album 7"),
        path="a.jpg", thumb_path="a-thumb.jpg",
    )
    db_session.add_all([
        album_art,
        CustomArt(entity_type="artist", entity_key="Artist 9", path="b.jpg", thumb_path="b-thumb.jpg"),
    ])
    db_session.commit()

    def custom(path: str, field: str, value: str) -> bool:
        rows = client.get(path, params={"full": 1}).json()
        assert len(rows) == 1200
        return next(r for r in rows if r[field] == value)["custom_art"]

    assert custom("/api/library/albums", "album", "Album 7") is True
    seen, stop = _statements(session_factory.kw["bind"])
    try:
        assert custom("/api/library/albums", "album", "Album 7") is True
        assert custom("/api/library/artists", "artist", "Artist 9") is True
    finally:
        stop()
    assert len([s for s in seen if "custom_art" in s]) == 2

    db_session.delete(album_art)
    db_session.commit()
    assert custom("/api/library/albums", "album", "Album 7") is False


def test_full_lists_answer_304_until_the_library_changes(client, db_session):
    root = _root(db_session)
    _track(db_session, root, "t", "Artist", "Album")
    rebuild_all_credits(db_session)
    for path in ("/api/library/albums", "/api/library/artists"):
        first = client.get(path, params={"full": 1})
        etag = first.headers["etag"]
        assert etag.startswith('W/"')
        again = client.get(path, params={"full": 1}, headers={"If-None-Match": etag})
        assert again.status_code == 304 and again.content == b""
        assert again.headers["cache-control"].startswith("private")
        # Another query is another tag.
        other = client.get(path, params={"full": 1, "direction": "desc"}, headers={"If-None-Match": etag})
        assert other.status_code == 200
        bump_library_version()
        changed = client.get(path, params={"full": 1}, headers={"If-None-Match": etag})
        assert changed.status_code == 200 and changed.headers["etag"] != etag
    # Paged requests are not tagged.
    assert "etag" not in client.get("/api/library/albums").headers
