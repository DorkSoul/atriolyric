"""The large text columns on MediaItem are deferred, and the paths that list
or pool tracks never load them, nor run a query per row."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event, select

from app.discovery.pools import _mix_pool
from app.models import Genre, LibraryRoot, MediaItem, MediaItemGenre, MediaKind, User

LARGE = (
    "musicbrainz_metadata_json",
    "lyrics_plain",
    "lyrics_synced",
    "artist_tags_json",
)


@contextmanager
def recorded_statements(engine) -> Iterator[list[str]]:
    """Every SQL statement `engine` runs inside the block."""
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _library(db_session, count: int, root_path: str) -> list[int]:
    root = LibraryRoot(path=root_path, display_name=root_path)
    genre = db_session.query(Genre).filter_by(normalized_name="rock").one_or_none() or Genre(
        name="Rock", normalized_name="rock"
    )
    db_session.add_all([root, genre])
    db_session.commit()
    items = [
        MediaItem(
            library_root_id=root.id,
            kind=MediaKind.track,
            relative_path=f"{n}.mp3",
            title=f"Song {n}",
            artist=f"Artist {n % 7}",
            album=f"Album {n % 3}",
            genre="Rock",
            musicbrainz_metadata_json={"padding": "x" * 5000},
            lyrics_plain="la " * 500,
            lyrics_synced="[00:01.00] la",
            artist_tags_json={"artists": ["A", "B"]},
        )
        for n in range(count)
    ]
    db_session.add_all(items)
    db_session.commit()
    db_session.add_all(
        MediaItemGenre(media_item_id=item.id, genre_id=genre.id, source="embedded") for item in items
    )
    db_session.commit()
    return [item.id for item in items]


def _loads_large_column(statements: list[str]) -> bool:
    return any(f"media_items.{name}" in s and "IS NOT NULL" not in s for s in statements for name in LARGE)


def test_a_plain_select_leaves_the_large_columns_out():
    sql = str(select(MediaItem))
    assert "media_items.title" in sql
    assert not any(f"media_items.{name}" in sql for name in LARGE)


def test_listing_tracks_and_resolving_a_queue_take_a_fixed_number_of_queries(
    client, db_session, session_factory
):
    engine = session_factory.kw["bind"]
    ids = _library(db_session, 50, "/large")

    def run(limit: int) -> list[str]:
        with recorded_statements(engine) as statements:
            assert len(client.get("/api/library/tracks", params={"limit": limit}).json()) == limit
            joined = ",".join(str(i) for i in ids[:limit])
            assert len(client.get("/api/library/media", params={"ids": joined}).json()) == limit
        return statements

    small, large = run(10), run(50)
    assert len(small) == len(large)
    assert not _loads_large_column(large)


def test_a_mix_pool_takes_a_fixed_number_of_queries_and_no_large_columns(db_session, session_factory):
    engine = session_factory.kw["bind"]
    user = db_session.query(User).one()
    _library(db_session, 10, "/small")

    def run() -> tuple[int, list[str]]:
        with recorded_statements(engine) as statements:
            pool, weights = _mix_pool(db_session, user, [("genre", "rock")], [("tag", "nope")])
        assert set(weights.values()) == {1.0}
        return len(pool), statements

    run()  # builds the artist-credit map, cached once per library version
    small_count, small = run()
    _library(db_session, 60, "/bigger")
    large_count, large = run()
    assert (small_count, large_count) == (10, 70)
    assert len(small) == len(large)
    assert not _loads_large_column(large)
