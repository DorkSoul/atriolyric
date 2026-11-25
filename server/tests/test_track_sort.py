"""The Songs tab's sorts on /api/library/tracks (sort, direction), the year
and play_count fields, and the "library" playback queue following the sort."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import event

from app import playback
from app.library_query import TrackSort
from app.models import (
    Album,
    LibraryRoot,
    MediaItem,
    MediaKind,
    PlayHistory,
    User,
    UserLibraryAccess,
    utcnow,
)
from app.security import hash_password
from tests.conftest import DEFAULT_USERNAME, login_as

# title, artist, album, track number, file date, album original date, days ago added
_ROWS = [
    ("delta", "Band B", "Second", 2, "2005-01-01", None, 5),
    ("Alpha", "Band B", "Second", 1, "2005-01-01", None, 4),
    ("charlie", "Band A", "First", 1, "1999-06-01", "1975-01-01", 3),
    ("Bravo", "Band A", "First", 2, None, "1975-01-01", 2),
    ("echo", None, None, None, None, None, 1),
    ("alpha", "Band C", "Third", 1, "2005-03-03", None, 0),
]


def _me(db_session) -> User:
    return db_session.query(User).filter_by(username=DEFAULT_USERNAME).one()


def _library(db_session) -> dict[str, MediaItem]:
    root = LibraryRoot(path="/fake-sort", display_name="Shared")
    db_session.add(root)
    db_session.commit()
    first = Album(owner="Band A", title="First", owner_key="band a", title_key="first", first_release_date="1975-01-01")
    db_session.add(first)
    db_session.commit()
    now = utcnow()
    items: dict[str, MediaItem] = {}
    for i, (title, artist, album, number, date, album_date, days) in enumerate(_ROWS):
        item = MediaItem(
            library_root_id=root.id,
            kind=MediaKind.track,
            relative_path=f"{i}.mp3",
            title=title,
            artist=artist,
            album=album,
            genre="Rock" if i % 2 == 0 else "Jazz",
            track_number=number,
            release_date=date,
            album_id=first.id if album_date else None,
            created_at=now - timedelta(days=days),
        )
        db_session.add(item)
        items[title] = item
    db_session.commit()
    return items


def _play(db_session, user: User, item: MediaItem, times: int, minutes_ago: int = 0) -> None:
    for n in range(times):
        db_session.add(
            PlayHistory(user_id=user.id, media_item_id=item.id, played_at=utcnow() - timedelta(minutes=minutes_ago + n))
        )
    db_session.commit()


def _titles(client, **params) -> list[str]:
    response = client.get("/api/library/tracks", params=params)
    assert response.status_code == 200, response.text
    return [t["title"] for t in response.json()]


def _default_order(items: dict[str, MediaItem]) -> list[str]:
    """The default order, computed by hand: artist, album, track number, id,
    NULLs first (SQLite's ascending order)."""

    def key(item: MediaItem):
        return (
            item.artist is not None, item.artist or "",
            item.album is not None, item.album or "",
            item.track_number is not None, item.track_number or 0,
            item.id,
        )

    return [item.title for item in sorted(items.values(), key=key)]


def test_default_order_is_unchanged_and_artist_asc_is_the_same(client, db_session):
    items = _library(db_session)
    expected = _default_order(items)
    assert _titles(client) == expected
    assert _titles(client, sort="artist") == expected
    assert _titles(client, sort="artist", direction="asc") == expected
    assert _titles(client, sort="artist", direction="desc") == list(reversed(expected))


def test_title_and_album_sorts_fold_case_and_put_no_album_last(client, db_session):
    _library(db_session)
    # "Alpha" and "alpha" fold to a tie: the raw value splits them.
    assert _titles(client, sort="title") == ["Alpha", "alpha", "Bravo", "charlie", "delta", "echo"]
    assert _titles(client, sort="title", direction="desc") == ["echo", "delta", "charlie", "Bravo", "alpha", "Alpha"]
    # Ties within an album keep the default order (track number); no album last.
    assert _titles(client, sort="album") == ["charlie", "Bravo", "Alpha", "delta", "alpha", "echo"]
    assert _titles(client, sort="album", direction="desc") == ["alpha", "Alpha", "delta", "charlie", "Bravo", "echo"]


def test_year_sort_uses_the_album_date_and_puts_unknown_years_last(client, db_session):
    _library(db_session)
    # Newest first by default; ties (2005) in the default order; no year last.
    assert _titles(client, sort="year") == ["Alpha", "delta", "alpha", "charlie", "Bravo", "echo"]
    assert _titles(client, sort="year", direction="asc") == ["charlie", "Bravo", "Alpha", "delta", "alpha", "echo"]
    years = {t["title"]: t["year"] for t in client.get("/api/library/tracks").json()}
    # The album page's date (its original release) wins over the file's tag.
    assert years == {"delta": 2005, "Alpha": 2005, "charlie": 1975, "Bravo": 1975, "echo": None, "alpha": 2005}


def test_date_added_sort(client, db_session):
    _library(db_session)
    newest_first = ["alpha", "echo", "Bravo", "charlie", "Alpha", "delta"]
    assert _titles(client, sort="date_added") == newest_first
    assert _titles(client, sort="date_added", direction="asc") == list(reversed(newest_first))


def test_played_sorts_are_per_user_and_put_never_played_last(client, session_factory, db_session):
    items = _library(db_session)
    me = _me(db_session)
    other = User(username="other", password_hash=hash_password("otherpass"))
    db_session.add(other)
    db_session.commit()
    _play(db_session, me, items["echo"], 3, minutes_ago=60)
    _play(db_session, me, items["Bravo"], 1, minutes_ago=1)
    _play(db_session, me, items["delta"], 1, minutes_ago=30)
    _play(db_session, other, items["alpha"], 5, minutes_ago=5)
    _play(db_session, other, items["echo"], 1, minutes_ago=10)
    unplayed_mine = [t for t in _default_order(items) if t not in {"echo", "Bravo", "delta"}]

    # Ties (one play each) keep the default order: Bravo (Band A) before delta.
    assert _titles(client, sort="most_played") == ["echo", "Bravo", "delta", *unplayed_mine]
    assert _titles(client, sort="most_played", direction="asc") == ["Bravo", "delta", "echo", *unplayed_mine]
    assert _titles(client, sort="last_played") == ["Bravo", "delta", "echo", *unplayed_mine]
    assert _titles(client, sort="last_played", direction="asc") == ["echo", "delta", "Bravo", *unplayed_mine]

    other_client = login_as(session_factory, client, "other", "otherpass")
    unplayed_theirs = [t for t in _default_order(items) if t not in {"alpha", "echo"}]
    assert _titles(other_client, sort="most_played") == ["alpha", "echo", *unplayed_theirs]
    assert _titles(other_client, sort="last_played") == ["alpha", "echo", *unplayed_theirs]

    counts = {t["title"]: t["play_count"] for t in client.get("/api/library/tracks").json()}
    assert counts == {"echo": 3, "Bravo": 1, "delta": 1, "charlie": 0, "Alpha": 0, "alpha": 0}
    theirs = {t["title"]: t["play_count"] for t in other_client.get("/api/library/tracks").json()}
    assert theirs["alpha"] == 5 and theirs["echo"] == 1 and theirs["Bravo"] == 0


def test_played_sorts_are_never_cached(client, db_session):
    items = _library(db_session)
    me = _me(db_session)
    assert _titles(client, sort="most_played")[0] == _default_order(items)[0]
    _play(db_session, me, items["delta"], 2)
    assert _titles(client, sort="most_played")[0] == "delta"
    # play_count is filled outside the cache for every sort.
    assert client.get("/api/library/tracks", params={"sort": "title"}).json()[4]["play_count"] == 2
    _play(db_session, me, items["delta"], 1)
    assert client.get("/api/library/tracks", params={"sort": "title"}).json()[4]["play_count"] == 3


@pytest.mark.parametrize("sort", [s.value for s in TrackSort])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_paging_is_stable_for_every_sort(client, db_session, sort, direction):
    items = _library(db_session)
    me = _me(db_session)
    _play(db_session, me, items["echo"], 2)
    _play(db_session, me, items["Bravo"], 2)
    whole = _titles(client, sort=sort, direction=direction)
    paged = [
        title
        for offset in range(0, len(items), 2)
        for title in _titles(client, sort=sort, direction=direction, limit=2, offset=offset)
    ]
    assert paged == whole
    assert sorted(whole) == sorted(items)


def test_filters_combine_with_every_sort(client, db_session):
    _library(db_session)
    rock = {"delta", "charlie", "echo"}
    for sort in TrackSort:
        assert set(_titles(client, sort=sort.value, genre="Rock")) == rock
        assert _titles(client, sort=sort.value, q="alpha") == [
            t for t in _titles(client, sort=sort.value) if t.lower() == "alpha"
        ]
    assert _titles(client, sort="title", direction="desc", genre="Rock") == ["echo", "delta", "charlie"]
    assert _titles(client, sort="year", artist="Band A") == ["charlie", "Bravo"]


def test_bad_sort_or_direction_is_rejected(client, db_session):
    assert client.get("/api/library/tracks", params={"sort": "loudness"}).status_code == 422
    assert client.get("/api/library/tracks", params={"direction": "up"}).status_code == 422


def test_played_sort_respects_a_private_root(client, session_factory, db_session):
    items = _library(db_session)
    private = LibraryRoot(path="/fake-private", display_name="Private", is_shared=False)
    db_session.add(private)
    db_session.commit()
    secret = MediaItem(library_root_id=private.id, kind=MediaKind.track, relative_path="s.mp3", title="secret", artist="Z")
    db_session.add(secret)
    plain = User(username="plain", password_hash=hash_password("plainpass"))
    db_session.add(plain)
    db_session.commit()
    _play(db_session, plain, secret, 9)
    _play(db_session, plain, items["delta"], 1)
    plain_client = login_as(session_factory, client, "plain", "plainpass")
    for sort in ("most_played", "last_played", "year", "title"):
        assert "secret" not in _titles(plain_client, sort=sort)
    assert _titles(plain_client, sort="most_played")[0] == "delta"

    db_session.add(UserLibraryAccess(user_id=plain.id, library_root_id=private.id))
    db_session.commit()
    assert _titles(plain_client, sort="most_played")[:2] == ["secret", "delta"]


def _count_statements(db_session, fn) -> int:
    engine = db_session.get_bind()
    seen: list[str] = []

    def record(_conn, _cursor, statement, *_args):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return len(seen)


def test_query_count_does_not_grow_with_page_size(client, db_session):
    root = LibraryRoot(path="/fake-many", display_name="Many")
    db_session.add(root)
    db_session.commit()
    me = _me(db_session)
    for i in range(60):
        item = MediaItem(
            library_root_id=root.id, kind=MediaKind.track, relative_path=f"{i}.mp3",
            title=f"T{i:02}", artist=f"A{i % 7}", release_date="2001-01-01",
        )
        db_session.add(item)
        db_session.flush()
        db_session.add(PlayHistory(user_id=me.id, media_item_id=item.id))
    db_session.commit()
    for sort in ("artist", "most_played"):
        small = _count_statements(db_session, lambda s=sort: _titles(client, sort=s, limit=5))
        large = _count_statements(db_session, lambda s=sort: _titles(client, sort=s, limit=60))
        assert large == small, (sort, small, large)


# --- the playback queue follows the sort -----------------------------------------


def _queue_ids(db_session, user) -> list[int]:
    return [q.media_item_id for q in playback._queue(db_session, user)]


def _start(db_session, user, start: MediaItem, **sorting) -> None:
    error = playback.set_queue_context(
        db_session, user, {"kind": "library", "filters": {}, **sorting}, start.id
    )
    assert error is None


@pytest.mark.parametrize(
    ("sort", "direction"),
    [("year", None), ("year", "asc"), ("title", "desc"), ("album", None), ("date_added", None), ("artist", "desc")],
)
def test_the_library_queue_plays_in_the_sorted_order(client, db_session, sort, direction):
    _library(db_session)
    me = _me(db_session)
    params = {"sort": sort, **({"direction": direction} if direction else {})}
    listed = client.get("/api/library/tracks", params=params).json()
    by_id = {item.id: item for item in db_session.query(MediaItem).all()}
    third = by_id[listed[2]["id"]]
    _start(db_session, me, third, **params)
    # The clicked (third) row first, then what follows it on screen.
    assert _queue_ids(db_session, me) == [t["id"] for t in listed[2:]]


def test_the_default_library_queue_is_unchanged(client, db_session):
    items = _library(db_session)
    me = _me(db_session)
    listed = [t["id"] for t in client.get("/api/library/tracks").json()]
    _start(db_session, me, items[_default_order(items)[1]])
    assert _queue_ids(db_session, me) == listed[1:]
    state = playback.get_or_create_state(db_session, me)
    assert "sort" not in state.queue_context_json
    # The default sort named explicitly is stored the same way.
    _start(db_session, me, items[_default_order(items)[1]], sort="artist", direction="asc")
    assert "sort" not in playback.get_or_create_state(db_session, me).queue_context_json


def test_a_played_sort_queue_keeps_its_order_while_it_plays(client, db_session):
    items = _library(db_session)
    me = _me(db_session)
    _play(db_session, me, items["echo"], 3, minutes_ago=10)
    _play(db_session, me, items["delta"], 2, minutes_ago=10)
    listed = [t["id"] for t in client.get("/api/library/tracks", params={"sort": "most_played"}).json()]
    _start(db_session, me, items["echo"], sort="most_played")
    # Plays made after the queue started do not reorder what it draws.
    for n in range(9):
        db_session.add(PlayHistory(user_id=me.id, media_item_id=items["alpha"].id, played_at=utcnow() + timedelta(seconds=n + 1)))
    db_session.commit()
    assert _queue_ids(db_session, me) == listed
    state = playback.get_or_create_state(db_session, me)
    assert state.queue_context_json["played_before"]
    playback._drop_unplayed_generated(db_session, me, state)
    state.tail_cursor = 1
    playback._ensure_window(db_session, me, state)
    assert _queue_ids(db_session, me) == listed


def test_an_invalid_queue_sort_is_refused(db_session):
    me = _me(db_session)
    items = _library(db_session)
    for bad in ({"sort": "loudness"}, {"sort": "year", "direction": "up"}, {"direction": 3}):
        assert playback.set_queue_context(
            db_session, me, {"kind": "library", "filters": {}, **bad}, items["echo"].id
        ) == "Invalid queue"
